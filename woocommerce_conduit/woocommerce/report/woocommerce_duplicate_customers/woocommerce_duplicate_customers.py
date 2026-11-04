"""
The Customers that look like the same buyer, by email, tax ID, phone, email domain or name, so that those of
earlier orders can be merged by hand: with Review in the Possible Duplicates of the Customer form, or with
Rename (Merge with existing). Every Customer is read, also one made by hand or by another app, which may be the
same buyer as one the shop's orders made. Pairs a person decided are not the same are left out, and so are the
email domains WooCommerce Settings ignores. It changes nothing.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cint, cstr

from woocommerce_conduit.tasks.customer_duplicates import (
	get_customer_emails,
	get_customer_phones,
	get_decided_pairs,
)
from woocommerce_conduit.tasks.order_customer import GUEST_PREFIX, normalise_tax_id, normalise_text

# The signs of two Customers being one buyer, as the Grouped By filter names them. Email, tax ID and phone are
# to be trusted, a name needs a look at every group.
EMAIL, TAX_ID, PHONE, EMAIL_DOMAIN, NAME = "Email", "Tax ID", "Phone", "Email Domain", "Name"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	customers = get_customers(filters)
	groups = build_groups(customers, filters, get_ignored_email_domains())
	return get_columns(), get_rows(drop_decided_pairs(groups, get_decided_pairs()), customers)


def get_columns() -> list[dict]:
	return [
		{"label": _("Grouped By"), "fieldname": "grouped_by", "fieldtype": "Data", "width": 110},
		{"label": _("Group"), "fieldname": "group", "fieldtype": "Data", "width": 200},
		{
			"label": _("Customer"),
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			"width": 200,
		},
		{"label": _("Customer Name"), "fieldname": "customer_name", "fieldtype": "Data", "width": 180},
		{"label": _("Type"), "fieldname": "customer_type", "fieldtype": "Data", "width": 90},
		{"label": _("Tax ID"), "fieldname": "tax_id", "fieldtype": "Data", "width": 130},
		{"label": _("WooCommerce Identifier"), "fieldname": "identifier", "fieldtype": "Data", "width": 200},
		{"label": _("Guest"), "fieldname": "is_guest", "fieldtype": "Check", "width": 60},
		{
			"label": _("WooCommerce Server"),
			"fieldname": "woocommerce_server",
			"fieldtype": "Link",
			"options": "WooCommerce Server",
			"width": 150,
		},
		{"label": _("Sales Orders"), "fieldname": "orders", "fieldtype": "Int", "width": 110},
		{"label": _("Ordered"), "fieldname": "ordered", "fieldtype": "Currency", "width": 120},
		{"label": _("Created"), "fieldname": "creation", "fieldtype": "Datetime", "width": 150},
	]


def get_grouping_labels() -> dict[str, str]:
	return {
		EMAIL: _("Email"),
		TAX_ID: _("Tax ID"),
		PHONE: _("Phone"),
		EMAIL_DOMAIN: _("Email Domain"),
		NAME: _("Name"),
	}


def get_ignored_email_domains() -> set[str]:
	"""
	The domains that don't tell who a customer is: free mail providers, whose addresses anyone gets. WooCommerce
	Settings lists them, separated by commas or lines, `@` optional.
	"""
	return parse_email_domains(frappe.get_cached_doc("WooCommerce Settings").ignore_email_domains)


def parse_email_domains(listed: str | None) -> set[str]:
	return {
		domain.strip().lower().lstrip("@")
		for domain in cstr(listed).replace("\n", ",").split(",")
		if domain.strip()
	}


def get_customers(filters) -> list[frappe._dict]:
	conditions = {}
	if filters.get("woocommerce_server"):
		conditions["woocommerce_server"] = filters.woocommerce_server

	customers = frappe.get_all(
		"Customer",
		filters=conditions,
		fields=[
			"name",
			"customer_name",
			"customer_type",
			"tax_id",
			"woocommerce_identifier",
			"woocommerce_server",
			"creation",
		],
	)

	# The Sales Orders of each, so the Customer worth keeping is plain to see
	sales_order = frappe.qb.DocType("Sales Order")
	totals = {
		row.customer: row
		for row in (
			frappe.qb.from_(sales_order)
			.select(
				sales_order.customer,
				Count(sales_order.name).as_("orders"),
				Sum(sales_order.base_grand_total).as_("ordered"),
			)
			.where(sales_order.docstatus < 2)
			.groupby(sales_order.customer)
		).run(as_dict=True)
	}
	emails, phones = get_customer_emails(), get_customer_phones()

	for customer in customers:
		row = totals.get(customer.name)
		customer.orders = row.orders if row else 0
		customer.ordered = row.ordered if row else 0
		customer.emails = emails.get(customer.name, set())
		customer.phones = phones.get(customer.name, set())
	return customers


def build_groups(customers: list, filters, ignored_domains: set[str]) -> dict[tuple[str, str], list[str]]:
	"""
	The Customers by each sign of being one buyer in turn, leaving out the email domains that don't tell who a
	customer is. Only groups of more than one are kept, as a group of one is just a customer.
	"""
	wanted = filters.get("grouped_by")
	groups = defaultdict(list)

	def add(grouping: str, key: str, customer):
		if key and (not wanted or grouping == wanted):
			groups[(grouping, key)].append(customer.name)

	for customer in customers:
		for email in sorted(customer.emails):
			add(EMAIL, email, customer)
		add(TAX_ID, normalise_tax_id(customer.tax_id), customer)
		for phone in sorted(customer.phones):
			add(PHONE, phone, customer)
		domains = {email.rsplit("@", 1)[-1] for email in customer.emails if "@" in email}
		for domain in sorted(domains - ignored_domains):
			add(EMAIL_DOMAIN, domain, customer)
		add(NAME, normalise_text(customer.customer_name), customer)

	return {key: names for key, names in groups.items() if len(names) > 1}


def drop_decided_pairs(groups: dict, decided: set[frozenset[str]]) -> dict[tuple[str, str], list[str]]:
	"""
	The groups without the Customers decided not to be the same as any other one of the group, and without the
	groups that leaves fewer than two in
	"""
	kept = {}
	for key, names in groups.items():
		names = [
			name
			for name in names
			if any(frozenset((name, other)) not in decided for other in names if other != name)
		]
		if len(names) > 1:
			kept[key] = names
	return kept


def get_rows(groups: dict, customers: list) -> list[dict]:
	by_name = {customer.name: customer for customer in customers}
	labels = get_grouping_labels()
	rows = []

	# The biggest groups first, and in each the oldest Customer first, which is usually the one to keep and
	# merge the others into
	for (grouping, key), names in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
		for name in sorted(names, key=lambda name: by_name[name].creation):
			customer = by_name[name]
			rows.append(
				{
					"grouped_by": labels[grouping],
					"group": key,
					"customer": customer.name,
					"customer_name": customer.customer_name,
					"customer_type": customer.customer_type,
					"tax_id": customer.tax_id,
					"identifier": customer.woocommerce_identifier,
					"is_guest": cint(cstr(customer.woocommerce_identifier).startswith(GUEST_PREFIX)),
					"woocommerce_server": customer.woocommerce_server,
					"orders": customer.orders,
					"ordered": customer.ordered,
					"creation": customer.creation,
				}
			)
	return rows
