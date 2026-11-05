from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.patches.v1.add_phone_country_codes import execute

CUSTOMER = "_Test WC Phones"


def make_address(address_line1: str, phone: str, customers: list[str]) -> str:
	return (
		frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": "Ann Smith",
				"address_type": "Billing",
				"address_line1": address_line1,
				"city": "Kraków",
				"pincode": "30-001",
				"country": "Poland",
				"phone": phone,
				"links": [{"link_doctype": "Customer", "link_name": name} for name in customers],
			}
		)
		.insert()
		.name
	)


class TestAddPhoneCountryCodes(IntegrationTestCase):
	def setUp(self):
		frappe.db.savepoint("test_add_phone_country_codes")
		self.addCleanup(frappe.db.rollback, save_point="test_add_phone_country_codes")
		frappe.get_doc(
			{
				"doctype": "Customer",
				"name": CUSTOMER,
				"customer_name": "Ann Smith",
				"customer_type": "Individual",
			}
		).db_insert()
		self.address = make_address("Order Street 1", "500 100 200", [CUSTOMER])
		self.unknown = make_address("Order Street 2", "555-0100", [CUSTOMER])
		self.of_no_customer = make_address("Other Street 3", "500 100 201", [])
		self.contact = (
			frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": "Ann",
					"last_name": "Smith",
					# Its phones are read in the country of its Address
					"address": self.address,
					"phone_nos": [{"phone": "600 100 200", "is_primary_phone": 1, "is_primary_mobile_no": 1}],
					"links": [{"link_doctype": "Customer", "link_name": CUSTOMER}],
				}
			)
			.insert()
			.name
		)
		# As ERPNext fetches it from the primary Contact
		frappe.db.set_value(
			"Customer", CUSTOMER, {"customer_primary_contact": self.contact, "mobile_no": "600 100 200"}
		)

	def run_patch(self):
		with patch("builtins.print"):
			execute()

	def phone(self, doctype: str, name: str):
		return frappe.db.get_value(doctype, name, "phone")

	def test_a_customers_numbers_get_their_country_code(self):
		self.run_patch()

		self.assertEqual(self.phone("Address", self.address), "+48500100200")
		self.assertEqual(
			frappe.db.get_value("Contact Phone", {"parent": self.contact}, "phone"), "+48600100200"
		)
		# What repeats or fetches the Contact's number follows it
		self.assertEqual(
			frappe.db.get_value("Contact", self.contact, ["phone", "mobile_no"]),
			("+48600100200", "+48600100200"),
		)
		self.assertEqual(frappe.db.get_value("Customer", CUSTOMER, "mobile_no"), "+48600100200")

	def test_other_numbers_are_left_as_they_are(self):
		self.run_patch()

		# One the numbering plan doesn't know, and one of an Address of no Customer
		self.assertEqual(self.phone("Address", self.unknown), "555-0100")
		self.assertEqual(self.phone("Address", self.of_no_customer), "500 100 201")

	def test_running_it_again_changes_nothing(self):
		self.run_patch()
		self.run_patch()

		self.assertEqual(self.phone("Address", self.address), "+48500100200")
		self.assertEqual(frappe.db.get_value("Customer", CUSTOMER, "mobile_no"), "+48600100200")
