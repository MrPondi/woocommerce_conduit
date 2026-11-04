import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.custom_fields import get_custom_fields


# Only reads: making a field commits (updatedb), so a test that made one would leave it on the site
class TestCustomFields(IntegrationTestCase):
	def test_the_site_has_the_fields_as_defined(self):
		for doctype, fields in get_custom_fields().items():
			for df in fields:
				with self.subTest(doctype=doctype, fieldname=df["fieldname"]):
					field = frappe.db.get_value(
						"Custom Field",
						{"dt": doctype, "fieldname": df["fieldname"]},
						["is_system_generated", *df],
						as_dict=True,
					)
					self.assertIsNotNone(field)
					self.assertEqual(field.pop("is_system_generated"), 1)
					self.assertEqual(field, df)

	def test_the_customer_form_starts_with_its_possible_duplicates_then_its_shop(self):
		# Above ERPNext's first section, the duplicates in a section of their own, so they take the whole width
		fieldnames = [df.fieldname for df in frappe.get_meta("Customer").fields]
		self.assertEqual(
			fieldnames[:7],
			[
				"woocommerce_duplicates_section",
				"woocommerce_duplicates",
				"woocommerce_section",
				"woocommerce_identifier",
				"woocommerce_column_break",
				"woocommerce_server",
				"basic_info",
			],
		)

	def test_each_field_follows_a_field_of_its_doctype(self):
		# Frappe puts a field whose insert_after it doesn't find at the end of the form, such as after a rename
		for doctype, fields in get_custom_fields().items():
			meta = frappe.get_meta(doctype)
			for df in fields:
				if df.get("insert_after"):
					with self.subTest(doctype=doctype, fieldname=df["fieldname"]):
						self.assertTrue(meta.has_field(df["insert_after"]))
