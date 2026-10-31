from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import flt

from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_CANCEL,
	ACTION_CLOSE,
	ACTION_SKIP,
	ACTION_SKIP_CANCEL,
	ACTION_SUBMIT,
	ACTION_SUBMIT_HOLD,
	ACTION_SUBMIT_INVOICE,
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

		if action in (ACTION_SUBMIT, ACTION_SUBMIT_INVOICE):
			self.resume_sales_order()
		elif action == ACTION_SUBMIT_HOLD:
			self.hold_sales_order()
		elif action in (ACTION_CANCEL, ACTION_SKIP_CANCEL):
			self.cancel_sales_order()
		elif action == ACTION_CLOSE:
			self.close_sales_order()

	def flag_sync_issue(self, message: str) -> None:
		self.sales_order.db_set("woocommerce_sync_issue", message, update_modified=False)

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
		Cancel the Sales Order, and the Sales Invoice made for it, as long as nothing was paid or delivered.
		Otherwise close it and flag it, as what to do about money and goods is for a person to decide.
		"""
		sales_order = self.sales_order
		if sales_order.docstatus != 1:
			return

		invoice = self.get_unpaid_sales_invoice()
		billed_elsewhere = flt(sales_order.per_billed) > 0 and not invoice
		if sales_order.woocommerce_payment_entry or flt(sales_order.per_delivered) > 0 or billed_elsewhere:
			self.close_sales_order()
			self.flag_sync_issue(
				_("Cancelled in WooCommerce, but it was already paid, invoiced or delivered. It is closed.")
			)
			return

		if invoice:
			invoice.cancel()
			sales_order.reload()
		sales_order.cancel()

	def get_unpaid_sales_invoice(self):
		"""
		The Sales Invoice made for the Sales Order if nothing was paid on it, otherwise None
		"""
		name = self.sales_order.get("woocommerce_sales_invoice")
		if not name or not frappe.db.exists("Sales Invoice", {"name": name, "docstatus": 1}):
			return None

		invoice = frappe.get_doc("Sales Invoice", name)
		if flt(invoice.outstanding_amount) < flt(invoice.rounded_total or invoice.grand_total):
			return None
		invoice.flags.created_by_sync = True
		return invoice
