import json
import re
from datetime import datetime
from typing import Literal

import frappe
import frappe.utils
from erpnext.selling.doctype.customer.customer import Customer
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from erpnext.selling.doctype.sales_order_item.sales_order_item import SalesOrderItem
from frappe import _
from frappe.contacts.doctype.address.address import Address
from frappe.contacts.doctype.contact.contact import Contact
from frappe.utils import get_datetime
from frappe.utils.data import now
from frappe.utils.synchronization import filelock

from woocommerce_conduit.exceptions import SyncDisabledError
from woocommerce_conduit.tasks.order_lines import SalesOrderLinesMixin
from woocommerce_conduit.tasks.order_payment import SalesOrderPaymentMixin, get_payment_method
from woocommerce_conduit.tasks.order_status import (
	SalesOrderStatusMixin,
	get_status_to_push,
	get_statuses_to_sync,
)
from woocommerce_conduit.tasks.sync import SynchroniseWooCommerce
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import (
	WooCommerceOrder,
	get_address_data,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_STATUS_ONLY,
	ACTION_SUBMIT_HOLD,
	CREATING_ACTIONS,
	ENDING_ACTIONS,
	normalise_status,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_settings.woocommerce_settings import (
	WooCommerceSettings,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	WooCommerceDocument,
	generate_woocommerce_record_name_from_domain_and_id,
)

# The most orders one sync looks at, fetched 100 per request
MAX_ORDERS_PER_SYNC = 10_000


class SyncedOrderItem(SalesOrderItem):
	woocommerce_id: int


class SyncedOrder(SalesOrder):
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		woocommerce_server: DF.Data
		woocommerce_id: DF.Data
		woocommerce_last_sync_hash: DF.Datetime
		woocommerce_status: DF.Data
		woocommerce_payment_method: DF.Data
		woocommerce_payment_entry: DF.Data
		woocommerce_sales_invoice: DF.Data
		woocommerce_sync_issue: DF.SmallText
		woocommerce_customer_note: DF.Data
		items: DF.Table[SyncedOrderItem]  # pyright: ignore[reportIncompatibleVariableOverride]


class WooCustomer(Customer):
	woocommerce_identifier: str


def enqueue_sales_order_sync(
	woocommerce_order_name: str | None = None, sales_order_name: str | None = None
) -> None:
	"""
	Sync an order in the background. The job gets names and loads the documents itself. A job that is
	still waiting in the queue will pick up everything that changed, so a second one is dropped.
	"""
	frappe.enqueue(
		run_sales_order_sync,
		queue="long",
		timeout=300,
		job_id=f"woocommerce_order_sync::{woocommerce_order_name or sales_order_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		woocommerce_order_name=woocommerce_order_name,
		sales_order_name=sales_order_name,
	)


def run_sales_order_sync_from_hook(doc, method):
	"""
	Intended to be triggered by a Document Controller hook from Sales Order
	"""
	if (
		doc.doctype == "Sales Order"
		and not doc.flags.get("created_by_sync", None)
		and doc.woocommerce_server
		and doc.woocommerce_id
	):
		frappe.msgprint(
			_("Background sync to WooCommerce triggered for {0} {1}").format(frappe.bold(doc.name), method),
			indicator="blue",
			alert=True,
		)
		enqueue_sales_order_sync(sales_order_name=doc.name)


def sync_woocommerce_orders_modified_since(date_time_from=None):
	"""
	Sync the WooCommerce orders modified since date_time_from (by default since the last run)
	"""
	settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	if not date_time_from:
		date_time_from = getattr(settings, "wc_last_sync_date_orders", None)

	# Taken before fetching, so an order that changes while this runs is found by the next run. If
	# WooCommerce can't be reached this raises and the cursor stays where it was.
	sync_started = now()
	for order_name in get_names_of_wc_orders(date_time_from=date_time_from, status=get_statuses_to_sync()):
		enqueue_sales_order_sync(woocommerce_order_name=order_name)

	settings.reload()
	settings.wc_last_sync_date_orders = sync_started
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

	It can be initiated from either the ERPNext side (a Sales Order) or the WooCommerce side (a
	WooCommerce Order). Either is given by name, or as a document of which only the name is used: the
	sync loads current copies of both itself.

	Parameters:
	    sales_order_name: ERPNext Sales Order name to synchronize
	    sales_order: ERPNext Sales Order document to synchronize
	    woocommerce_order_name: Name of WooCommerce Order to synchronize
	    woocommerce_order: WooCommerce Order document to synchronize
	    enqueue: Whether to process synchronization in background

	Returns:
	    tuple: (ERPNext Sales Order, WooCommerce Order) after synchronization, (None, None) when enqueued

	Raises:
	    ValueError: If no valid input parameters are provided
	    ValidationError: If the Sales Order is not linked to a WooCommerce Order
	"""
	woocommerce_order_name = woocommerce_order_name or (woocommerce_order.name if woocommerce_order else None)
	sales_order_name = sales_order_name or (sales_order.name if sales_order else None)

	if not (woocommerce_order_name or sales_order_name):
		raise ValueError(
			"At least one of sales_order_name, sales_order, woocommerce_order_name, woocommerce_order is required"
		)

	if enqueue:
		enqueue_sales_order_sync(
			woocommerce_order_name=woocommerce_order_name,
			sales_order_name=None if woocommerce_order_name else sales_order_name,
		)
		return None, None

	sync = SynchroniseSalesOrder(
		sales_order_name=None if woocommerce_order_name else sales_order_name,
		woocommerce_order_name=woocommerce_order_name,
	)
	sync.run()
	return sync.sales_order, sync.woocommerce_order


class SynchroniseSalesOrder(
	SalesOrderStatusMixin, SalesOrderPaymentMixin, SalesOrderLinesMixin, SynchroniseWooCommerce
):
	"""
	Class for managing synchronisation of a WooCommerce Order with an ERPNext Sales Order
	"""

	def __init__(
		self,
		sales_order_name: str | None = None,
		woocommerce_order_name: str | None = None,
	) -> None:
		super().__init__()
		self.sales_order_name = sales_order_name
		self.woocommerce_order_name = woocommerce_order_name
		self.sales_order: SyncedOrder | None = None
		self.woocommerce_order: WooCommerceOrder | None = None
		self.settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	def get_lock_name(self) -> str:
		"""
		One lock per WooCommerce Order, whichever side the sync starts from
		"""
		record_name = self.woocommerce_order_name
		if not record_name:
			link = frappe.db.get_value(
				"Sales Order", self.sales_order_name, ["woocommerce_server", "woocommerce_id"], as_dict=True
			)
			if not link:
				frappe.throw(_("Sales Order {0} not found").format(self.sales_order_name))
			if not (link.woocommerce_server and link.woocommerce_id):
				frappe.throw(
					_("Sales Order {0} is not linked to a WooCommerce Order").format(self.sales_order_name)
				)
			record_name = generate_woocommerce_record_name_from_domain_and_id(
				link.woocommerce_server, link.woocommerce_id
			)
		return "woocommerce_order_" + re.sub(r"[^\w.~-]", "_", record_name)

	def run(self):
		"""
		Run synchronisation
		"""
		self.run_locked(self.sync_wc_order_with_erpnext_order)

	def push_status(self):
		"""
		Set the WooCommerce Order to the status that its Sales Order has now
		"""
		self.run_locked(self.push_status_to_woocommerce)

	def run_locked(self, sync):
		"""
		Load both orders and sync them. The documents are loaded inside the lock, so the webhook, the hourly
		sync and the Sales Order hook can't all create the Sales Order of the same WooCommerce Order, or
		change it on top of each other.
		"""
		with filelock(self.get_lock_name(), timeout=120):
			try:
				self.load_orders()
				self.get_corresponding_sales_order_or_woocommerce_order()
				sync()
			except Exception:
				error_message = (
					f"{frappe.get_traceback()}\n\n"
					f"Sales Order Data: \n{describe_doc(self.sales_order)}\n\n"
					f"WC Order Data \n{describe_doc(self.woocommerce_order)})"
				)
				frappe.log_error("WooCommerce Error", error_message)
				raise

	def load_orders(self):
		if self.woocommerce_order_name:
			self.woocommerce_order = load_wc_order(self.woocommerce_order_name)
		else:
			self.sales_order = frappe.get_doc("Sales Order", self.sales_order_name)  # type: ignore

	def get_corresponding_sales_order_or_woocommerce_order(self):
		"""
		If we have an ERPNext Sales Order, get the corresponding WooCommerce Order
		If we have a WooCommerce Order, get the corresponding ERPNext Sales Order

		Assumes that both exist, and that the Sales Order is linked to the WooCommerce Order
		"""
		if self.sales_order and not self.woocommerce_order:
			if not self.sales_order.woocommerce_server or not self.sales_order.woocommerce_id:
				frappe.throw(
					_("Sales Order {0} is not linked to a WooCommerce Order").format(self.sales_order.name)
				)

			# Validate that this Sales Order's WooCommerce Server has sync enabled
			wc_server: WooCommerceServer = frappe.get_cached_doc(
				"WooCommerce Server",
				self.sales_order.woocommerce_server,  # type: ignore
			)
			if not wc_server.enabled:
				raise SyncDisabledError(wc_server)

			self.woocommerce_order = load_wc_order(
				generate_woocommerce_record_name_from_domain_and_id(
					self.sales_order.woocommerce_server, self.sales_order.woocommerce_id
				)
			)

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
		if not self.woocommerce_order:
			return

		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server
		)  # type: ignore
		status_row = wc_server.get_order_status_row(self.woocommerce_order.status)
		# A status that the map doesn't know leaves ERPNext as it is
		action = status_row.erpnext_action if status_row else ACTION_STATUS_ONLY

		if not self.sales_order:
			# Only some statuses mean there is an order to keep. For the others nothing is made, and that
			# includes the Customer, Address and Contact that a Sales Order would need.
			if action not in CREATING_ACTIONS:
				return
			self.create_sales_order(action)
		# WooCommerce only wins when it changed since the last pull or push
		elif wc_order_changed(
			self.woocommerce_order.woocommerce_date_modified, self.sales_order.woocommerce_last_sync_hash
		):
			self.update_sales_order(action)

		# Also when nothing changed, as the Sales Order may have been submitted in the mean time
		self.sync_invoice_and_payment(action)

	def push_status_to_woocommerce(self):
		"""
		Set the WooCommerce Order to the status that the server maps the status of its Sales Order to. If
		WooCommerce changed the order in the meantime, say a payment came in, it wins and the order is
		pulled instead. Otherwise that payment would be undone with a stale status.
		"""
		if not (self.sales_order and self.woocommerce_order):
			return

		if wc_order_changed(
			self.woocommerce_order.woocommerce_date_modified, self.sales_order.woocommerce_last_sync_hash
		):
			self.sync_wc_order_with_erpnext_order()
			return

		status = get_status_to_push(self.sales_order)
		if not status:
			return

		if normalise_status(self.woocommerce_order.status) != status:
			self.woocommerce_order.status = status
			self.woocommerce_order.flags.ignore_permissions = True
			self.woocommerce_order.save()

		# The date that WooCommerce gave back for the change is what the next pull compares with
		self.sales_order.db_set(
			{
				"woocommerce_status": status,
				"woocommerce_last_sync_hash": get_datetime(self.woocommerce_order.woocommerce_date_modified),
			},
			update_modified=False,
		)

	def update_sales_order(self, action: str = ACTION_STATUS_ONLY):
		"""
		Update the ERPNext Sales Order with fields from it's corresponding WooCommerce Order
		"""
		if not self.woocommerce_order or not self.sales_order:
			return

		# Ignore cancelled Sales Orders
		if self.sales_order.docstatus != 2:
			# Update the woocommerce_status field if necessary
			wc_order_status = normalise_status(self.woocommerce_order.status)
			status_changed = normalise_status(self.sales_order.woocommerce_status) != wc_order_status
			self.sales_order.woocommerce_status = wc_order_status

			if (
				self.woocommerce_order.customer_note
				and self.sales_order.woocommerce_customer_note != self.woocommerce_order.customer_note
			):
				self.sales_order.woocommerce_customer_note = self.woocommerce_order.customer_note

			payment_method = get_payment_method(self.woocommerce_order)
			if payment_method and self.sales_order.woocommerce_payment_method != payment_method:
				self.sales_order.woocommerce_payment_method = payment_method

			# Always note what was seen, so an unchanged WooCommerce Order isn't looked at again
			self.sales_order.woocommerce_last_sync_hash = self.woocommerce_order.woocommerce_date_modified
			self.sales_order.flags.created_by_sync = True
			self.sales_order.save()

			# The lines of an order that is being cancelled or closed don't matter any more
			if self.sales_order.docstatus == 1 and not (status_changed and action in ENDING_ACTIONS):
				self.run_flagged(self.update_sales_order_items, _("The lines could not be updated: {0}"))
				self.sales_order.reload()

			# What a status means is done once, when it changes. Otherwise editing the note of an order would
			# undo a hold that was put on its Sales Order in ERPNext.
			if status_changed:
				self.apply_status_action(action)

	def create_sales_order(self, action: str) -> None:
		"""
		Create an ERPNext Sales Order from the given WooCommerce Order
		"""
		if not self.woocommerce_order:
			return

		customer_docname = self.create_or_link_customer_and_address()
		if not customer_docname:
			frappe.log_error(
				"WooCommerce Sync",
				f"Failed to create/link customer for WC Order {self.woocommerce_order.woocommerce_id} ({self.woocommerce_order.name}). Aborting Sales Order creation.",
			)
			return
		self.create_missing_items()

		new_sales_order: SyncedOrder = frappe.new_doc("Sales Order")  # type: ignore
		new_sales_order.customer = customer_docname
		new_sales_order.woocommerce_id = self.woocommerce_order.woocommerce_id
		# The number the customer sees, which plugins can make differ from the id
		new_sales_order.po_no = self.woocommerce_order.number or self.woocommerce_order.woocommerce_id
		if self.woocommerce_order.customer_note:
			new_sales_order.woocommerce_customer_note = self.woocommerce_order.customer_note

		new_sales_order.woocommerce_status = normalise_status(self.woocommerce_order.status)
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server
		)  # type: ignore

		new_sales_order.woocommerce_server = self.woocommerce_order.woocommerce_server
		if payment_method := get_payment_method(self.woocommerce_order):
			new_sales_order.woocommerce_payment_method = payment_method
		created_date = str(self.woocommerce_order.woocommerce_date_created).split("T", 1)
		new_sales_order.transaction_date = created_date[0]
		delivery_after = wc_server.delivery_after_days or 7
		new_sales_order.delivery_date = frappe.utils.add_days(created_date[0], delivery_after)
		new_sales_order.company = wc_server.company
		new_sales_order.currency = self.woocommerce_order.currency

		if (
			(wc_server.enabled_shipping_methods)
			and (shipping_lines := json.loads(self.woocommerce_order.shipping_lines))
			and len(wc_server.shipping_rule_map) > 0
		):
			shipping_rule_mapping = next(
				(
					rule
					for rule in wc_server.shipping_rule_map
					if rule.wc_shipping_method_id == shipping_lines[0]["method_id"]
				),
				None,
			)
			# A method without a rule is charged as WooCommerce charged it
			if shipping_rule_mapping:
				new_sales_order.shipping_rule = shipping_rule_mapping.shipping_rule

		self.set_items_in_sales_order(new_sales_order)
		new_sales_order.woocommerce_last_sync_hash = self.woocommerce_order.woocommerce_date_modified
		new_sales_order.flags.ignore_mandatory = True
		new_sales_order.flags.created_by_sync = True
		new_sales_order.insert()

		# Kept, but flagged for a person to look at
		issues = list(self.line_issues)
		if issue := self.check_totals(new_sales_order):
			issues.append(issue)
		if issues:
			new_sales_order.woocommerce_sync_issue = "; ".join(issues)
		new_sales_order.submit()

		new_sales_order.reload()
		new_sales_order.flags.created_by_sync = True

		self.sales_order = new_sales_order
		try:
			if action == ACTION_SUBMIT_HOLD:
				self.hold_sales_order()
		except Exception:
			# The Sales Order is made, so a hold that fails must not undo it
			error_message = f"{frappe.get_traceback()}\n\nSales Order Data{new_sales_order.as_dict()!s}"
			frappe.log_error("WooCommerce Error", error_message)

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
			billing_data = get_address_data(self.woocommerce_order, "billing")
			shipping_data = get_address_data(self.woocommerce_order, "shipping")
			customer_data = billing_data

		first_name = billing_data.get("first_name", "").strip()
		last_name = billing_data.get("last_name", "").strip()
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
		elif "username" in customer_data and customer_data.get("username", "").strip():
			customer_identifier = customer_data.get("username", "").strip()
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
		address.country = frappe.get_value("Country", {"code": raw_data.get("country", "PL").upper()})  # type: ignore
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
		address.country = frappe.get_value("Country", {"code": raw_data.get("country", "PL").upper()})  # type: ignore
		address.state = raw_data.get("state")
		address.pincode = raw_data.get("postcode")
		address.phone = raw_data.get("phone")
		address.address_title = customer.customer_name
		address.is_primary_address = is_primary_address
		address.is_shipping_address = is_shipping_address

		address.flags.ignore_mandatory = True
		address.save()
		return address


def push_order_status(sales_order_name: str) -> None:
	"""
	Background job: set the WooCommerce Order of a Sales Order to the status that the server maps the
	status of the Sales Order to
	"""
	SynchroniseSalesOrder(sales_order_name=sales_order_name).push_status()


def wc_order_changed(woocommerce_date_modified, last_sync_hash) -> bool:
	"""
	Whether WooCommerce changed the order since it was last pulled or pushed. The hash is the
	`date_modified` WooCommerce had then, so both sides come from WooCommerce and the clock of
	ERPNext (or when the Sales Order was last saved) plays no part.
	"""
	if not last_sync_hash:
		return True
	return get_datetime(woocommerce_date_modified) > get_datetime(last_sync_hash)


def load_wc_order(woocommerce_order_name: str) -> WooCommerceOrder:
	"""
	Load the full WooCommerce Order, as the list view only has the fields it asked for
	"""
	wc_order: WooCommerceOrder = frappe.get_doc(
		{"doctype": "WooCommerce Order", "name": woocommerce_order_name}
	)  # type: ignore
	wc_order.load_from_db()

	if not wc_order.woocommerce_server or not wc_order.woocommerce_id:
		raise ValueError(
			f"WooCommerce Order {wc_order.name} is missing required fields: "
			f"server={wc_order.woocommerce_server}, id={wc_order.woocommerce_id}"
		)
	return wc_order


def describe_doc(doc) -> str:
	"""
	A document for an error log, which must not fail on a document that is half loaded
	"""
	if not doc:
		return ""
	try:
		return str(doc.as_dict())
	except Exception:
		return str(doc)


def get_names_of_wc_orders(
	date_time_from: datetime | str | None = None,
	status: list[str] | None = None,
) -> list[str]:
	"""
	The names of the WooCommerce Orders on all enabled servers that were modified after `date_time_from`
	and created after the minimum creation date of the settings, with one of the statuses.

	Raises when WooCommerce can't be reached, so an unreachable shop isn't taken for one without orders.
	"""
	settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	filters = []
	if date_time_from:
		# WooCommerce takes the date without microseconds
		filters.append(
			[
				"WooCommerce Order",
				"date_modified",
				">",
				get_datetime(date_time_from).strftime("%Y-%m-%dT%H:%M:%S"),
			]
		)
	if settings.minimum_creation_date:
		filters.append(["WooCommerce Order", "date_created", ">", settings.minimum_creation_date])
	if status:
		filters.append(["WooCommerce Order", "status", "in", status])

	records = WooCommerceOrder.get_list_of_records(
		{
			"doctype": "WooCommerce Order",
			"filters": filters,
			"fields": ["name"],
			"page_length": MAX_ORDERS_PER_SYNC,
			"max_results": MAX_ORDERS_PER_SYNC,
			"strict": True,
		}
	)
	return [record["name"] for record in records]


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

	# Every order of a customer would otherwise add one more Contact
	if existing := get_contact_of_customer(customer.name, email, phone):
		return frappe.get_doc("Contact", existing)

	contact: Contact = frappe.new_doc("Contact")  # type: ignore
	contact.first_name = data.get("first_name")
	contact.last_name = data.get("last_name")
	contact.is_primary_contact = 1
	contact.address = billing_address.name

	if phone:
		contact.add_phone(phone, is_primary_mobile_no=1, is_primary_phone=1)

	if email:
		contact.add_email(email, is_primary=1)

	contact.append("links", {"link_doctype": "Customer", "link_name": customer.name})

	contact.flags.ignore_mandatory = True
	contact.save()

	return contact


def get_contact_of_customer(customer_name: str, email: str | None, phone: str | None) -> str | None:
	"""
	The Contact of the Customer with this email, or with this phone when there is no email
	"""
	filters = [
		["Dynamic Link", "link_doctype", "=", "Customer"],
		["Dynamic Link", "link_name", "=", customer_name],
	]
	if email:
		filters.append(["Contact Email", "email_id", "=", email])
	else:
		filters.append(["Contact Phone", "phone", "=", phone])
	contacts = frappe.get_all("Contact", filters=filters, pluck="name", limit=1)
	return contacts[0] if contacts else None


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
