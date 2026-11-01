from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import cstr, flt

from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_CANCEL,
	ACTION_CLOSE,
	ACTION_COMPLETE,
	ACTION_SKIP,
	ACTION_SUBMIT,
	ACTION_SUBMIT_HOLD,
	ACTION_SUBMIT_INVOICE,
	ACTION_SUBMIT_INVOICE_HOLD,
	DEFAULT_STATUSES,
	normalise_status,
)

if TYPE_CHECKING:
	from woocommerce_conduit.tasks.sync_sales_orders import SyncedOrder


def get_statuses_to_sync() -> list[str]:
	"""
	The WooCommerce statuses that do something in ERPNext on at least one enabled server. Orders with
	another status are only fetched when they are asked for by name.
	"""
	statuses = set()
	for server_name in frappe.get_all("WooCommerce Server", filters={"enabled": 1}, pluck="name"):
		server = frappe.get_cached_doc("WooCommerce Server", server_name)
		statuses.update(
			row.woocommerce_status
			for row in server.sales_order_status_map
			if row.erpnext_action != ACTION_SKIP
		)
	return sorted(
		statuses or {slug for slug, _label, action, _push in DEFAULT_STATUSES if action != ACTION_SKIP}
	)


def get_status_to_push(sales_order) -> str | None:
	"""
	The WooCommerce status to set for the status the Sales Order has now. None when the server doesn't push
	status changes, doesn't map this status, or the WooCommerce Order has that status already.
	"""
	if not (sales_order.woocommerce_server and sales_order.woocommerce_id):
		return None

	server = frappe.get_cached_doc("WooCommerce Server", sales_order.woocommerce_server)
	if not (server.enabled and server.enabled_order_status):
		return None

	row = server.get_push_row(sales_order.status)
	if not row or row.woocommerce_status == normalise_status(sales_order.woocommerce_status):
		return None
	return row.woocommerce_status


def enqueue_status_push_from_hook(doc, method=None):
	"""
	Intended to be triggered by the on_change of a Sales Order. That includes the status changes that
	ERPNext makes itself when a Delivery Note or Sales Invoice is submitted.
	"""
	if frappe.flags.in_test or doc.docstatus == 0 or doc.flags.get("created_by_sync"):
		return
	if not get_status_to_push(doc):
		return

	# Imported here, as the sync imports this module
	from woocommerce_conduit.tasks.sync_sales_orders import push_order_status

	frappe.enqueue(
		push_order_status,
		queue="long",
		timeout=300,
		job_id=f"woocommerce_order_status::{doc.name}",
		deduplicate=True,
		enqueue_after_commit=True,
		sales_order_name=doc.name,
	)


class SalesOrderStatusMixin:
	"""
	What a WooCommerce status does to the Sales Order of an order that was synced before
	"""

	sales_order: "SyncedOrder"

	def apply_status_action(self, action: str) -> None:
		# The status changes made here must not be pushed back to WooCommerce
		self.sales_order.flags.created_by_sync = True

		if action in (ACTION_SUBMIT, ACTION_SUBMIT_INVOICE, ACTION_COMPLETE):
			self.resume_sales_order()
		elif action == ACTION_SUBMIT_HOLD:
			self.hold_sales_order()
		elif action == ACTION_SUBMIT_INVOICE_HOLD:
			self.resume_sales_order()
			self.invoice_and_hold()
		elif action == ACTION_CANCEL:
			self.cancel_sales_order()
		elif action == ACTION_CLOSE:
			self.close_sales_order()

	def flag_sync_issue(self, message: str) -> None:
		# Added to the issues flagged before, so a later step doesn't hide one a person still has to look at
		flagged = cstr(self.sales_order.woocommerce_sync_issue)
		if message in flagged:
			return
		self.sales_order.db_set(
			"woocommerce_sync_issue", f"{flagged}; {message}" if flagged else message, update_modified=False
		)

	def run_flagged(self, step, message: str) -> None:
		"""
		Run a step that the Sales Order doesn't depend on. When it fails its changes are rolled back, and it
		is logged and flagged on the Sales Order (`message` gets the error), instead of failing the sync and
		with it the Sales Order.
		"""
		savepoint = "woocommerce_sync_step"
		frappe.db.savepoint(savepoint)
		try:
			step()
		except Exception as error:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(
				"WooCommerce Error",
				f"{frappe.get_traceback()}\n\nSales Order: {self.sales_order.name}",
			)
			self.flag_sync_issue(message.format(error))

	def hold_sales_order(self) -> None:
		if self.sales_order.docstatus == 1 and self.sales_order.status not in ("On Hold", "Closed"):
			self.sales_order.update_status("On Hold")

	def resume_sales_order(self) -> None:
		if self.sales_order.docstatus == 1 and self.sales_order.status == "On Hold":
			self.sales_order.update_status("Draft")

	def close_sales_order(self) -> None:
		if self.sales_order.docstatus == 1 and self.sales_order.status != "Closed":
			self.sales_order.update_status("Closed")

	def cancel_sales_order(self) -> None:
		"""
		Cancel the Sales Order, and its Sales Invoices, as long as nothing was paid or delivered and the
		invoices bill nothing but this order. Otherwise close it and flag it, as what to do about money and
		goods is for a person to decide.
		"""
		sales_order = self.sales_order
		if sales_order.docstatus != 1:
			return

		invoices = [invoice for invoice in self.get_invoices() if invoice.docstatus == 1]
		if (
			flt(sales_order.per_delivered) > 0
			or flt(sales_order.advance_paid) > 0
			or self.get_payment_entries()
			or not all(self.can_be_cancelled_with_the_order(invoice) for invoice in invoices)
		):
			self.close_sales_order()
			self.flag_sync_issue(
				_("Cancelled in WooCommerce, but it was already paid, invoiced or delivered. It is closed.")
			)
			return

		for invoice in invoices:
			invoice_doc = frappe.get_doc("Sales Invoice", invoice.name)
			invoice_doc.flags.created_by_sync = True
			invoice_doc.cancel()
		if invoices:
			sales_order.reload()
		sales_order.cancel()

	def can_be_cancelled_with_the_order(self, invoice) -> bool:
		"""
		Whether a submitted Sales Invoice of the order can be cancelled with it: it isn't a return, nothing was
		paid on it, and it bills nothing but this Sales Order
		"""
		if invoice.is_return or flt(invoice.outstanding_amount) < flt(
			invoice.rounded_total or invoice.grand_total
		):
			return False
		billed_orders = frappe.get_all(
			"Sales Invoice Item",
			filters={"parent": invoice.name, "parenttype": "Sales Invoice"},
			pluck="sales_order",
		)
		return set(billed_orders) == {self.sales_order.name}
