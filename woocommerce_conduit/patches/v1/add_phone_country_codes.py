from collections import defaultdict

import frappe
from frappe.query_builder.functions import IfNull

from woocommerce_conduit.tasks.order_customer import get_country_code, international_phone


def execute():
	"""
	The order sync writes phone numbers with their country code now (international_phone), as most of the shop's
	numbers came with it already. The Customers' Addresses and Contacts get theirs, read in their country, else
	the system's: a number of that country without its code gets it, and one the numbering plan doesn't know,
	or with an extension, is left as it is. Documents already made keep the numbers they show.
	"""
	default_region = get_country_code(frappe.db.get_single_value("System Settings", "country"))
	addresses = convert_address_phones(default_region)
	contact_phones = convert_contact_phones(default_region)
	print(f"Phone numbers given their country code: {addresses} of Addresses, {contact_phones} of Contacts")


def convert_address_phones(default_region: str | None) -> int:
	"""
	The phones of the Customers' Addresses, each read in its Address's country
	"""
	address = frappe.qb.DocType("Address")
	link = frappe.qb.DocType("Dynamic Link")
	rows = (
		frappe.qb.from_(address)
		.join(link)
		.on((link.parent == address.name) & (link.parenttype == "Address"))
		.where((link.link_doctype == "Customer") & (IfNull(address.phone, "") != ""))
		.select(address.name, address.phone, address.country)
		.distinct()
	).run(as_dict=True)
	conversions = get_conversions(rows, default_region)
	frappe.db.bulk_update(
		"Address", {name: {"phone": new} for name, (_old, new) in conversions.items()}, update_modified=False
	)
	return len(conversions)


def convert_contact_phones(default_region: str | None) -> int:
	"""
	The phones of the Customers' Contacts, each read in the country of its Contact's Address. A Contact's own
	`phone` and `mobile_no` repeat its primary numbers, and a Customer's `mobile_no` is fetched from its primary
	Contact, so they follow.
	"""
	contact_phone = frappe.qb.DocType("Contact Phone")
	contact = frappe.qb.DocType("Contact")
	link = frappe.qb.DocType("Dynamic Link")
	address = frappe.qb.DocType("Address")
	rows = (
		frappe.qb.from_(contact_phone)
		.join(contact)
		.on(contact.name == contact_phone.parent)
		.join(link)
		.on((link.parent == contact.name) & (link.parenttype == "Contact"))
		.left_join(address)
		.on(address.name == contact.address)
		.where(
			(contact_phone.parenttype == "Contact")
			& (link.link_doctype == "Customer")
			& (IfNull(contact_phone.phone, "") != "")
		)
		.select(contact_phone.name, contact_phone.parent, contact_phone.phone, address.country)
		.distinct()
	).run(as_dict=True)
	conversions = get_conversions(rows, default_region)
	if not conversions:
		return 0
	frappe.db.bulk_update(
		"Contact Phone",
		{name: {"phone": new} for name, (_old, new) in conversions.items()},
		update_modified=False,
	)

	# What each Contact's numbers become
	parents = {row.name: row.parent for row in rows}
	numbers = defaultdict(dict)
	for name, (old, new) in conversions.items():
		numbers[parents[name]][old] = new

	contact_updates = {}
	for row in frappe.get_all(
		"Contact", filters={"name": ["in", list(numbers)]}, fields=["name", "phone", "mobile_no"]
	):
		changes = {
			fieldname: numbers[row.name][row[fieldname]]
			for fieldname in ("phone", "mobile_no")
			if row[fieldname] in numbers[row.name]
		}
		if changes:
			contact_updates[row.name] = changes
	frappe.db.bulk_update("Contact", contact_updates, update_modified=False)

	customer_updates = {}
	for row in frappe.get_all(
		"Customer",
		filters={"customer_primary_contact": ["in", list(numbers)]},
		fields=["name", "customer_primary_contact", "mobile_no"],
	):
		if new := numbers[row.customer_primary_contact].get(row.mobile_no):
			customer_updates[row.name] = {"mobile_no": new}
	frappe.db.bulk_update("Customer", customer_updates, update_modified=False)
	return len(conversions)


def get_conversions(rows: list, default_region: str | None) -> dict[str, tuple[str, str]]:
	"""
	The rows whose phone changes, by name: the phone they had and the one they get
	"""
	conversions = {}
	for row in rows:
		phone = international_phone(row.phone, get_country_code(row.country) or default_region)
		# A stored number that Frappe's check would refuse now is never emptied
		if phone and phone != row.phone:
			conversions[row.name] = (row.phone, phone)
	return conversions
