# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.desk.form.load import run_onload
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks.customer_duplicates import (
	get_decided_pairs,
	get_duplicate_review,
	get_possible_duplicates,
	mark_customers_different,
	merge_customers,
)
from woocommerce_conduit.tasks.order_customer import get_merged_customer, normalise_phone

SERVER = "one.example.com"
DECISION = "WooCommerce Customer Decision"


class TestCustomerDuplicates(IntegrationTestCase):
	"""
	Customers, Contacts and decisions made for real in the test's transaction
	"""

	def setUp(self):
		super().setUp()
		# Frappe rolls back once a class is done, and each test finds Customers by what the others left
		frappe.db.savepoint("test_customer_duplicates")
		self.addCleanup(frappe.db.rollback, save_point="test_customer_duplicates")
		# What the Customers and decisions link to
		frappe.get_doc(
			{"doctype": "WooCommerce Server", "name": SERVER, "woocommerce_server_url": f"https://{SERVER}"}
		).db_insert()

	def make_customer(self, name, email=None, phone=None, creation="2026-01-01 10:00:00", **values) -> str:
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"name": f"_Test WC {name}",
				"customer_name": values.pop("customer_name", name),
				"customer_type": "Individual",
				"creation": creation,
				**values,
			}
		)
		customer.db_insert()
		if email or phone:
			contact = frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": name,
					"links": [{"link_doctype": "Customer", "link_name": customer.name}],
				}
			)
			if email:
				contact.append("email_ids", {"email_id": email, "is_primary": 1})
			if phone:
				contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})
			contact.insert()
		return customer.name

	def merge(self, customer, into):
		"""
		Merge as Rename does in the desk, with Customers named by their name, which ERPNext then names after
		their ID
		"""
		get_global_default = frappe.defaults.get_global_default
		with patch.object(
			frappe.defaults,
			"get_global_default",
			side_effect=lambda key, *args, **kwargs: (
				"Customer Name" if key == "cust_master_name" else get_global_default(key, *args, **kwargs)
			),
		):
			frappe.rename_doc("Customer", customer, into, merge=True, rebuild_search=False)

	def test_customers_that_share_an_email_a_phone_or_a_tax_id_may_be_the_same(self):
		ann = self.make_customer(
			"Ann", "ann@duplicates.example", "+48 999 888 777", tax_id="PL 000-111-22-33"
		)
		by_email = self.make_customer("Ann Email", "ANN@duplicates.example")
		by_phone = self.make_customer("Ann Phone", phone="999888777", creation="2026-01-02 10:00:00")
		by_tax_id = self.make_customer("Ann Tax", tax_id="0001112233", creation="2026-01-03 10:00:00")
		# A name alone is no sign, and a disabled Customer is not suggested
		self.make_customer("Ann Name", customer_name="Ann")
		self.make_customer("Ann Disabled", "ann@duplicates.example", disabled=1)

		duplicates = get_possible_duplicates(ann)

		self.assertEqual(
			[(row["customer"], row["reasons"]) for row in duplicates],
			[(by_email, ["email"]), (by_phone, ["phone"]), (by_tax_id, ["tax_id"])],
		)

	def test_phones_are_compared_by_their_last_nine_digits(self):
		self.assertEqual(normalise_phone("+48 500-100-200"), "500100200")
		self.assertEqual(normalise_phone("0048500100200"), "500100200")
		# Too short to tell a number apart
		self.assertEqual(normalise_phone("12 34"), "")

	def test_customers_told_apart_are_not_suggested_again(self):
		ann = self.make_customer("Ann", "shared@duplicates.example")
		bob = self.make_customer("Bob", "shared@duplicates.example")

		mark_customers_different(bob, ann)
		mark_customers_different(ann, bob)

		self.assertEqual(frappe.db.count(DECISION, {"customer": ["in", [ann, bob]]}), 1)
		self.assertEqual(get_possible_duplicates(ann), [])
		self.assertEqual(get_possible_duplicates(bob), [])

	def test_telling_customers_apart_needs_write_access(self):
		ann = self.make_customer("Ann", "shared@duplicates.example")
		bob = self.make_customer("Bob", "shared@duplicates.example")

		with self.set_user("Guest"), self.assertRaises(frappe.PermissionError):
			mark_customers_different(ann, bob)
		with self.assertRaises(frappe.ValidationError):
			mark_customers_different(ann, ann)

	def test_the_review_keeps_the_customer_with_more_orders(self):
		ann = self.make_customer("Ann", "shared@duplicates.example", tax_id="PL0001112233")
		bob = self.make_customer(
			"Bob", "shared@duplicates.example", tax_id="PL9998887766", creation="2026-02-01 10:00:00"
		)
		frappe.get_doc(
			{
				"doctype": "Sales Order",
				"name": "_Test WC SO",
				"customer": bob,
				"docstatus": 1,
				"base_grand_total": 120,
			}
		).db_insert()

		review = get_duplicate_review(ann, bob)

		self.assertEqual(review["keep"], bob)
		self.assertEqual(review["reasons"], ["email"])
		self.assertTrue(review["tax_ids_differ"])
		ann_summary, bob_summary = review["customers"]
		self.assertEqual(
			(ann_summary.emails, bob_summary.orders, bob_summary.ordered),
			(["shared@duplicates.example"], 1, 120),
		)

	def test_a_merge_keeps_what_was_decided_and_what_the_merged_customer_was(self):
		# A shop's Customer, and one made by hand for an offline sale, whose ID isn't its name
		shop = self.make_customer(
			"Ann Shop",
			"ann@duplicates.example",
			customer_name="Ann Smith",
			tax_id="PL0001112233",
			woocommerce_server=SERVER,
			woocommerce_identifier="ann",
		)
		hand = self.make_customer("Ann", customer_name="Ann Smith")
		bob = self.make_customer("Bob", "bob@duplicates.example")
		mark_customers_different(shop, bob)
		mark_customers_different(shop, hand)

		self.merge(shop, hand)

		kept = frappe.get_doc("Customer", hand)
		# ERPNext would have named it "_Test WC Ann"
		self.assertEqual(kept.customer_name, "Ann Smith")
		self.assertEqual(
			(kept.tax_id, kept.woocommerce_server, kept.woocommerce_identifier),
			("PL0001112233", SERVER, "ann"),
		)
		self.assertEqual(
			frappe.get_all(
				DECISION,
				filters={"decision": "Merged", "customer": hand},
				fields=["customer", "merged_customer", "merged_customer_name", "woocommerce_identifier"],
			),
			[
				{
					"customer": hand,
					"merged_customer": shop,
					"merged_customer_name": "Ann Smith",
					"woocommerce_identifier": "ann",
				}
			],
		)
		# What was decided about the merged Customer is the kept one's, and the pair of the two is gone
		self.assertEqual(get_decided_pairs(hand), {frozenset((hand, bob))})
		self.assertEqual(get_merged_customer(SERVER, "ann"), hand)
		# Its Contacts are the kept one's, so are its emails
		self.assertEqual(get_possible_duplicates(hand), [])

	def test_a_merge_into_a_shops_customer_keeps_its_identity(self):
		kept = self.make_customer(
			"Ann", customer_name="Ann Smith", woocommerce_server=SERVER, woocommerce_identifier="ann"
		)
		guest = self.make_customer("Guest", woocommerce_server=SERVER, woocommerce_identifier="Guest-12")

		self.merge(guest, kept)

		self.assertEqual(
			frappe.db.get_value("Customer", kept, ["customer_name", "woocommerce_identifier"]),
			("Ann Smith", "ann"),
		)
		# The guest's identifier finds the kept Customer, though no guest order comes again
		self.assertEqual(get_merged_customer(SERVER, "Guest-12"), kept)

	def test_merge_customers_is_frappes_merge(self):
		with patch.object(frappe, "rename_doc", return_value="_Test WC Kept") as rename_doc:
			self.assertEqual(merge_customers("_Test WC Gone", "_Test WC Kept"), "_Test WC Kept")

		rename_doc.assert_called_once_with("Customer", "_Test WC Gone", "_Test WC Kept", merge=True)

	def test_the_form_lists_the_possible_duplicates(self):
		ann = self.make_customer("Ann", phone="999 777 555")
		bob = self.make_customer("Bob", phone="+48999777555")

		# As the desk loads the form
		customer = frappe.get_doc("Customer", ann)
		run_onload(customer)

		self.assertEqual(
			customer.get_onload().woocommerce_duplicates,
			[{"customer": bob, "customer_name": "Bob", "reasons": ["phone"]}],
		)
