"""
The Customers that look like the same buyer, by email, tax ID, email domain or name, so that those of earlier
orders can be merged by hand, with Rename on the Customer form (Merge with existing). It changes nothing.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cint, cstr

from woocommerce_conduit.tasks.order_customer import GUEST_PREFIX, normalise_tax_id, normalise_text

# Providers whose address says nothing about who the customer is, so a shared domain there is no sign of two
# Customers being one buyer: the big ones of the world, and Poland's. The default of the Ignore Email Domains
# filter, which the report's JSON repeats.
DEFAULT_IGNORED_EMAIL_DOMAINS = (
	"gmail.com, googlemail.com, yahoo.com, ymail.com, hotmail.com, outlook.com, live.com, msn.com, "
	"icloud.com, me.com, mac.com, aol.com, protonmail.com, proton.me, pm.me, gmx.com, gmx.net, "
	"zoho.com, mail.com, yandex.com, qq.com, 163.com, wp.pl, o2.pl, onet.pl, op.pl, interia.pl, "
	"interia.eu, onet.eu, poczta.onet.pl, vp.pl, gazeta.pl, tlen.pl, poczta.fm, go2.pl, int.pl"
)

# The signs of two Customers being one buyer, as the Grouped By filter names them. Email and tax ID are to
# be trusted, a name needs a look at every group.
EMAIL, TAX_ID, EMAIL_DOMAIN, NAME = "Email", "Tax ID", "Email Domain", "Name"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	customers = get_customers(filters)
	groups = build_groups(customers, filters)
	return get_columns(), get_rows(groups, customers)


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
	return {EMAIL: _("Email"), TAX_ID: _("Tax ID"), EMAIL_DOMAIN: _("Email Domain"), NAME: _("Name")}


def get_ignored_email_domains(filters) -> set[str]:
	"""
	The domains that don't tell who a customer is. Separated by commas or lines, `@` optional.
	"""
	listed = filters.get("ignore_email_domains")
	if listed is None:
		listed = DEFAULT_IGNORED_EMAIL_DOMAINS
	return {
		domain.strip().lower().lstrip("@")
		for domain in listed.replace("\n", ",").split(",")
		if domain.strip()
	}


def get_customers(filters) -> list[frappe._dict]:
	conditions = {}
	if filters.get("woocommerce_only"):
		conditions["woocommerce_identifier"] = ["is", "set"]
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
	emails = get_customer_emails()

	for customer in customers:
		row = totals.get(customer.name)
		customer.orders = row.orders if row else 0
		customer.ordered = row.ordered if row else 0
		customer.emails = emails.get(customer.name, set())
	return customers


def get_customer_emails() -> dict[str, set[str]]:
	"""
	Every email of each Customer's Contacts, in lower case, by Customer
	"""
	contact_email = frappe.qb.DocType("Contact Email")
	link = frappe.qb.DocType("Dynamic Link")
	rows = (
		frappe.qb.from_(contact_email)
		.join(link)
		.on((link.parenttype == "Contact") & (link.parent == contact_email.parent))
		.select(link.link_name, contact_email.email_id)
		.where(contact_email.parenttype == "Contact")
		.where(link.link_doctype == "Customer")
	).run(as_dict=True)

	emails = defaultdict(set)
	for row in rows:
		if email := cstr(row.email_id).strip().lower():
			emails[row.link_name].add(email)
	return emails


def build_groups(customers: list, filters) -> dict[tuple[str, str], list[str]]:
	"""
	The Customers by each sign of being one buyer in turn. Only groups of more than one are kept, as a group
	of one is just a customer.
	"""
	wanted = filters.get("grouped_by")
	ignored_domains = get_ignored_email_domains(filters)
	groups = defaultdict(list)

	def add(grouping: str, key: str, customer):
		if key and (not wanted or grouping == wanted):
			groups[(grouping, key)].append(customer.name)

	for customer in customers:
		for email in sorted(customer.emails):
			add(EMAIL, email, customer)
		add(TAX_ID, normalise_tax_id(customer.tax_id), customer)
		domains = {email.rsplit("@", 1)[-1] for email in customer.emails if "@" in email}
		for domain in sorted(domains - ignored_domains):
			add(EMAIL_DOMAIN, domain, customer)
		add(NAME, normalise_text(customer.customer_name), customer)

	return {key: names for key, names in groups.items() if len(names) > 1}


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
