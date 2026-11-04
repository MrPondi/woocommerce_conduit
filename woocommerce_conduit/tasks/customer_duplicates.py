"""
Customers that may be the same buyer, which a person decides about on the Customer form (customer.js): merge
them, with Frappe's own merge, or tell them apart. Two Customers may be the same when their Contacts share an
email or a phone, or they have the same tax ID. Customers are never merged without a person.

What was decided is kept as a WooCommerce Customer Decision. Its links to Customer follow any merge, as Frappe's
merge moves every link to the Customer it removes, also a merge made with Rename in the desk. The Customer hooks
record that merge too.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cstr, flt

from woocommerce_conduit.tasks.order_customer import (
	get_addresses_linking_to,
	normalise_phone,
	normalise_tax_id,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_customer_decision.woocommerce_customer_decision import (
	DECISION_MERGED,
	DECISION_NOT_THE_SAME,
)

DECISION_DOCTYPE = "WooCommerce Customer Decision"
# What two Customers share, as the form names it
EMAIL, TAX_ID, PHONE = "email", "tax_id", "phone"
# The names of the Customers that merges keep, from their before_rename to their after_rename, which run in the
# same request or job
KEPT_NAMES_FLAG = "woocommerce_kept_customer_names"


def get_contact_rows(
	child_doctype: str, fieldname: str, customers: list[str] | None = None, values: list[str] | None = None
) -> list[frappe._dict]:
	"""
	The emails (Contact Email, `email_id`) or phones (Contact Phone, `phone`) of the Contacts of Customers, as
	`customer` and `value`: of `customers` only, and only `values`, when they are given
	"""
	child = frappe.qb.DocType(child_doctype)
	link = frappe.qb.DocType("Dynamic Link")
	query = (
		frappe.qb.from_(child)
		.join(link)
		.on((link.parenttype == "Contact") & (link.parent == child.parent))
		.select(link.link_name.as_("customer"), child[fieldname].as_("value"))
		.where(child.parenttype == "Contact")
		.where(link.link_doctype == "Customer")
	)
	if customers is not None:
		query = query.where(link.link_name.isin(customers or [""]))
	if values is not None:
		query = query.where(child[fieldname].isin(values or [""]))
	return query.run(as_dict=True)


def get_customer_emails(customers: list[str] | None = None) -> dict[str, set[str]]:
	"""
	Every email of each Customer's Contacts, in lower case, by Customer
	"""
	emails = defaultdict(set)
	for row in get_contact_rows("Contact Email", "email_id", customers):
		if email := cstr(row.value).strip().lower():
			emails[row.customer].add(email)
	return emails


def get_customer_phones(customers: list[str] | None = None) -> dict[str, set[str]]:
	"""
	Every phone of each Customer's Contacts, as normalise_phone compares it, by Customer
	"""
	phones = defaultdict(set)
	for row in get_contact_rows("Contact Phone", "phone", customers):
		if phone := normalise_phone(row.value):
			phones[row.customer].add(phone)
	return phones


def get_decided_pairs(customer: str | None = None) -> set[frozenset[str]]:
	"""
	The pairs of Customers decided not to be the same buyer, those of `customer` only when it is given
	"""
	rows = frappe.get_all(
		DECISION_DOCTYPE,
		filters={"decision": DECISION_NOT_THE_SAME},
		or_filters={"customer": customer, "other_customer": customer} if customer else None,
		fields=["customer", "other_customer"],
	)
	return {frozenset((row.customer, row.other_customer)) for row in rows}


def get_matches(customer: str) -> dict[str, set[str]]:
	"""
	The other Customers that share an email or a phone of their Contacts with `customer`, or its tax ID, with
	what they share
	"""
	matches = defaultdict(set)

	if emails := get_customer_emails([customer]).get(customer):
		for row in get_contact_rows("Contact Email", "email_id", values=list(emails)):
			matches[row.customer].add(EMAIL)

	if phones := get_customer_phones([customer]).get(customer):
		for other, other_phones in get_customer_phones().items():
			if other_phones & phones:
				matches[other].add(PHONE)

	if tax_id := normalise_tax_id(frappe.db.get_value("Customer", customer, "tax_id")):
		for row in frappe.get_all(
			"Customer", filters={"tax_id": ["is", "set"]}, fields=["name", "tax_id"], order_by="creation asc"
		):
			if normalise_tax_id(row.tax_id) == tax_id:
				matches[row.name].add(TAX_ID)

	matches.pop(customer, None)
	return matches


def get_possible_duplicates(customer: str) -> list[dict]:
	"""
	The enabled Customers that may be the same buyer as `customer`, the oldest first: those that share an email
	or a phone of their Contacts with it, or its tax ID. Those decided not to be the same are left out, and so are
	those the user can't read.
	"""
	matches = get_matches(customer)
	decided = get_decided_pairs(customer)
	names = [name for name in matches if frozenset((customer, name)) not in decided]
	if not names:
		return []

	return [
		{"customer": row.name, "customer_name": row.customer_name, "reasons": sorted(matches[row.name])}
		for row in frappe.get_all(
			"Customer",
			filters={"name": ["in", names], "disabled": 0},
			fields=["name", "customer_name"],
			order_by="creation asc",
		)
		if frappe.has_permission("Customer", "read", doc=row.name)
	]


def add_possible_duplicates(doc, method=None):
	"""
	Customer onload: the Customers that may be the same buyer, which the form's Possible Duplicates field lists
	for a person to review
	"""
	if doc.is_new() or doc.disabled:
		return
	doc.set_onload("woocommerce_duplicates", get_possible_duplicates(doc.name))


def get_customer_summary(customer: str) -> frappe._dict:
	"""
	A Customer as the review dialog shows it beside the other one
	"""
	summary = frappe.db.get_value(
		"Customer",
		customer,
		[
			"name",
			"customer_name",
			"customer_type",
			"tax_id",
			"woocommerce_server",
			"woocommerce_identifier",
			"creation",
		],
		as_dict=True,
	)
	summary.emails = sorted(get_customer_emails([customer]).get(customer, set()))
	summary.phones = sorted(
		{cstr(row.value).strip() for row in get_contact_rows("Contact Phone", "phone", [customer])} - {""}
	)
	summary.addresses = [
		", ".join(part for part in (row.address_line1, row.pincode, row.city) if part)
		for row in get_addresses_linking_to("Customer", customer, fields=["address_line1", "pincode", "city"])
	]
	sales_order = frappe.qb.DocType("Sales Order")
	orders, ordered = (
		frappe.qb.from_(sales_order)
		.select(Count(sales_order.name), Sum(sales_order.base_grand_total))
		.where(sales_order.customer == customer)
		.where(sales_order.docstatus < 2)
	).run()[0]
	summary.orders, summary.ordered = orders or 0, flt(ordered)
	return summary


@frappe.whitelist()
def get_duplicate_review(customer: str, other_customer: str) -> dict:
	"""
	Two Customers side by side for the review dialog, with what they share, and which one to keep: the one with
	more Sales Orders, else the older one
	"""
	for name in (customer, other_customer):
		frappe.has_permission("Customer", "read", doc=name, throw=True)

	customers = [get_customer_summary(customer), get_customer_summary(other_customer)]
	keep = min(customers, key=lambda summary: (-summary.orders, summary.creation))
	tax_ids = [normalise_tax_id(summary.tax_id) for summary in customers]
	return {
		"customers": customers,
		"reasons": sorted(get_matches(customer).get(other_customer, set())),
		"keep": keep.name,
		# Two companies, as like as not
		"tax_ids_differ": all(tax_ids) and tax_ids[0] != tax_ids[1],
	}


@frappe.whitelist(methods=["POST"])
def mark_customers_different(customer: str, other_customer: str) -> None:
	"""
	Keep that two Customers are not the same buyer, so neither is suggested for the other again
	"""
	if customer == other_customer:
		frappe.throw(_("A Customer is the same as itself"))
	for name in (customer, other_customer):
		frappe.has_permission("Customer", "write", doc=name, throw=True)
	if frozenset((customer, other_customer)) in get_decided_pairs(customer):
		return

	frappe.get_doc(
		{
			"doctype": DECISION_DOCTYPE,
			"decision": DECISION_NOT_THE_SAME,
			"customer": customer,
			"other_customer": other_customer,
		}
	).insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def merge_customers(customer: str, into: str) -> str:
	"""
	Merge `customer` into `into` with Frappe's merge, as Rename does in the desk: its documents, Addresses and
	Contacts then belong to `into`, and it is removed. Frappe's rules apply: write access to both Customers, and
	delete access to the one removed. record_merge keeps what was decided.
	"""
	return frappe.rename_doc("Customer", customer, into, merge=True)


def remember_kept_customer_name(doc, method, old, new, merge=False):
	"""
	Customer before_rename: before a merge, the name of the Customer it keeps. ERPNext's after_rename names that
	Customer after its ID when Customers are named by their name, which would make "Jan Kowalski" of
	"Jan Kowalski - 1" "Jan Kowalski - 1".
	"""
	if merge:
		frappe.flags.setdefault(KEPT_NAMES_FLAG, {})[new] = frappe.db.get_value(
			"Customer", new, "customer_name"
		)


def record_merge(doc, method, old, new, merge=False):
	"""
	Customer after_rename: after `old` was merged into `doc`, before Frappe removes `old`. The links of the
	merged Customer, its decisions among them, point at `doc` already.
	- `doc` keeps its name, and gets what it lacks of the merged Customer: its tax ID, and the shop and
	  identifier of a Customer made by hand, so the shop's orders find it.
	- The merge is kept as a decision, whose identifier the order sync finds `doc` by (get_merged_customer).
	- Decisions that now pair `doc` with itself are dropped.
	"""
	if not merge:
		return

	merged = (
		frappe.db.get_value(
			"Customer",
			old,
			["customer_name", "tax_id", "woocommerce_server", "woocommerce_identifier"],
			as_dict=True,
		)
		or frappe._dict()
	)

	updates = {}
	kept_name = frappe.flags.get(KEPT_NAMES_FLAG, {}).pop(new, None)
	if kept_name and doc.customer_name != kept_name:
		updates["customer_name"] = kept_name
	if merged.tax_id and not doc.tax_id:
		updates["tax_id"] = merged.tax_id
	if merged.woocommerce_server and not doc.woocommerce_server:
		updates["woocommerce_server"] = merged.woocommerce_server
		updates["woocommerce_identifier"] = merged.woocommerce_identifier
	if updates:
		doc.db_set(updates)

	frappe.get_doc(
		{
			"doctype": DECISION_DOCTYPE,
			"decision": DECISION_MERGED,
			"customer": new,
			"merged_customer": old,
			"merged_customer_name": merged.customer_name,
			"woocommerce_server": merged.woocommerce_server,
			"woocommerce_identifier": merged.woocommerce_identifier,
		}
	).insert(ignore_permissions=True)
	frappe.db.delete(
		DECISION_DOCTYPE, {"decision": DECISION_NOT_THE_SAME, "customer": new, "other_customer": new}
	)
