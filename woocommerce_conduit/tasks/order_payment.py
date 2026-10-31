from typing import TYPE_CHECKING

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from frappe import _
from frappe.utils import flt, getdate

from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	get_mode_of_payment_account,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_SUBMIT_INVOICE,
)

if TYPE_CHECKING:
	from woocommerce_conduit.tasks.sync_sales_orders import SyncedOrder
	from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
	from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
		WooCommerceServer,
	)

# The field the title of the payment method is stored in is this long
PAYMENT_METHOD_LENGTH = 140

# The fields of a Sales Order that link to the documents the sync made for it
SALES_ORDER_LINK_FIELDS = {
	"Sales Invoice": "woocommerce_sales_invoice",
	"Payment Entry": "woocommerce_payment_entry",
}


def unlink_from_sales_order(doc, method=None):
	"""
	Intended to be triggered by the on_cancel of a Sales Invoice or Payment Entry. The Sales Order that links
	to it no longer has it, and Frappe would refuse the cancel while a submitted Sales Order links to it.
	"""
	fieldname = SALES_ORDER_LINK_FIELDS.get(doc.doctype)
	if not fieldname:
		return
	for sales_order in frappe.get_all("Sales Order", filters={fieldname: doc.name}, pluck="name"):
		frappe.db.set_value("Sales Order", sales_order, fieldname, None, update_modified=False)


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
		Every step is done once: it is skipped when the Sales Order is linked to its document already. A step
		that fails is flagged on the Sales Order and logged, as the order itself is still worth keeping.
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
		if wc_server.create_sales_invoice and action == ACTION_SUBMIT_INVOICE:
			self.run_flagged(
				lambda: self.create_sales_invoice(wc_server), _("The Sales Invoice could not be made: {0}")
			)
		if wc_server.enabled_payments_sync:
			self.run_flagged(
				lambda: self.create_payment_entry(wc_server), _("The Payment Entry could not be made: {0}")
			)

	def create_sales_invoice(self, wc_server) -> None:
		sales_order = self.sales_order
		# One invoice per order. Something billed by hand, or an order that is flagged, is left to a person.
		if sales_order.woocommerce_sales_invoice or flt(sales_order.per_billed) > 0:
			return
		if sales_order.woocommerce_sync_issue:
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

		sales_order.db_set("woocommerce_sales_invoice", invoice.name, update_modified=False)

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
		if not wc_order.date_paid or sales_order.woocommerce_payment_entry:
			return
		# Nothing was paid, so there is nothing to book
		if flt(sales_order.grand_total) == 0:
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

		sales_order.db_set("woocommerce_payment_entry", payment_entry.name, update_modified=False)

	def is_paid(self, reference_doctype: str, reference_name: str) -> bool:
		if reference_doctype == "Sales Invoice":
			return flt(frappe.db.get_value("Sales Invoice", reference_name, "outstanding_amount")) <= 0
		return flt(self.sales_order.advance_paid) >= flt(self.sales_order.grand_total)

	def get_payment_reference(self) -> tuple[str, str | None]:
		"""
		What the payment is booked against: the Sales Invoice of the order if there is one (it has to be
		submitted first), otherwise the Sales Order as an advance
		"""
		sales_order = self.sales_order
		invoice = sales_order.woocommerce_sales_invoice
		if not invoice and flt(sales_order.per_billed) > 0:
			# Invoiced by hand
			invoice = frappe.db.get_value(
				"Sales Invoice Item", {"sales_order": sales_order.name, "docstatus": 1}, "parent"
			)

		if not invoice:
			return "Sales Order", sales_order.name
		if frappe.db.get_value("Sales Invoice", invoice, "docstatus") != 1:
			return "Sales Invoice", None
		return "Sales Invoice", invoice
