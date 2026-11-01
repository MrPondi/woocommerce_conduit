from typing import TYPE_CHECKING

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note, make_sales_invoice
from frappe import _
from frappe.utils import flt, get_datetime, getdate

from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	get_mode_of_payment_account,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_COMPLETE,
	INVOICING_ACTIONS,
)

if TYPE_CHECKING:
	from woocommerce_conduit.tasks.sync_sales_orders import SyncedOrder
	from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
	from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
		WooCommerceServer,
	)

# The field the title of the payment method is stored in is this long
PAYMENT_METHOD_LENGTH = 140


def get_order_invoices(sales_order: str) -> list:
	"""
	The Sales Invoices that bill a Sales Order, as ERPNext links their items to it: drafts included and
	cancelled ones not, the newest first
	"""
	return frappe.get_all(
		"Sales Invoice",
		filters=[["Sales Invoice Item", "sales_order", "=", sales_order], ["docstatus", "<", 2]],
		fields=["name", "docstatus", "is_return", "outstanding_amount", "grand_total", "rounded_total"],
		order_by="creation desc",
		distinct=True,
	)


def get_order_payment_entries(sales_order: str, invoices: list[str]) -> list[str]:
	"""
	The Payment Entries that pay a Sales Order or one of its invoices: drafts included and cancelled ones not
	"""
	references = [("Sales Order", [sales_order])]
	if invoices:
		references.append(("Sales Invoice", invoices))

	payment_entries = set()
	for reference_doctype, reference_names in references:
		payment_entries.update(
			frappe.get_all(
				"Payment Entry Reference",
				filters={
					"parenttype": "Payment Entry",
					"reference_doctype": reference_doctype,
					"reference_name": ["in", reference_names],
					"docstatus": ["<", 2],
				},
				pluck="parent",
			)
		)
	return sorted(payment_entries)


def get_payment_method(wc_order) -> str | None:
	"""
	The title of the payment method of an order, or its id when the title doesn't fit
	"""
	title = wc_order.payment_method_title
	if title and len(title) < PAYMENT_METHOD_LENGTH:
		return title
	return wc_order.payment_method or None


class SalesOrderPaymentMixin:
	"""
	The Sales Invoice and Payment Entry of an order that was paid, or will be. Neither waits for a delivery.
	"""

	sales_order: "SyncedOrder"
	woocommerce_order: "WooCommerceOrder"

	def sync_invoice_and_payment(self, action: str) -> None:
		"""
		Make the Sales Invoice when the status of the order says so, and the Payment Entry when it was paid.
		Every step is done once: it is skipped when the Sales Order has its document already, whoever made it
		(ERPNext's own links say so). A step that fails is flagged on the Sales Order and logged, as the order
		itself is still worth keeping.

		A completed order gets its Delivery Note too, whether it was completed in WooCommerce before ERPNext
		saw it or by mistake after, so ERPNext doesn't drift from the shop.
		"""
		sales_order = self.sales_order
		if not sales_order or sales_order.docstatus != 1:
			return

		sales_order.reload()
		# A held order is not paid yet, a closed one is done with. Both pick up again when they are resumed.
		if sales_order.status in ("On Hold", "Closed"):
			return
		sales_order.flags.created_by_sync = True

		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server
		)  # type: ignore
		self.create_invoice_and_payment(wc_server, invoice=action in INVOICING_ACTIONS)
		if action == ACTION_COMPLETE:
			self.run_flagged(self.create_delivery_note, _("The Delivery Note could not be made: {0}"))

	def create_invoice_and_payment(self, wc_server, invoice: bool) -> None:
		if invoice and wc_server.create_sales_invoice:
			self.run_flagged(
				lambda: self.create_sales_invoice(wc_server), _("The Sales Invoice could not be made: {0}")
			)
		if wc_server.enabled_payments_sync:
			self.run_flagged(
				lambda: self.create_payment_entry(wc_server), _("The Payment Entry could not be made: {0}")
			)

	def invoice_and_hold(self) -> None:
		"""
		Invoice the order, and book its payment if it was paid, then hold it. ERPNext doesn't invoice an
		order on hold, so this is done once, when the order gets the status, rather than on every sync.
		"""
		sales_order = self.sales_order
		sales_order.reload()
		sales_order.flags.created_by_sync = True
		if sales_order.status != "Closed":
			wc_server: WooCommerceServer = frappe.get_cached_doc(
				"WooCommerce Server", self.woocommerce_order.woocommerce_server
			)  # type: ignore
			self.create_invoice_and_payment(wc_server, invoice=True)
			sales_order.reload()
			sales_order.flags.created_by_sync = True
		self.hold_sales_order()

	def get_invoices(self) -> list:
		return get_order_invoices(self.sales_order.name)

	def get_payment_entries(self) -> list[str]:
		return get_order_payment_entries(
			self.sales_order.name, [invoice.name for invoice in self.get_invoices()]
		)

	def create_sales_invoice(self, wc_server) -> None:
		sales_order = self.sales_order
		# One invoice per order. An order with an invoice already (made by hand, or a draft), or one that is
		# flagged, is left to a person.
		if sales_order.woocommerce_sync_issue or flt(sales_order.per_billed) > 0 or self.get_invoices():
			return

		invoice = make_sales_invoice(sales_order.name)
		# Delivering is a separate step, with its own Delivery Note
		invoice.update_stock = 0
		invoice.set_posting_time = 1
		invoice.posting_date = sales_order.transaction_date
		invoice.flags.created_by_sync = True
		invoice.insert()
		if wc_server.submit_sales_invoice:
			invoice.submit()

	def create_delivery_note(self) -> None:
		"""
		Deliver an order that was completed in WooCommerce before ERPNext saw it, dated when it was completed
		"""
		sales_order, wc_order = self.sales_order, self.woocommerce_order
		sales_order.reload()
		if flt(sales_order.per_delivered) > 0:
			return

		delivery_note = make_delivery_note(sales_order.name)
		completed = wc_order.get("date_completed")
		delivery_note.set_posting_time = 1
		delivery_note.posting_date = getdate(completed) if completed else sales_order.transaction_date
		if completed:
			delivery_note.posting_time = get_datetime(completed).strftime("%H:%M:%S")
		delivery_note.flags.created_by_sync = True
		delivery_note.insert()
		delivery_note.submit()

	def create_payment_entry(self, wc_server) -> None:
		"""
		Book the payment of the order, when its payment method has a Mode of Payment in the server's Payment
		Method Map and WooCommerce says it was paid. Methods without one, say cash on delivery or a payment
		that only reserves a place, are booked by hand or not at all.
		"""
		sales_order, wc_order = self.sales_order, self.woocommerce_order
		method = wc_server.get_payment_method_row(wc_order.payment_method)
		if not (method and method.mode_of_payment):
			return
		if not wc_order.date_paid or self.get_payment_entries():
			return
		# Nothing was paid, so there is nothing to book
		if flt(sales_order.grand_total) == 0:
			return
		# The Payment Entry is for the Sales Order's total, so one that isn't what was paid is left to a
		# person, like its invoice
		if issue := self.check_totals(sales_order):
			self.flag_sync_issue(issue)
			return

		account = get_mode_of_payment_account(method.mode_of_payment, sales_order.company)
		if not account:
			self.flag_sync_issue(
				_("Mode of Payment {0} has no default account for company {1}").format(
					method.mode_of_payment, sales_order.company
				)
			)
			return

		reference_doctype, reference_name = self.get_payment_reference()
		if not reference_name:
			return
		# Paid in some other way, say after a Payment Entry of the sync was cancelled and made again by hand
		if self.is_paid(reference_doctype, reference_name):
			return

		date_paid = getdate(wc_order.date_paid)
		payment_entry = get_payment_entry(
			reference_doctype, reference_name, bank_account=account, reference_date=date_paid
		)
		payment_entry.posting_date = date_paid
		payment_entry.mode_of_payment = method.mode_of_payment
		payment_entry.reference_no = wc_order.get("transaction_id") or wc_order.payment_method_title
		if method.bank_account:
			payment_entry.bank_account = method.bank_account
		payment_entry.flags.created_by_sync = True
		payment_entry.insert()
		if wc_server.submit_sales_invoice:
			payment_entry.submit()

	def is_paid(self, reference_doctype: str, reference_name: str) -> bool:
		if reference_doctype == "Sales Invoice":
			return flt(frappe.db.get_value("Sales Invoice", reference_name, "outstanding_amount")) <= 0
		return flt(self.sales_order.advance_paid) >= flt(self.sales_order.grand_total)

	def get_payment_reference(self) -> tuple[str, str | None]:
		"""
		What the payment is booked against: the Sales Invoice of the order if there is one (it has to be
		submitted first), otherwise the Sales Order as an advance
		"""
		invoices = [invoice for invoice in self.get_invoices() if not invoice.is_return]
		if not invoices:
			return "Sales Order", self.sales_order.name
		return "Sales Invoice", next((invoice.name for invoice in invoices if invoice.docstatus == 1), None)
