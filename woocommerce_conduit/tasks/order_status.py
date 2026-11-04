from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import cstr, flt

from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_CANCEL,
	ACTION_CLOSE,
	ACTION_HOLD,
	ACTION_INVOICE,
	ACTION_SKIP,
	ACTION_SUBMIT,
	DEFAULT_STATUSES,
	ENDING_ACTIONS,
	RESUMING_ACTIONS,
	TRASH_STATUS,
	normalise_status,
)

if TYPE_CHECKING:
	from woocommerce_conduit.tasks.sync_sales_orders import SyncedOrder
	from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder


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
			if ACTION_SKIP not in row.get_actions()
		)
	return sorted(
		statuses or {slug for slug, _label, actions, _push in DEFAULT_STATUSES if ACTION_SKIP not in actions}
	)


def get_status_to_push(sales_order) -> str | None:
	"""
	The WooCommerce status to set for the status the Sales Order has now. None when the server doesn't push
	status changes, doesn't map this status, or the WooCommerce Order has that status already.
	"""
	if not (sales_order.woocommerce_server and sales_order.woocommerce_id):
		return None
	# A status would take the order out of the trash, and one that was deleted has nothing to take it
	if normalise_status(sales_order.woocommerce_status) == TRASH_STATUS:
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
	if frappe.in_test or doc.docstatus == 0 or doc.flags.get("created_by_sync"):
		return
	# An Order Import's documents change the status of a Sales Order on ERPNext's own copy of it, without the
	# flag, and the shop's old orders mustn't be changed by them
	if frappe.flags.get("woocommerce_order_import"):
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

	sales_order: SyncedOrder
	woocommerce_order: WooCommerceOrder

	def apply_status_actions(self, actions: frozenset[str]) -> None:
		# The status changes made here must not be pushed back to WooCommerce
		self.sales_order.flags.created_by_sync = True

		if ACTION_CANCEL in actions:
			self.cancel_sales_order()
		elif ACTION_CLOSE in actions:
			self.close_sales_order()
		elif ACTION_HOLD in actions and ACTION_INVOICE in actions:
			self.resume_sales_order()
			self.invoice_and_hold()
		elif ACTION_HOLD in actions:
			self.hold_sales_order()
		elif actions & RESUMING_ACTIONS:
			self.resume_sales_order()

	def sync_deleted_order(self) -> None:
		"""
		The shop deleted the order for good. Its Sales Order does what the `trash` row of the status map ticks,
		once, as the order usually went through the trash. Nothing else of an order that is gone can be synced.
		"""
		sales_order = self.sales_order
		if not sales_order or sales_order.docstatus == 2:
			return
		if normalise_status(sales_order.woocommerce_status) == TRASH_STATUS:
			return

		wc_server = frappe.get_cached_doc("WooCommerce Server", sales_order.woocommerce_server)
		row = wc_server.get_order_status_row(TRASH_STATUS)
		sales_order.flags.created_by_sync = True
		sales_order.db_set("woocommerce_status", TRASH_STATUS, update_modified=False)
		self.apply_status_actions(row.get_actions() if row else frozenset())

	def came_back(self, actions: frozenset[str]) -> bool:
		"""
		Whether the order is back in WooCommerce after its status cancelled or closed the Sales Order: the
		status it has now makes Sales Orders, and the one the Sales Order last saw ended it. Say it was
		restored from the trash, or paid on a retry after it failed.
		"""
		if ACTION_SUBMIT not in actions or not self.sales_order.woocommerce_status:
			return False
		wc_server = frappe.get_cached_doc("WooCommerce Server", self.woocommerce_order.woocommerce_server)
		row = wc_server.get_order_status_row(self.sales_order.woocommerce_status)
		return bool(row and row.get_actions() & ENDING_ACTIONS)

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

	def reopen_sales_order(self, status: str) -> None:
		"""
		Reopen the closed Sales Order of an order that is back in WooCommerce, as ERPNext's Re-open does. It
		is flagged for a person, as an order is closed when it was paid, invoiced or delivered, or refunded.
		"""
		if self.sales_order.docstatus == 1 and self.sales_order.status == "Closed":
			self.sales_order.update_status("Draft")
			self.flag_sync_issue(_("Back in WooCommerce as {0}, so it was reopened").format(status))

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

	def amend_sales_order(self) -> None:
		"""
		Amend the cancelled Sales Order of an order that is back in WooCommerce, and carry on with the
		amendment. The sync only cancels a Sales Order that nothing was paid on or delivered, so the amendment
		is that order again. Like the form's Amend it copies every field, the WooCommerce ids of the lines and
		what was flagged among them.
		"""
		cancelled = self.sales_order
		amendment: SyncedOrder = frappe.copy_doc(cancelled)  # type: ignore
		amendment.amended_from = cancelled.name
		# copy_doc keeps it under tests
		amendment.docstatus = 0
		amendment.flags.ignore_mandatory = True
		amendment.flags.created_by_sync = True
		amendment.insert()
		amendment.submit()
		self.sales_order = amendment
