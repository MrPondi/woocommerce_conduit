# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.woocommerce.report.woocommerce_duplicate_customers.woocommerce_duplicate_customers import (
	build_groups,
	drop_decided_pairs,
	execute,
	get_ignored_email_domains,
	get_rows,
	parse_email_domains,
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

		groups = build_groups(customers, frappe._dict(), {"wp.pl"})

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
			list(build_groups(customers, frappe._dict(grouped_by="Tax ID"), {"wp.pl"})),
			[("Tax ID", "6791003801")],
		)
		# A domain that isn't ignored groups its Customers
		self.assertIn(
			("Email Domain", "wp.pl"),
			build_groups(customers, frappe._dict(grouped_by="Email Domain"), {"gmail.com"}),
		)

	def test_the_ignored_email_domains_are_those_of_woocommerce_settings(self):
		with patch.object(
			frappe, "get_cached_doc", return_value=frappe._dict(ignore_email_domains=" Firma.PL\n@wp.pl,, ")
		):
			self.assertEqual(get_ignored_email_domains(), {"firma.pl", "wp.pl"})
		self.assertEqual(parse_email_domains(None), set())

		# The Settings ignore the free mail providers until someone changes them
		default = frappe.get_meta("WooCommerce Settings").get_field("ignore_email_domains").default
		self.assertLessEqual({"gmail.com", "outlook.com", "wp.pl", "onet.pl"}, parse_email_domains(default))

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
		groups = build_groups(customers, frappe._dict(grouped_by="Email"), set())

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
		for name, creation, server in (
			("_Test WC Ann", "2026-01-01", SERVER),
			("_Test WC Guest", "2026-02-01", SERVER),
			# Made by hand, which may be the same buyer as well
			("_Test WC By Hand", "2026-03-01", None),
		):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"name": name,
					"customer_name": "Ann Smith",
					"woocommerce_server": server,
					"woocommerce_identifier": name if server else None,
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

		def get_group(filters):
			_columns, rows = execute({"grouped_by": "Email", **filters})
			return [
				(row["customer"], row["orders"], row["ordered"])
				for row in rows
				if row["group"] == "ann@example.com"
			]

		# Every Customer, whoever made it
		self.assertEqual(
			get_group({}),
			[("_Test WC Ann", 1, 120), ("_Test WC Guest", 0, 0), ("_Test WC By Hand", 0, 0)],
		)
		self.assertEqual(
			get_group({"woocommerce_server": SERVER}), [("_Test WC Ann", 1, 120), ("_Test WC Guest", 0, 0)]
		)
