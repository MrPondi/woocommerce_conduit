import json
from datetime import datetime
from typing import Literal

import frappe
import frappe.utils
from erpnext.accounts.doctype.sales_taxes_and_charges_template.sales_taxes_and_charges_template import (
	SalesTaxesandChargesTemplate,
)
from erpnext.selling.doctype.customer.customer import Customer
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from erpnext.selling.doctype.sales_order_item.sales_order_item import SalesOrderItem
from erpnext.stock.doctype.item.item import Item
from frappe import ValidationError, _
from frappe.contacts.doctype.address.address import Address
from frappe.contacts.doctype.contact.contact import Contact
from frappe.utils import get_datetime
from frappe.utils.data import cstr, now

from woocommerce_conduit.exceptions import SyncDisabledError
from woocommerce_conduit.tasks.sync import SynchroniseWooCommerce
from woocommerce_conduit.tasks.sync_items import run_item_sync
from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_order.woocommerce_order import (
	WC_ORDER_STATUS_MAPPING,
	WC_ORDER_STATUS_MAPPING_REVERSE,
	WooCommerceOrder,
)
from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_settings.woocommerce_settings import (
	WooCommerceSettings,
)
from woocommerce_conduit.woocommerce_conduit.woocommerce_api import (
	WooCommerceDocument,
	generate_woocommerce_record_name_from_domain_and_id,
)


class SyncedOrderItem(SalesOrderItem):
	woocommerce_id: int


class SyncedOrder(SalesOrder):
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		woocommerce_server: str
		woocommerce_id: str
		woocommerce_last_sync_hash: datetime
		woocommerce_status: str
		woocommerce_payment_method: str
		woocommerce_customer_note: str
		woocommerce_payment_entry: str
		items: DF.Table[SyncedOrderItem]


class WooCustomer(Customer):
	woocommerce_identifier: str


def run_sales_order_sync_from_hook(doc, method):
	"""
	Intended to be triggered by a Document Controller hook from Item
	"""
	if doc.doctype == "Sales Order" and not doc.flags.get("created_by_sync", None) and doc.woocommerce_server:
		frappe.msgprint(
			_("Background sync to WooCommerce triggered for {0} {1}").format(frappe.bold(doc.name), method),
			indicator="blue",
			alert=True,
		)
		frappe.enqueue(run_sales_order_sync, queue="long", sales_order_name=doc.name)


def sync_woocommerce_orders_modified_since(date_time_from=None):
	"""
	Get list of WooCommerce orders modified since date_time_from
	"""
	settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	if not date_time_from:
		date_time_from = getattr(settings, "wc_last_sync_date_orders", None)

	wc_orders = get_list_of_wc_orders(date_time_from=date_time_from, status="pending,processing,on-hold,completed,cancelled")
	for wc_order in wc_orders:
		try:
			run_sales_order_sync(woocommerce_order=wc_order, enqueue=True)
		# Skip orders with errors, as these exceptions will be logged
		except Exception as e:
			frappe.log_error("Order sync error", f"There was an exception when syncing an order: {e!s}")

	settings.reload()
	settings.wc_last_sync_date_orders = now()
	settings.flags.ignore_mandatory = True
	settings.save()


@frappe.whitelist()
def run_sales_order_sync(
	sales_order_name: str | None = None,
	sales_order: SyncedOrder | None = None,
	woocommerce_order_name: str | None = None,
	woocommerce_order: WooCommerceOrder | None = None,
	enqueue=False,
) -> tuple[SyncedOrder | None, WooCommerceOrder | None]:
	"""
	Synchronize an ERPNext Sales Order with its corresponding WooCommerce Order.

	This function serves as a central dispatcher for item synchronization operations.
	It can be initiated from either the ERPNext side (using sales_order_name/sales_order) or
	the WooCommerce side (using woocommerce_order_name/woocommerce_order).

	Parameters:
	    sales_order_name: ERPNext Sales Order name to synchronize
	    sales_order: ERPNext Sales Order document to synchronize
	    woocommerce_order_name: Name of WooCommerce Order to synchronize
	    woocommerce_order: WooCommerce Order document to synchronize
	    enqueue: Whether to process synchronization in background

	Returns:
	    tuple: (ERPNext Sales Order, WooCommerce Order) after synchronization

	Raises:
	    ValueError: If no valid input parameters are provided
	    ValidationError: If no WooCommerce Servers are defined for the order
	"""
	# Initialize sync object reference
	sync = None
	erpnext_order = sales_order  # type: ignore
	wc_order = woocommerce_order

	# Validate inputs, at least one of the parameters should be provided
	if not any([sales_order_name, erpnext_order, woocommerce_order_name, wc_order]):
		raise ValueError(
			"At least one of sales_order_name, sales_order, woocommerce_order_name, woocommerce_order is required"
		)

	# Case 1: Sync initiated from WooCommerce side
	if woocommerce_order_name or wc_order:
		# Check if we need to load the order from the database
		load_full_order = False

		# If only name provided, we need to load the order
		if not wc_order and woocommerce_order_name:
			load_full_order = True
			lookup_name = woocommerce_order_name
		# If order provided but might be a partial/list view order
		elif wc_order:
			lookup_name = wc_order.get("name")
			# Check for essential fields that would only exist in a full order
			# List view orders typically only have name, id, date_created, date_modified, status, number
			# Full orders have many more fields like created_via, customer_id, billing, etc.
			required_full_fields = ["created_via", "customer_id", "billing", "shipping", "line_items"]

			# If any of these fields are missing or None, we need to reload
			if isinstance(wc_order, dict):
				load_full_order = any(not wc_order.get(field) for field in required_full_fields)
			else:
				load_full_order = any(not getattr(wc_order, field, None) for field in required_full_fields)

		if load_full_order:
			try:
				# Load the full order with all fields from the database
				full_wc_order: WooCommerceOrder = frappe.get_doc(
					{"doctype": "WooCommerce Order", "name": lookup_name}
				)  # type: ignore
				full_wc_order.load_from_db()

				# Validate WooCommerce Order has required fields
				if not full_wc_order.woocommerce_server or not full_wc_order.woocommerce_id:
					raise ValueError(
						f"WooCommerce Order {full_wc_order.name} is missing required fields: "
						f"server={full_wc_order.woocommerce_server}, id={full_wc_order.woocommerce_id}\n\nWC ORDER:\n{full_wc_order.as_dict()}"
					)

				wc_order = full_wc_order

			except frappe.DoesNotExistError:
				frappe.throw(_(f"WooCommerce Order {lookup_name} not found"))

		# Initialize synchronization
		sync = SynchroniseSalesOrder(woocommerce_order=wc_order)

		# Execute sync now or in background
		if enqueue:
			frappe.enqueue(sync.run, queue="long", timeout=300, job_name=f"Sync WC Order {wc_order.name}")  # type: ignore
		else:
			sync.run()

	# Case 2: Sync initiated from ERPNext side
	elif sales_order_name or erpnext_order:
		# Load item if only the name was provided
		if not erpnext_order and sales_order_name:
			try:
				erpnext_order: SyncedOrder = frappe.get_doc("Sales Order", sales_order_name)  # type: ignore
			except frappe.DoesNotExistError:
				frappe.throw(_(f"Sales Order {sales_order_name} not found"))

		# Validate item has WooCommerce servers configured
		if not erpnext_order.woocommerce_server:
			frappe.throw(
				_(f"No WooCommerce Server defined for Sales Order {erpnext_order.name or sales_order_name}")
			)

		# Initialize synchronization for this server
		sync = SynchroniseSalesOrder(sales_order=erpnext_order)

		# Execute sync now or in background
		if enqueue:
			frappe.enqueue(
				sync.run,
				queue="long",
				timeout=300,
				job_name=f"Sync Order {erpnext_order.name}",
			)
		else:
			sync.run()

	# Return the synchronize sales order and woocommerce order
	if sync:
		return (
			sync.sales_order if sync.sales_order else None,
			sync.woocommerce_order if sync.woocommerce_order else None,
		)
	return None, None


class SynchroniseSalesOrder(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of a WooCommerce Order with an ERPNext Sales Order
	"""

	def __init__(
		self,
		sales_order: SyncedOrder | None = None,
		woocommerce_order: WooCommerceOrder | None = None,
	) -> None:
		super().__init__()
		self.sales_order = sales_order  # type: ignore
		self.woocommerce_order = woocommerce_order  # type: ignore
		self.settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	def run(self):
		"""
		Run synchronisation
		"""
		try:
			self.get_corresponding_sales_order_or_woocommerce_order()
			self.sync_wc_order_with_erpnext_order()
		except Exception as err:
			try:
				woocommerce_order_dict = (
					self.woocommerce_order.as_dict()
					if isinstance(self.woocommerce_order, WooCommerceOrder)
					else self.woocommerce_order
				)
				sales_order_dict = (
					self.sales_order.as_dict()
					if isinstance(self.sales_order, SyncedOrder)
					else self.sales_order
				)
			except ValidationError:
				woocommerce_order_dict = self.woocommerce_order
				sales_order_dict = self.sales_order
			error_message = f"{frappe.get_traceback()}\n\nSales Order Data: \n{str(sales_order_dict) if self.sales_order else ''}\n\nWC Order Data \n{str(woocommerce_order_dict) if self.woocommerce_order else ''})"
			frappe.log_error("WooCommerce Error", error_message)
			raise err

	def get_corresponding_sales_order_or_woocommerce_order(self):
		"""
		If we have an ERPNext Sales Order, get the corresponding WooCommerce Order
		If we have a WooCommerce Order, get the corresponding ERPNext Sales Order

		Assumes that both exist, and that the Sales Order is linked to the WooCommerce Order
		"""
		if self.sales_order and not self.woocommerce_order and self.sales_order.woocommerce_id:
			# Validate that this Sales Order's WooCommerce Server has sync enabled
			wc_server: WooCommerceServer = frappe.get_cached_doc(
				"WooCommerce Server",
				self.sales_order.woocommerce_server,  # type: ignore
			)
			if not wc_server.enabled:
				raise SyncDisabledError(wc_server)

			wc_orders = get_list_of_wc_orders(sales_order=self.sales_order)

			if not wc_orders:
				raise ValueError(
					f"No WooCommerce Order found with ID {self.sales_order.woocommerce_id} on {self.sales_order.woocommerce_server}"
				)
			self.woocommerce_order: WooCommerceOrder = frappe.get_doc(
				{"doctype": "WooCommerce Order", "name": wc_orders[0]["name"]}  # type: ignore
			)
			self.woocommerce_order.load_from_db()

		if self.woocommerce_order and not self.sales_order:
			self.get_erpnext_sales_order()

	def get_erpnext_sales_order(self):
		"""
		Get erpnext sales order for a WooCommerce Order
		"""
		if not self.woocommerce_order:
			raise ValueError("woocommerce_order required")
		if not all([self.woocommerce_order.woocommerce_server, self.woocommerce_order.woocommerce_id]):
			raise ValueError("Both woocommerce_server and woocommerce_id required")

		filters = [
			["Sales Order", "woocommerce_id", "is", "set"],
			["Sales Order", "woocommerce_server", "is", "set"],
		]
		filters.append(["Sales Order", "woocommerce_id", "=", self.woocommerce_order.woocommerce_id])
		filters.append(
			[
				"Sales Order",
				"woocommerce_server",
				"=",
				self.woocommerce_order.woocommerce_server,
			]
		)

		sales_orders = frappe.get_all(
			"Sales Order",
			filters=filters,
			fields=["name"],
		)
		if sales_orders:
			self.sales_order: SyncedOrder = frappe.get_doc("Sales Order", sales_orders[0].name)  # type: ignore

	def sync_wc_order_with_erpnext_order(self):
		"""
		Syncronise Sales Order between ERPNext and WooCommerce
		"""
		if self.sales_order and not self.woocommerce_order:
			# create missing order in WooCommerce
			pass
		elif self.woocommerce_order and not self.sales_order:
			# create missing order in ERPNext
			self.create_sales_order()
		elif self.sales_order and self.woocommerce_order:
			# both exist, check sync hash
			if (
				self.woocommerce_order.woocommerce_date_modified
				!= self.sales_order.woocommerce_last_sync_hash
			):
				if get_datetime(self.woocommerce_order.woocommerce_date_modified) > get_datetime(
					self.sales_order.modified
				):  # type: ignore
					self.update_sales_order()
				if get_datetime(self.woocommerce_order.woocommerce_date_modified) < get_datetime(
					self.sales_order.modified
				):  # type: ignore
					self.update_woocommerce_order()

			# If the Sales Order exists and has been submitted in the mean time, sync Payment Entries
			if self.sales_order.docstatus == 1 and not self.sales_order.woocommerce_payment_entry:
				self.sales_order.reload()
				if self.create_and_link_payment_entry(self.woocommerce_order, self.sales_order):
					self.sales_order.save()

	def update_sales_order(self):
		"""
		Update the ERPNext Sales Order with fields from it's corresponding WooCommerce Order
		"""
		if not self.woocommerce_order or not self.sales_order:
			return
		# Ignore cancelled Sales Orders
		if self.sales_order.docstatus != 2:
			so_dirty = False

			# Update the woocommerce_status field if necessary
			wc_order_status = WC_ORDER_STATUS_MAPPING_REVERSE[self.woocommerce_order.status]
			if self.sales_order.woocommerce_status != wc_order_status:
				self.sales_order.woocommerce_status = wc_order_status
				so_dirty = True

			if (
				self.woocommerce_order.customer_note
				and self.sales_order.woocommerce_customer_note != self.woocommerce_order.customer_note
			):
				self.sales_order.woocommerce_customer_note = self.woocommerce_order.customer_note
				so_dirty = True

			# Update the payment_method_title field if necessary, use the payment method ID
			# if the title field is too long
			payment_method = None

			if (
				self.woocommerce_order.payment_method_title
				and len(self.woocommerce_order.payment_method_title) < 140
			):
				payment_method = self.woocommerce_order.payment_method_title
			elif self.woocommerce_order.payment_method:
				payment_method = self.woocommerce_order.payment_method

			if payment_method and self.sales_order.woocommerce_payment_method != payment_method:
				self.sales_order.woocommerce_payment_method = payment_method
				so_dirty = True

			if not self.sales_order.woocommerce_payment_entry:
				if self.create_and_link_payment_entry(self.woocommerce_order, self.sales_order):
					so_dirty = True

			if so_dirty:
				self.sales_order.flags.created_by_sync = True
				self.sales_order.save()

	def create_and_link_payment_entry(self, wc_order: WooCommerceOrder, sales_order: SyncedOrder) -> bool:
		"""
		Create a Payment Entry for a WooCommerce Order that has been marked as Paid
		"""
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", wc_order.woocommerce_server
		)  # type: ignore
		if not wc_server:
			raise ValueError("Could not find woocommerce_server in list of servers")

		# Validate that WooCommerce order has been paid, and that sales order doesn't have a linked Payment Entry yet
		if (
			wc_server.enabled_payments_sync
			and wc_order.payment_method
			and wc_order.date_paid
			and not sales_order.woocommerce_payment_entry
			and sales_order.docstatus == 1
		):
			# If the grand total is 0, skip payment entry creation
			if sales_order.grand_total is None or float(sales_order.grand_total) == 0:
				return True

			# Get Company Bank Account for this Payment Method
			payment_method_bank_account_mapping = json.loads(wc_server.payment_method_bank_account_mapping)

			if wc_order.payment_method not in payment_method_bank_account_mapping:
				raise KeyError(
					f"WooCommerce payment method {wc_order.payment_method} not found in WooCommerce Server"
				)

			company_bank_account = payment_method_bank_account_mapping[wc_order.payment_method]

			if company_bank_account:
				# Get G/L Account for this Payment Method
				payment_method_gl_account_mapping = json.loads(wc_server.payment_method_gl_account_mapping)
				company_gl_account = payment_method_gl_account_mapping[wc_order.payment_method]

				# Create a new Payment Entry
				company = frappe.get_value("Account", company_gl_account, "company")

				# Attempt to get Payfast Transaction ID
				payment_reference_no = wc_order.get("transaction_id", None)

				# Determine if the reference should be Sales Order or Sales Invoice
				reference_doctype = "Sales Order"
				reference_name = sales_order.name
				total_amount = sales_order.grand_total
				if sales_order.per_billed > 0:
					si_item_details = frappe.get_all(
						"Sales Invoice Item",
						fields=["name", "parent"],
						filters={"sales_order": sales_order.name},
					)
					if len(si_item_details) > 0:
						reference_doctype = "Sales Invoice"
						reference_name = si_item_details[0].parent
						total_amount = sales_order.grand_total

				# Create Payment Entry
				payment_entry_dict = {
					"company": company,
					"payment_type": "Receive",
					"reference_no": payment_reference_no or wc_order.payment_method_title,
					"reference_date": wc_order.date_paid,
					"party_type": "Customer",
					"party": sales_order.customer,
					"posting_date": wc_order.date_paid or sales_order.transaction_date,
					"paid_amount": float(wc_order.total),
					"received_amount": float(wc_order.total),
					"bank_account": company_bank_account,
					"paid_to": company_gl_account,
				}
				payment_entry = frappe.new_doc("Payment Entry")
				payment_entry.update(payment_entry_dict)
				row = payment_entry.append("references")
				row.reference_doctype = reference_doctype
				row.reference_name = reference_name
				row.total_amount = total_amount
				row.allocated_amount = total_amount
				payment_entry.save()

				# Link created Payment Entry to Sales Order
				sales_order.woocommerce_payment_entry = payment_entry.name  # type: ignore
			return True
		return False

	def update_woocommerce_order(self) -> None:
		"""
		Update the WooCommerce Order with fields from it's corresponding ERPNext Sales Order
		"""
		if not self.woocommerce_order or not self.sales_order:
			return

		wc_order_dirty = False

		# Update the woocommerce_status field if necessary
		sales_order_wc_status = (
			WC_ORDER_STATUS_MAPPING[self.sales_order.woocommerce_status]
			if self.sales_order.woocommerce_status
			else None
		)
		if sales_order_wc_status and sales_order_wc_status != self.woocommerce_order.status:
			self.woocommerce_order.status = sales_order_wc_status  # type: ignore
			wc_order_dirty = True

		# Get the Item WooCommerce ID's
		for so_item in self.sales_order.items:
			so_item.woocommerce_id = frappe.get_value(
				"Item WooCommerce Server",
				filters={
					"parent": so_item.item_code,
					"woocommerce_server": self.woocommerce_order.woocommerce_server,
				},
				fieldname="woocommerce_id",
			)  # type: ignore

		# Update the line_items field if necessary
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server
		)  # type: ignore
		if wc_server.sync_so_items_to_wc:
			sales_order_items_changed = False
			line_items = json.loads(self.woocommerce_order.line_items)
			# Check if count of line items are different
			if len(line_items) != len(self.sales_order.items):
				sales_order_items_changed = True
			# Check if any line item properties changed
			else:
				for i, so_item in enumerate(self.sales_order.items):
					if not so_item.woocommerce_id:
						break
					elif (
						int(so_item.woocommerce_id) != line_items[i]["product_id"]
						or so_item.qty != line_items[i]["quantity"]
						or so_item.rate != get_tax_inc_price_for_woocommerce_line_item(line_items[i])
					):
						sales_order_items_changed = True
						break

			if sales_order_items_changed:
				# Set the product_id for existing lines to null, to clear the line items for the WooCommerce order
				replacement_line_items = [
					{"id": line_item["id"], "product_id": None}
					for line_item in json.loads(self.woocommerce_order.line_items)
				]
				# Add the correct lines
				replacement_line_items.extend(
					[
						{"product_id": so_item.woocommerce_id, "quantity": so_item.qty, "price": so_item.rate}
						for so_item in self.sales_order.items
					]
				)
				self.woocommerce_order.line_items = json.dumps(replacement_line_items)
				wc_order_dirty = True

		if wc_order_dirty:
			self.woocommerce_order.save()

	def create_sales_order(self) -> None:
		"""
		Create an ERPNext Sales Order from the given WooCommerce Order
		"""
		if not self.woocommerce_order:
			return

		customer_docname = self.create_or_link_customer_and_address()
		if not customer_docname:
			return
		self.create_missing_items()

		new_sales_order: SyncedOrder = frappe.new_doc("Sales Order")  # type: ignore
		new_sales_order.customer = customer_docname
		new_sales_order.po_no = new_sales_order.woocommerce_id = self.woocommerce_order.woocommerce_id
		if self.woocommerce_order.customer_note:
			new_sales_order.woocommerce_customer_note = self.woocommerce_order.customer_note

		new_sales_order.woocommerce_status = WC_ORDER_STATUS_MAPPING_REVERSE[self.woocommerce_order.status]
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server
		)  # type: ignore

		new_sales_order.woocommerce_server = self.woocommerce_order.woocommerce_server
		# Set the payment_method_title field if necessary, use the payment method ID if the title field is too long
		payment_method = None

		if (
			self.woocommerce_order.payment_method_title
			and len(self.woocommerce_order.payment_method_title) < 140
		):
			payment_method = self.woocommerce_order.payment_method_title
		elif self.woocommerce_order.payment_method:
			payment_method = self.woocommerce_order.payment_method

		if payment_method:
			new_sales_order.woocommerce_payment_method = payment_method
		created_date = str(self.woocommerce_order.woocommerce_date_created).split("T", 1)
		new_sales_order.transaction_date = created_date[0]
		if True or wc_server.delivery_after_days:
			delivery_after = wc_server.delivery_after_days or 7
			new_sales_order.delivery_date = frappe.utils.add_days(created_date[0], delivery_after)
		new_sales_order.company = wc_server.company
		new_sales_order.currency = self.woocommerce_order.currency

		if (
			(wc_server.enabled_shipping_methods)
			and (shipping_lines := json.loads(self.woocommerce_order.shipping_lines))
			and len(wc_server.shipping_rule_map) > 0
		):
			if self.woocommerce_order.shipping_lines != {}:
				shipping_rule_mapping = next(
					(
						rule
						for rule in wc_server.shipping_rule_map
						if rule.wc_shipping_method_id == shipping_lines[0]["method_id"]
					),
				)
				new_sales_order.shipping_rule = shipping_rule_mapping.shipping_rule

		self.set_items_in_sales_order(new_sales_order)
		new_sales_order.flags.ignore_mandatory = True
		new_sales_order.flags.created_by_sync = True
		new_sales_order.insert()
		if wc_server.submit_sales_orders:
			new_sales_order.submit()

		new_sales_order.reload()
		self.create_and_link_payment_entry(self.woocommerce_order, new_sales_order)

		try:
			new_sales_order.save()
		except Exception:
			error_message = f"{frappe.get_traceback()}\n\nSales Order Data{new_sales_order.as_dict()!s}"
			frappe.log_error("WooCommerce Error", error_message)
		finally:
			self.sales_order = new_sales_order

	def create_or_link_customer_and_address(self) -> str | None:
		"""
		Create or update Customer and Address records, with special handling for guest orders using order ID.
		"""
		if not self.woocommerce_order:
			return

		# Determine if the order is from a guest user
		is_customer = (
			self.woocommerce_order.customer_id not in [None, 0]
			and "customer" in self.woocommerce_order._links
		)

		if is_customer:
			customer_data = WooCommerceDocument.get_api_response(
				self.woocommerce_order.woocommerce_server,
				json.loads(self.woocommerce_order._links)["customer"][0]["href"].split("/wp-json/wc/v3/", 1)[
					1
				],
			)
			billing_data = customer_data["billing"]
			shipping_data = customer_data["shipping"]
		else:
			billing_data = json.loads(self.woocommerce_order.billing)
			shipping_data = json.loads(self.woocommerce_order.shipping)
			customer_data = billing_data

		first_name = customer_data.get("first_name", "").strip()
		last_name = customer_data.get("last_name", "").strip()
		company_name = billing_data.get("company", "").strip()
		email = customer_data.get("email", "").strip()
		individual_name = f"{first_name} {last_name}".strip()

		if not email and is_customer:
			# Log raw_billing_data
			frappe.log_error(
				"WooCommerce Error",
				f"Email is required to create or link a customer. \n\nCustomer Data: {customer_data}",
			)
			return None

		# Use order ID for guest users, otherwise use email
		if not is_customer:
			customer_identifier = f"Guest-{self.woocommerce_order.woocommerce_id}"
		elif company_name:
			customer_identifier = f"{email}-{company_name}"
		else:
			customer_identifier = email

		# Check if customer exists using the identifier
		existing_customer = frappe.get_value(
			"Customer", {"woocommerce_identifier": customer_identifier}, "name"
		)

		if not existing_customer:
			# Create Customer
			customer: WooCustomer = frappe.new_doc("Customer")  # type: ignore
			customer.woocommerce_identifier = customer_identifier
			customer.customer_type = "Company" if company_name else "Individual"
			if is_customer:
				customer.image = customer_data["avatar_url"]
		else:
			# Edit Customer
			customer: WooCustomer = frappe.get_doc("Customer", existing_customer)  # type: ignore

		customer.customer_name = company_name if company_name else individual_name
		customer.woocommerce_identifier = customer_identifier

		# Check if vat_id exists in billing_data and is a valid string
		vat_id = billing_data.get("vat_id", None)

		if isinstance(vat_id, str) and vat_id.strip():
			customer.tax_id = vat_id

		customer.flags.ignore_mandatory = True

		try:
			customer.save()
		except Exception:
			error_message = f"{frappe.get_traceback()}\n\nCustomer Data{customer.as_dict()!s}"
			frappe.log_error("WooCommerce Error", error_message)
		finally:
			self.customer = customer

		billing_address = self.create_or_update_address(billing_data, shipping_data)
		self.customer.customer_primary_address = billing_address.name
		contact = create_contact(
			customer_data,
			billing_address,
			self.customer,
		)
		self.customer.reload()
		if contact:
			self.customer.customer_primary_contact = contact.name
		try:
			self.customer.save()
		except Exception:
			error_message = f"{frappe.get_traceback()}\n\nCustomer Data{customer.as_dict()!s}"
			frappe.log_error("WooCommerce Error", error_message)

		return customer.name

	def create_missing_items(self):
		"""
		Searching for items linked to multiple WooCommerce sites
		"""
		for item_data in json.loads(self.woocommerce_order.line_items):
			item_woo_com_id = cstr(item_data.get("variation_id") or item_data.get("product_id"))

			# Deleted items will have a "0" for variation_id/product_id
			if item_woo_com_id != "0":
				woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
					self.woocommerce_order.woocommerce_server, item_woo_com_id
				)
				run_item_sync(woocommerce_product_name=woocommerce_product_name)

	def set_items_in_sales_order(self, new_sales_order: SyncedOrder):
		"""
		Customised version of set_items_in_sales_order to allow searching for items linked to
		multiple WooCommerce sites
		"""
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", new_sales_order.woocommerce_server
		)  # type: ignore
		if not wc_server.warehouse:
			frappe.throw(_("Please set Warehouse in WooCommerce Server"))

		for item in json.loads(self.woocommerce_order.line_items):
			woocomm_item_id = item.get("variation_id") or item.get("product_id")

			# Deleted items will have a "0" for variation_id/product_id
			if woocomm_item_id == 0:
				found_item = create_placeholder_item(new_sales_order)
			else:
				iws = frappe.qb.DocType("Item WooCommerce Server")
				itm = frappe.qb.DocType("Item")
				item_codes = (
					frappe.qb.from_(iws)
					.join(itm)
					.on(iws.parent == itm.name)
					.where(
						(iws.woocommerce_id == cstr(woocomm_item_id))
						& (iws.woocommerce_server == new_sales_order.woocommerce_server)
						& (itm.disabled == 0)
					)
					.select(iws.parent)
					.limit(1)
				).run(as_dict=True)

				found_item: Item | None = frappe.get_doc("Item", item_codes[0].parent) if item_codes else None  # type: ignore

			if found_item:
				# If we are applying a Sales Taxes and Charges Template (as opposed to Actual Tax), then we need to
				# determine if the item price should include tax or not
				if not wc_server.use_actual_tax_type:
					tax_template: SalesTaxesandChargesTemplate = frappe.get_cached_doc(
						"Sales Taxes and Charges Template", wc_server.sales_taxes_and_charges_template
					)  # type: ignore

				item = {
					"item_code": found_item.name,
					"item_name": found_item.item_name,
					"description": found_item.item_name,
					"qty": item.get("quantity"),
					"rate": item.get("price")
					if wc_server.use_actual_tax_type or not tax_template.taxes[0].included_in_print_rate
					else get_tax_inc_price_for_woocommerce_line_item(item),
					"warehouse": wc_server.warehouse,
					"discount_percentage": 100 if item.get("price") == 0 else 0,
				}
				if new_sales_order.delivery_date:
					item["delivery_date"] = new_sales_order.delivery_date
				wc_server.sales_taxes_and_charges_template
				new_sales_order.append("items", item)

				if not wc_server.use_actual_tax_type:
					new_sales_order.taxes_and_charges = wc_server.sales_taxes_and_charges_template

					# Trigger taxes calculation
					new_sales_order.set_missing_lead_customer_details()
				else:
					ordered_items_tax = item.get("total_tax")
					add_tax_details(
						new_sales_order, ordered_items_tax, "Ordered Item tax", wc_server.tax_account
					)

		# If a Shipping Rule is added, shipping charges will be determined by the Shipping Rule. If not, then
		# get it from the WooCommerce Order
		if not new_sales_order.shipping_rule:
			if self.woocommerce_order.shipping_total != 0:
				add_tax_details(
					new_sales_order,
					self.woocommerce_order.shipping_tax,
					"Shipping Tax",
					wc_server.freight_and_forwarding_account,
				)
			add_tax_details(
				new_sales_order,
				self.woocommerce_order.shipping_total,
				"Shipping Total",
				wc_server.freight_and_forwarding_account,
			)

		# Handle scenario where Woo Order has no items, then manually set the total
		if len(new_sales_order.items) == 0:
			new_sales_order.base_grand_total = float(self.woocommerce_order.total)
			new_sales_order.grand_total = float(self.woocommerce_order.total)
			new_sales_order.base_rounded_total = float(self.woocommerce_order.total)
			new_sales_order.rounded_total = float(self.woocommerce_order.total)

	def create_or_update_address(self, billing: dict, shipping: dict) -> Address:
		"""
		If the address(es) exist, update it, else create it
		"""
		addresses = get_addresses_linking_to(
			"Customer", self.customer.name, fields=["name", "is_primary_address", "is_shipping_address"]
		)

		existing_billing_address = next((addr for addr in addresses if addr.is_primary_address == 1), None)
		existing_shipping_address = next((addr for addr in addresses if addr.is_shipping_address == 1), None)

		address_keys_to_compare = [
			"first_name",
			"last_name",
			"company",
			"address_1",
			"address_2",
			"city",
			"postcode",
			"country",
			"state",
			"phone",
		]
		address_keys_same = [
			True if billing[key] == shipping[key] else False for key in address_keys_to_compare
		]

		if all(address_keys_same):
			# Use one address for both billing and shipping
			address = existing_billing_address or existing_shipping_address
			if address:
				billing_address = self.update_address(
					address.name, billing, self.customer, is_primary_address=1, is_shipping_address=1
				)
			else:
				billing_address = self.create_address(
					billing, self.customer, "Billing", is_primary_address=1, is_shipping_address=1
				)
		else:
			# Handle billing address
			if existing_billing_address:
				billing_address = self.update_address(
					existing_billing_address.name,
					billing,
					self.customer,
					is_primary_address=1,
					is_shipping_address=0,
				)
			else:
				billing_address = self.create_address(
					billing, self.customer, "Billing", is_primary_address=1, is_shipping_address=0
				)

			# Handle shipping address
			if existing_shipping_address:
				self.update_address(
					existing_shipping_address.name,
					shipping,
					self.customer,
					is_primary_address=0,
					is_shipping_address=1,
				)
			else:
				self.create_address(
					shipping, self.customer, "Shipping", is_primary_address=0, is_shipping_address=1
				)

		return billing_address

	def create_address(
		self,
		raw_data: dict,
		customer: Customer,
		address_type: Literal[
			"Billing",
			"Shipping",
			"Office",
			"Personal",
			"Plant",
			"Postal",
			"Shop",
			"Subsidiary",
			"Warehouse",
			"Current",
			"Permanent",
			"Other",
		],
		is_primary_address=0,
		is_shipping_address=0,
	):
		address: Address = frappe.new_doc("Address")  # type: ignore

		address.address_type = address_type
		address.address_line1 = raw_data.get("address_1", "Not Provided")
		address.address_line2 = raw_data.get("address_2", "Not Provided")
		address.city = raw_data.get("city", "Not Provided")
		address.country = frappe.get_value("Country", {"code": raw_data.get("country", "IN").lower()})  # type: ignore
		address.state = raw_data.get("state")
		address.pincode = raw_data.get("postcode")
		address.phone = raw_data.get("phone")
		address.address_title = customer.customer_name
		address.is_primary_address = is_primary_address
		address.is_shipping_address = is_shipping_address
		address.append("links", {"link_doctype": "Customer", "link_name": customer.name})

		address.flags.ignore_mandatory = True
		address.save()
		return address

	def update_address(
		self,
		address_name: str,
		raw_data: dict,
		customer: Customer,
		is_primary_address=0,
		is_shipping_address=0,
	):
		address: Address = frappe.get_doc("Address", address_name)  # type: ignore

		address.address_line1 = raw_data.get("address_1", "Not Provided")
		address.address_line2 = raw_data.get("address_2", "Not Provided")
		address.city = raw_data.get("city", "Not Provided")
		address.country = frappe.get_value("Country", {"code": raw_data.get("country", "IN").lower()})  # type: ignore
		address.state = raw_data.get("state")
		address.pincode = raw_data.get("postcode")
		address.phone = raw_data.get("phone")
		address.address_title = customer.customer_name
		address.is_primary_address = is_primary_address
		address.is_shipping_address = is_shipping_address

		address.flags.ignore_mandatory = True
		address.save()
		return address


def get_list_of_wc_orders(
	sales_order: SyncedOrder | None = None,
	date_time_from: datetime | None = None,
	status: str | None = None,
) -> list[WooCommerceOrder]:
	"""
	Fetches a list of WooCommerce Orders within a specified date range or linked with a Sales Order.

	This function efficiently retrieves WooCommerce orders using the built-in pagination
	and caching mechanisms of the WooCommerceOrder class.

	Args:
		sales_order: Optional ERPNext order to sync with WooCommerce
		date_time_from: Optional datetime to filter orders modified after this time

	Returns:
		List of WooCommerceProduct documents
	"""
	# Build filters
	filters = []
	servers = None

	settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore
	minimum_creation_date = settings.minimum_creation_date

	# Build filters
	if date_time_from:
		filters.append(["WooCommerce Order", "date_modified", ">", date_time_from])
	if minimum_creation_date:
		filters.append(["WooCommerce Order", "date_created", ">", minimum_creation_date])
	if status:
		filters.append(["WooCommerce Order", "status", "=", status])
	if sales_order:
		if not hasattr(sales_order, "woocommerce_id") or not sales_order.woocommerce_id:
			frappe.log_error(
				"WooCommerce Sync Error", f"Item {sales_order.name} has no WooCommerce server configuration"
			)
			return []

		filters.append(["WooCommerce Order", "id", "=", sales_order.woocommerce_id])
		servers = [sales_order.woocommerce_server]

	try:
		# Leverage the WooCommerceProduct.get_list method which already has caching
		woocommerce_product: WooCommerceOrder = frappe.get_doc({"doctype": "WooCommerce Order"})  # type: ignore

		# Use a single call with appropriate arguments
		wc_products = woocommerce_product.get_list(
			args={
				"doctype": "WooCommerce Order",
				"filters": filters,
				"servers": servers,
				"as_doc": True,
				# Let the API handle pagination efficiently
				# Set a reasonable limit for maximum records
				"page_length": 1 if sales_order else 1000,
			}
		)

		return wc_products or []  # type: ignore

	except Exception as e:
		frappe.log_error(
			"WooCommerce Sync Error", f"Error fetching WooCommerce orders: {e!s}\n{frappe.get_traceback()}"
		)
		return []


def rename_address(address, customer):
	old_address_title = address.name
	new_address_title = customer.name + "-" + address.address_type
	address.address_title = customer.customer_name
	address.save()

	frappe.rename_doc("Address", old_address_title, new_address_title)


def create_contact(data, billing_address, customer):
	email = data.get("email", None)
	phone = data.get("phone", None)

	if not email and not phone:
		return

	contact: Contact = frappe.new_doc("Contact")  # type: ignore
	contact.first_name = data.get("first_name")
	contact.last_name = data.get("last_name")
	contact.is_primary_contact = 1
	contact.is_billing_contact = 1  # type: ignore
	contact.address = billing_address

	if phone:
		contact.add_phone(phone, is_primary_mobile_no=1, is_primary_phone=1)

	if email:
		contact.add_email(email, is_primary=1)

	contact.append("links", {"link_doctype": "Customer", "link_name": customer.name})

	contact.flags.ignore_mandatory = True
	contact.save()

	return contact


def add_tax_details(sales_order, price, desc, tax_account_head):
	sales_order.append(
		"taxes",
		{
			"charge_type": "Actual",
			"account_head": tax_account_head,
			"tax_amount": price,
			"description": desc,
		},
	)


def get_tax_inc_price_for_woocommerce_line_item(line_item: dict):
	"""
	WooCommerce's Line Item "price" field will always show the tax excluding amount.
	This function calculates the tax inclusive rate for an item
	"""
	return (float(line_item.get("subtotal")) + float(line_item.get("subtotal_tax"))) / float(  # type: ignore
		line_item.get("quantity")  # type: ignore
	)


def create_placeholder_item(sales_order: SyncedOrder):
	"""
	Create a placeholder Item for deleted WooCommerce Products
	"""
	wc_server: WooCommerceServer = frappe.get_cached_doc("WooCommerce Server", sales_order.woocommerce_server)  # type: ignore
	if not frappe.db.exists("Item", "DELETED_WOOCOMMERCE_PRODUCT"):
		item: Item = frappe.new_doc("Item")  # type: ignore
		item.item_code = "DELETED_WOOCOMMERCE_PRODUCT"
		item.item_name = "Deletet WooCommerce Product"
		item.description = "Deletet WooCommerce Product"
		item.item_group = "All Item Groups"
		item.stock_uom = wc_server.uom
		item.is_stock_item = 0
		item.is_fixed_asset = 0
		item.opening_stock = 0
		item.flags.created_by_sync = True
		item.save()
	else:
		item: Item = frappe.get_doc("Item", "DELETED_WOOCOMMERCE_PRODUCT")  # type: ignore
	return item


def get_addresses_linking_to(doctype, docname, fields=None):
	"""Return a list of Addresses containing a link to the given document."""
	return frappe.get_all(
		"Address",
		fields=fields,
		filters=[
			["Dynamic Link", "link_doctype", "=", doctype],
			["Dynamic Link", "link_name", "=", docname],
		],
	)
