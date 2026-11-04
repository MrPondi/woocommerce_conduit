# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.woocommerce.report.woocommerce_duplicate_customers.woocommerce_duplicate_customers import (
	build_groups,
	drop_decided_pairs,
	execute,
	get_rows,
)

SERVER = "one.example.com"


def customer(
	name, emails=(), tax_id=None, customer_name=None, identifier=None, creation="2026-01-01", phones=()
):
	"""
	A Customer as the report reads it
	"""
	return frappe._dict(
		name=name,
		customer_name=customer_name or name,
		customer_type="Individual",
		tax_id=tax_id,
		woocommerce_identifier=identifier,
		woocommerce_server=SERVER,
		creation=creation,
		orders=0,
		ordered=0,
		emails=set(emails),
		phones=set(phones),
	)


class TestDuplicateCustomers(IntegrationTestCase):
	def test_customers_are_grouped_by_each_sign_of_one_buyer(self):
		customers = [
			customer("A", ["ann@firma.pl"], "PL6791003801", "Ann Smith", phones=["500100200"]),
			customer("B", ["ann@firma.pl"]),
			customer("C", tax_id="679-100-38-01"),
			customer("D", ["bob@firma.pl"], customer_name="ANN  smith", phones=["500100200"]),
			# Free mail says nothing about who the customer is
			customer("E", ["x@wp.pl"]),
			customer("F", ["y@wp.pl"]),
		]

		groups = build_groups(customers, frappe._dict())

		self.assertEqual(
			groups,
			{
				("Email", "ann@firma.pl"): ["A", "B"],
				("Tax ID", "6791003801"): ["A", "C"],
				("Phone", "500100200"): ["A", "D"],
				("Email Domain", "firma.pl"): ["A", "B", "D"],
				("Name", "ann smith"): ["A", "D"],
			},
		)
		self.assertEqual(
			list(build_groups(customers, frappe._dict(grouped_by="Tax ID"))), [("Tax ID", "6791003801")]
		)
		# The domains to leave out are the filter's
		self.assertIn(
			("Email Domain", "wp.pl"),
			build_groups(
				customers, frappe._dict(grouped_by="Email Domain", ignore_email_domains="gmail.com")
			),
		)

	def test_customers_decided_not_to_be_the_same_are_left_out(self):
		groups = {("Email", "a@firma.pl"): ["A", "B", "C"], ("Phone", "500100200"): ["A", "B"]}
		# A and B are told apart, so only C keeps them in the email's group
		decided = {frozenset(("A", "B"))}

		self.assertEqual(drop_decided_pairs(groups, decided), {("Email", "a@firma.pl"): ["A", "B", "C"]})
		decided |= {frozenset(("A", "C")), frozenset(("B", "C"))}
		self.assertEqual(drop_decided_pairs(groups, decided), {})

	def test_the_biggest_group_comes_first_and_its_oldest_customer(self):
		customers = [
			customer("New", ["a@firma.pl"], identifier="Guest-12", creation="2026-03-01"),
			customer("Old", ["a@firma.pl"], identifier="ann", creation="2026-01-01"),
			customer("Third", ["c@firma.pl", "a@firma.pl"], creation="2026-02-01"),
			customer("Other", ["c@firma.pl"], creation="2026-01-15"),
		]
		groups = build_groups(customers, frappe._dict(grouped_by="Email"))

		rows = get_rows(groups, customers)

		self.assertEqual(
			[(row["group"], row["customer"]) for row in rows],
			[
				("a@firma.pl", "Old"),
				("a@firma.pl", "Third"),
				("a@firma.pl", "New"),
				("c@firma.pl", "Other"),
				("c@firma.pl", "Third"),
			],
		)
		self.assertEqual([row["is_guest"] for row in rows[:3]], [0, 0, 1])

	def test_the_emails_are_those_of_the_customers_contacts(self):
		frappe.db.savepoint("test_duplicate_customers")
		self.addCleanup(frappe.db.rollback, save_point="test_duplicate_customers")
		for name, creation in (("_Test WC Ann", "2026-01-01"), ("_Test WC Guest", "2026-02-01")):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"name": name,
					"customer_name": "Ann Smith",
					"woocommerce_server": SERVER,
					"woocommerce_identifier": name,
					"creation": creation,
				}
			).db_insert()
			frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": "Ann",
					"email_ids": [{"email_id": "Ann@Example.com", "is_primary": 1}],
					"links": [{"link_doctype": "Customer", "link_name": name}],
				}
			).insert()
		frappe.get_doc(
			{
				"doctype": "Sales Order",
				"name": "_Test WC SO",
				"customer": "_Test WC Ann",
				"docstatus": 1,
				"base_grand_total": 120,
			}
		).db_insert()

		_columns, rows = execute({"grouped_by": "Email", "woocommerce_server": SERVER, "woocommerce_only": 1})

		self.assertEqual(
			[(row["group"], row["customer"], row["orders"], row["ordered"]) for row in rows],
			[("ann@example.com", "_Test WC Ann", 1, 120), ("ann@example.com", "_Test WC Guest", 0, 0)],
		)
