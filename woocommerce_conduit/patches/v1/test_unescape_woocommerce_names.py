from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.patches.v1 import unescape_woocommerce_names as patch_module


def make_attribute(*values):
	attribute = frappe.new_doc("Item Attribute")
	attribute.attribute_name = "Type"
	for value in values:
		attribute.append("item_attribute_values", {"attribute_value": value, "abbr": value.replace(" ", "")})
	return attribute


class TestUnescapeAttributeValues(IntegrationTestCase):
	def rename(self, attribute, save_error=None):
		with (
			patch.object(frappe, "get_all", return_value=["Type"]),
			patch.object(frappe, "get_doc", return_value=attribute),
			patch.object(type(attribute), "save", side_effect=save_error) as save,
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe, "log_error") as log_error,
		):
			left = patch_module.rename_attribute_values()
		return left, save, rollback, log_error

	def test_escaped_values_are_renamed_on_their_row(self):
		attribute = make_attribute("Sleeves &amp; Toploader", "Box")

		left, save, _rollback, _log_error = self.rename(attribute)

		self.assertEqual(
			[(row.attribute_value, row.abbr) for row in attribute.item_attribute_values],
			# The abbreviation stays, as ERPNext would rename the variants' codes with it
			[("Sleeves & Toploader", "Sleeves&amp;Toploader"), ("Box", "Box")],
		)
		save.assert_called_once()
		self.assertEqual(left, [])

	def test_a_value_with_a_plain_twin_is_left(self):
		# Twins regardless of case, as ERPNext compares them
		attribute = make_attribute("Sleeves &amp; Toploader", "sleeves & toploader")

		left, save, _rollback, _log_error = self.rename(attribute)

		self.assertEqual(attribute.item_attribute_values[0].attribute_value, "Sleeves &amp; Toploader")
		save.assert_not_called()
		self.assertEqual(left, ["Type: Sleeves &amp; Toploader"])

	def test_an_attribute_erpnext_will_not_save_is_left_and_logged(self):
		attribute = make_attribute("Sleeves &amp; Toploader")

		left, _save, rollback, log_error = self.rename(attribute, save_error=frappe.ValidationError("no"))

		rollback.assert_called_once_with(save_point="unescape_attribute")
		log_error.assert_called_once()
		self.assertEqual(left, ["Type: Sleeves &amp; Toploader"])

	def test_numeric_attributes_are_left_alone(self):
		attribute = make_attribute("1&amp;2")
		attribute.numeric_values = 1

		_left, save, _rollback, _log_error = self.rename(attribute)

		save.assert_not_called()
