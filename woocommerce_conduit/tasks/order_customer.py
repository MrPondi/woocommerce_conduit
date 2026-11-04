"""
The Customer of a new Sales Order, with the Address and Contact of its order. An order goes to the Customer
with its tax ID, else to a registered customer's by their account, else, for a guest or an order the shop's
staff placed for someone, to the one with its email. Only when there is none is a Customer made for it.

An order never renames the Customer it finds or rewrites its Addresses: ERPNext puts the Customer's name and
tax ID on every Sales Order, invoice and delivery of it, and renders their addresses from the linked Address
whenever they are saved. The order adds the Address and Contact the Customer lacks, its Sales Order links
them, and it is flagged when the Customer bears another name than the order.
"""

import re
from typing import TYPE_CHECKING, Literal

import frappe
from erpnext.selling.doctype.customer.customer import Customer
from frappe import _
from frappe.contacts.doctype.address.address import Address
from frappe.contacts.doctype.contact.contact import Contact
from frappe.utils import cint, cstr

from woocommerce_conduit.tasks.field_map import get_mapped_values
from woocommerce_conduit.woocommerce.doctype.woocommerce_customer_decision.woocommerce_customer_decision import (
	DECISION_MERGED,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import (
	WooCommerceOrder,
	get_address_data,
)
from woocommerce_conduit.woocommerce.woocommerce_api import log_and_raise_error

if TYPE_CHECKING:
	from woocommerce_conduit.tasks.sync_sales_orders import SyncedOrder
	from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
		WooCommerceServer,
	)

# The WordPress roles of a shop's staff, who place orders for other people
STAFF_ROLES = ("administrator", "shop_manager")
# The identifier of a guest's Customer, before the id of the order it was made for
GUEST_PREFIX = "Guest-"
# The identifier of a company's Customer made for its tax ID, when the account's identifier names another
TAX_ID_PREFIX = "TaxID-"
# The fields an address of the order is compared with the Customer's Addresses by
ADDRESS_KEY_FIELDS = ("address_line1", "address_line2", "city", "state", "pincode", "country", "phone")


class WooCustomer(Customer):
	woocommerce_identifier: str
	woocommerce_server: str


def normalise_text(value) -> str:
	"""
	Text as it is compared: regardless of case, and of the spaces around and between its words
	"""
	return " ".join(cstr(value).split()).casefold()


def same_name(name, other_name) -> bool:
	return normalise_text(name) == normalise_text(other_name)


def normalise_tax_id(tax_id) -> str:
	"""
	A tax ID as it is compared: its letters and digits in capitals, without a country prefix. So
	`PL 679-100-38-01` and `6791003801` are the same.
	"""
	key = re.sub(r"[^0-9A-Z]", "", cstr(tax_id).upper())
	return re.sub(r"^[A-Z]{2}(?=\d)", "", key)


def normalise_phone(phone) -> str:
	"""
	A phone number as it is compared: its last 9 digits, so `+48 500 100 200` and `500100200` are the same. A
	number of fewer digits isn't compared.
	"""
	digits = re.sub(r"\D", "", cstr(phone))
	return digits[-9:] if len(digits) >= 9 else ""


def get_merged_customer(woocommerce_server: str, identifier: str) -> str | None:
	"""
	The Customer that the shop's Customer with this identifier was merged into, so its account's orders go there
	rather than to a new one. The decision's link follows any later merge, as Frappe's merge moves every link
	to the Customer it removes.
	"""
	return frappe.db.get_value(
		"WooCommerce Customer Decision",
		{
			"decision": DECISION_MERGED,
			"woocommerce_server": woocommerce_server,
			"woocommerce_identifier": identifier,
		},
		"customer",
		order_by="creation desc",
	)


def get_buyer(billing: dict, tax_id=None) -> frappe._dict:
	"""
	Who placed the order, as its billing address says. `name` is what a Customer made for the order is called:
	the company, else the person, else their email.
	"""
	first_name = cstr(billing.get("first_name")).strip()
	last_name = cstr(billing.get("last_name")).strip()
	company = cstr(billing.get("company")).strip()
	email = cstr(billing.get("email")).strip()
	return frappe._dict(
		first_name=first_name,
		last_name=last_name,
		company=company,
		name=company or f"{first_name} {last_name}".strip() or email,
		email=email,
		phone=cstr(billing.get("phone")).strip(),
		tax_id=cstr(tax_id).strip(),
	)


def get_account_identifier(account: dict | None, buyer) -> str | None:
	"""
	What identifies a registered customer's Customer: their username, or their email when the shop no longer
	has the account (with the company, as one email can buy for several). None without either.
	"""
	if username := cstr((account or {}).get("username")).strip():
		return username
	email = cstr((account or {}).get("email")).strip() or buyer.email
	if not email:
		return None
	return f"{email}-{buyer.company}" if buyer.company else email


def get_order_data(order) -> dict:
	"""
	The order as the WooCommerce Order doctype has it, its JSON fields decoded, for the paths of the Customer
	Field Map to run on: `$.meta_data[?key=='…'].value`
	"""
	data = {df.fieldname: order.get(df.fieldname) for df in frappe.get_meta("WooCommerce Order").fields}
	data["name"] = order.get("name")
	return WooCommerceOrder.deserialize_attributes_of_type_dict_or_list(data)


def find_customer_by_tax_id(woocommerce_server: str, tax_id: str | None) -> str | None:
	"""
	The shop's oldest enabled Customer with this tax ID, however either of them is written
	"""
	key = normalise_tax_id(tax_id)
	if not key:
		return None
	customers = frappe.get_all(
		"Customer",
		filters={"woocommerce_server": woocommerce_server, "disabled": 0, "tax_id": ["is", "set"]},
		fields=["name", "tax_id"],
		order_by="creation asc",
	)
	return next((row.name for row in customers if normalise_tax_id(row.tax_id) == key), None)


def find_customer_by_email(woocommerce_server: str, email: str | None) -> str | None:
	"""
	The shop's oldest enabled Customer with a Contact that has this email, say a guest's who ordered before
	"""
	if not email:
		return None
	contact_email = frappe.qb.DocType("Contact Email")
	link = frappe.qb.DocType("Dynamic Link")
	customer = frappe.qb.DocType("Customer")
	names = (
		frappe.qb.from_(contact_email)
		.join(link)
		.on((link.parenttype == "Contact") & (link.parent == contact_email.parent))
		.join(customer)
		.on((link.link_doctype == "Customer") & (customer.name == link.link_name))
		.select(customer.name)
		.where(contact_email.parenttype == "Contact")
		.where(contact_email.email_id == email)
		.where(customer.woocommerce_server == woocommerce_server)
		.where(customer.disabled == 0)
		.orderby(customer.creation)
		.limit(1)
	).run(pluck=True)
	return names[0] if names else None


def takes_tax_id(customer_name: str, buyer) -> bool:
	"""
	Whether the Customer of an order's account or email is the company of the order's tax ID: it has no tax
	ID yet, and the company's name
	"""
	customer = frappe.db.get_value("Customer", customer_name, ["customer_name", "tax_id"], as_dict=True)
	return bool(customer) and not customer.tax_id and same_name(customer.customer_name, buyer.name)


def has_address(data: dict) -> bool:
	"""
	Whether an address of the order says where, rather than being left empty
	"""
	return any(cstr(data.get(key)).strip() for key in ("address_1", "address_2", "city", "postcode"))


def get_address_title(data: dict) -> str:
	"""
	The name an address of the order gives: its company, else its person
	"""
	person = f"{cstr(data.get('first_name')).strip()} {cstr(data.get('last_name')).strip()}".strip()
	return cstr(data.get("company")).strip() or person


def get_address_key(values) -> tuple[str, ...]:
	return tuple(normalise_text(values.get(fieldname)) for fieldname in ADDRESS_KEY_FIELDS)


def find_address(addresses: list, values: dict) -> str | None:
	"""
	The Address among `addresses` that is the same as `values`
	"""
	key = get_address_key(values)
	return next((address.name for address in addresses if get_address_key(address) == key), None)


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


def get_contact_of_customer(customer_name: str, buyer) -> str | None:
	"""
	The Customer's Contact with the order's email and name, or with its phone and name when there is no email
	"""
	filters = [
		["Dynamic Link", "link_doctype", "=", "Customer"],
		["Dynamic Link", "link_name", "=", customer_name],
	]
	if buyer.email:
		filters.append(["Contact Email", "email_id", "=", buyer.email])
	else:
		filters.append(["Contact Phone", "phone", "=", buyer.phone])
	person = f"{buyer.first_name} {buyer.last_name}"
	contacts = frappe.get_all("Contact", filters=filters, fields=["name", "first_name", "last_name"])
	return next(
		(row.name for row in contacts if same_name(f"{row.first_name or ''} {row.last_name or ''}", person)),
		None,
	)


def create_contact(buyer, billing_address: str, customer: Customer, is_primary_contact=False) -> str:
	contact: Contact = frappe.new_doc("Contact")  # type: ignore
	contact.first_name = buyer.first_name
	contact.last_name = buyer.last_name
	# Frappe takes a primary Contact's mark from the Customer's others
	contact.is_primary_contact = cint(is_primary_contact)
	contact.address = billing_address

	if buyer.phone:
		contact.add_phone(buyer.phone, is_primary_mobile_no=1, is_primary_phone=1)

	if buyer.email:
		contact.add_email(buyer.email, is_primary=1)

	contact.append("links", {"link_doctype": "Customer", "link_name": customer.name})

	contact.flags.ignore_mandatory = True
	contact.insert()
	return contact.name


class SalesOrderCustomerMixin:
	"""
	The Customer of a new Sales Order, and the Address and Contact of its order, which the Sales Order links
	"""

	woocommerce_order: WooCommerceOrder
	customer: WooCustomer
	# What a person should check about the Customer the order went to, flagged on its Sales Order
	customer_issues: list[str]
	billing_address: str | None
	shipping_address: str | None
	contact: str | None

	def create_or_link_customer_and_address(self) -> str | None:
		"""
		Find or make the Customer of the order, and its Address and Contact, which set_customer_in_sales_order
		puts on the Sales Order. The Customer is looked up within the order's shop, as ids and usernames repeat
		across shops:
		- the one with the order's tax ID, which says which company it is
		- else a registered customer's, by their username (or the Customer theirs was merged into); or, for a
		  guest's order or one the shop's staff placed for someone, the one with the order's email
		- else a new one
		A Customer that is found keeps its name and details and only gets what it lacks. Its Sales Order is
		flagged when it bears another name than the order, as the order may be someone else's.
		"""
		self.customer_issues = []
		if not self.woocommerce_order:
			return None

		order = self.woocommerce_order
		billing = get_address_data(order, "billing")
		values = self.get_customer_field_values()
		buyer = get_buyer(billing, values.get("tax_id"))

		identifier, avatar_url = None, None
		if cint(order.customer_id) > 0:
			# None when the shop no longer has the account
			account = self.get_customer_account()
			# The shop's staff place orders for other people, so such an order is the buyer's, as a guest's is
			if not (account and account.get("role") in STAFF_ROLES):
				identifier = get_account_identifier(account, buyer)
				if not identifier:
					frappe.log_error(
						"WooCommerce Error",
						f"Email is required to create or link a customer. \n\nCustomer Data: {account or billing}",
					)
					return None
				avatar_url = (account or {}).get("avatar_url")

		server = order.woocommerce_server
		name = find_customer_by_tax_id(server, buyer.tax_id)
		found_by_tax_id = bool(name)
		if not name:
			if identifier:
				name = frappe.db.get_value(
					"Customer", {"woocommerce_server": server, "woocommerce_identifier": identifier}, "name"
				) or get_merged_customer(server, identifier)
			else:
				name = find_customer_by_email(server, buyer.email)
			# An order with a tax ID is a company's. The Customer of its account or email is that company only
			# while it has no tax ID and the company's name: a person who also buys for their firm gets two.
			if name and buyer.tax_id and not takes_tax_id(name, buyer):
				name = None
				if identifier:
					# The account's identifier names its other Customer
					identifier, avatar_url = TAX_ID_PREFIX + normalise_tax_id(buyer.tax_id), None

		updates = {}
		if name:
			self.customer = frappe.get_doc("Customer", name)  # type: ignore
			# The tax ID says which company it is, there is nothing to check
			if not found_by_tax_id and not same_name(self.customer.customer_name, buyer.name):
				self.customer_issues.append(
					_("Placed as {0}, but its Customer is {1}: check who bought it before invoicing").format(
						buyer.name, self.customer.customer_name
					)
				)
			# What the Customer lacks, from an order that is surely its own
			elif values:
				updates = {
					fieldname: value
					for fieldname, value in values.items()
					if value not in (None, "") and not self.customer.get(fieldname)
				}
		else:
			self.customer = self.create_customer(
				buyer, values, identifier or f"{GUEST_PREFIX}{order.woocommerce_id}", avatar_url
			)

		self.set_order_addresses(billing, get_address_data(order, "shipping"))
		self.set_order_contact(buyer)
		if not self.customer.customer_primary_address:
			updates["customer_primary_address"] = self.billing_address
		if self.contact and not self.customer.customer_primary_contact:
			updates["customer_primary_contact"] = self.contact
		if updates:
			self.update_customer(updates)

		return self.customer.name

	def get_customer_field_values(self) -> dict:
		"""
		What the server's Customer Field Map takes from the order, by Customer field: a tax ID that a plugin
		keeps in the order's meta data, say
		"""
		order = self.woocommerce_order
		wc_server: WooCommerceServer = frappe.get_cached_doc("WooCommerce Server", order.woocommerce_server)  # type: ignore
		field_map = wc_server.get("customer_field_map") or []
		if not field_map:
			return {}
		return get_mapped_values(field_map, get_order_data(order), frappe.get_meta("Customer"), order.name)

	def get_customer_account(self) -> dict | None:
		"""
		The account of the order's customer in the shop. Its username identifies their Customer, and its role
		tells the shop's staff, who order for other people. None when the shop no longer has it.
		"""
		order = self.woocommerce_order
		endpoint = f"customers/{cint(order.customer_id)}"
		response = order.current_wc_api.get(endpoint, params={"_fields": "email,username,avatar_url,role"})
		if response.status_code == 404:
			return None
		if response.status_code != 200:
			log_and_raise_error(error_text=f"Could not read {endpoint}", response=response)
		return response.json()

	def create_customer(self, buyer, values: dict, identifier: str, avatar_url: str | None) -> WooCustomer:
		"""
		A Customer for the order, named and typed as the order gives the buyer, with what the Customer Field Map
		takes from it
		"""
		customer: WooCustomer = frappe.new_doc("Customer")  # type: ignore
		customer.update(values)
		customer.woocommerce_server = self.woocommerce_order.woocommerce_server
		customer.woocommerce_identifier = identifier
		customer.customer_name = buyer.name
		customer.customer_type = "Company" if buyer.company else "Individual"
		if avatar_url:
			customer.image = avatar_url
		customer.flags.ignore_mandatory = True
		customer.insert()
		return customer

	def update_customer(self, updates: dict) -> None:
		"""
		Give the Customer what it lacks. One that can't be saved is logged and left as it was, as the order is
		still worth its Sales Order.
		"""
		customer = self.customer
		# The Addresses and Contact made for it may have changed it in the database
		customer.reload()
		customer.update(updates)
		customer.flags.ignore_mandatory = True
		savepoint = "woocommerce_customer"
		frappe.db.savepoint(savepoint)
		try:
			customer.save()
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			error_message = f"{frappe.get_traceback()}\n\nCustomer Data{customer.as_dict()!s}"
			frappe.log_error("WooCommerce Error", error_message)

	def set_order_addresses(self, billing: dict, shipping: dict) -> None:
		"""
		The Customer's Addresses of the order's billing and shipping address: one it has with the same lines,
		city, state, postcode, country and phone, else a new one. The sync never changes an Address again, so
		the documents of the Customer's other orders keep the address they were made with. A shipping address
		the order leaves empty is the billing one, and one without a phone has the billing phone. A Customer's
		first Addresses are its primary ones.
		"""
		addresses = [
			address
			for address in get_addresses_linking_to(
				"Customer",
				self.customer.name,
				fields=["name", "disabled", "is_primary_address", "is_shipping_address", *ADDRESS_KEY_FIELDS],
			)
			if not address.disabled
		]
		needs_primary = not any(address.is_primary_address for address in addresses)
		needs_shipping = not any(address.is_shipping_address for address in addresses)

		billing_values = self.get_address_values(billing)
		shipping_values = billing_values
		if has_address(shipping):
			shipping_values = self.get_address_values(shipping)
			# Without a phone of its own, it is reached at the billing one's
			shipping_values["phone"] = shipping_values["phone"] or billing_values["phone"]
		same = get_address_key(shipping_values) == get_address_key(billing_values)

		self.billing_address = find_address(addresses, billing_values) or self.create_address(
			billing_values,
			"Billing",
			is_primary_address=needs_primary,
			is_shipping_address=needs_shipping and same,
		)
		if same:
			self.shipping_address = self.billing_address
		else:
			self.shipping_address = find_address(addresses, shipping_values) or self.create_address(
				shipping_values, "Shipping", is_shipping_address=needs_shipping
			)

	def get_address_values(self, data: dict) -> dict:
		"""
		An address of the order as the fields of an Address, titled with the name it gives
		"""
		return {
			"address_title": get_address_title(data) or self.customer.customer_name,
			"address_line1": data.get("address_1"),
			"address_line2": data.get("address_2"),
			"city": data.get("city"),
			"state": data.get("state"),
			"pincode": data.get("postcode"),
			"country": self.get_country(data.get("country")),
			"phone": data.get("phone"),
		}

	def create_address(
		self,
		values: dict,
		address_type: Literal["Billing", "Shipping"],
		is_primary_address=False,
		is_shipping_address=False,
	) -> str:
		address: Address = frappe.new_doc("Address")  # type: ignore
		address.update(values)
		address.address_type = address_type
		address.is_primary_address = cint(is_primary_address)
		address.is_shipping_address = cint(is_shipping_address)
		address.append("links", {"link_doctype": "Customer", "link_name": self.customer.name})
		address.flags.ignore_mandatory = True
		address.insert()
		return address.name

	def get_country(self, code: str | None) -> str | None:
		"""
		The Country of a WooCommerce country code, else the country of the server's company
		"""
		if code and (country := frappe.db.get_value("Country", {"code": code.lower()})):
			return country  # type: ignore
		company = frappe.get_cached_value(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server, "company"
		)
		return frappe.get_cached_value("Company", company, "country") if company else None

	def set_order_contact(self, buyer) -> None:
		"""
		The Customer's Contact with the order's email and name (its phone and name, without an email), else a
		new one. An order with neither email nor phone has none.
		"""
		self.contact = None
		if not (buyer.email or buyer.phone):
			return
		self.contact = get_contact_of_customer(self.customer.name, buyer) or create_contact(
			buyer,
			self.billing_address,
			self.customer,
			is_primary_contact=not self.customer.customer_primary_contact,
		)

	def set_customer_in_sales_order(self, sales_order: SyncedOrder) -> None:
		"""
		The Customer of a new Sales Order, with the Address and Contact of its order. ERPNext would otherwise
		give it the Customer's defaults, which can be another order's.
		"""
		sales_order.customer = self.customer.name
		sales_order.customer_address = self.billing_address
		sales_order.shipping_address_name = self.shipping_address
		if not self.contact:
			return
		# As ERPNext fills them in for a Contact
		contact_fields = frappe.db.get_value(
			"Contact",
			self.contact,
			[
				"name as contact_person",
				"full_name as contact_display",
				"email_id as contact_email",
				"mobile_no as contact_mobile",
				"phone as contact_phone",
			],
			as_dict=True,
		)
		sales_order.update(contact_fields or {})
