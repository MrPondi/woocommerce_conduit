from unittest.mock import patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks.field_map import fit_value, get_first_field_names, parse_path


def field(fieldtype, **values):
	return _dict(
		{"fieldname": "target", "fieldtype": fieldtype, "reqd": 0, "length": 0, "options": None, **values}
	)


class TestPaths(IntegrationTestCase):
	def test_the_first_field_of_a_path(self):
		for path, first in (
			("$.meta_data[?key=='_gtin'].value", ("meta_data",)),
			("$.categories[0].name", ("categories",)),
			("$['tags'][0].name", ("tags",)),
			("sku", ("sku",)),
			("meta_data[0].value", ("meta_data",)),
			("$.sku,slug", ("sku", "slug")),
		):
			with self.subTest(path=path):
				self.assertEqual(get_first_field_names(path), first)

	def test_a_path_that_starts_nowhere_in_particular_has_no_first_field(self):
		for path in ("$..name", "$.*", "$"):
			with self.subTest(path=path):
				self.assertIsNone(get_first_field_names(path))

	def test_a_path_is_parsed_once(self):
		self.assertIs(parse_path("$.sku"), parse_path("$.sku"))


class TestFittingValues(IntegrationTestCase):
	def test_a_list_or_dict_only_goes_into_a_json_field(self):
		self.assertEqual(fit_value(field("JSON"), [{"name": "Garden"}]), ('[{"name": "Garden"}]', None))
		self.assertIsNotNone(fit_value(field("Data"), [{"name": "Garden"}])[1])
		self.assertIsNotNone(fit_value(field("Link", options="Item Group"), {"name": "Garden"})[1])

	def test_a_number_field_only_takes_a_plain_number(self):
		self.assertEqual(fit_value(field("Float"), "12.5"), ("12.5", None))
		self.assertEqual(fit_value(field("Check"), True), (True, None))
		# Frappe would read these as 125 and 0 without a word
		self.assertIsNotNone(fit_value(field("Float"), "12,5")[1])
		self.assertIsNotNone(fit_value(field("Check"), "yes")[1])

	def test_a_select_takes_one_of_its_options(self):
		select = field("Select", options="\nFIFO\nMoving Average")

		self.assertEqual(fit_value(select, " FIFO "), ("FIFO", None))
		self.assertIsNotNone(fit_value(select, "LIFO")[1])

	def test_text_longer_than_the_column_is_left_out(self):
		self.assertEqual(fit_value(field("Data"), "M" * 140), ("M" * 140, None))
		self.assertIsNotNone(fit_value(field("Data"), "M" * 141)[1])
		self.assertIsNotNone(fit_value(field("Data", length=10), "M" * 11)[1])
		# A text column has no such limit
		self.assertEqual(fit_value(field("Small Text"), "M" * 141), ("M" * 141, None))

	def test_a_mandatory_field_is_not_emptied(self):
		self.assertIsNotNone(fit_value(field("Data", reqd=1), "")[1])
		self.assertEqual(fit_value(field("Data"), ""), ("", None))

	def test_a_link_takes_a_record_that_exists_as_the_database_spells_it(self):
		link = field("Link", options="Item Group")
		with patch.object(frappe.db, "exists", return_value="Mowers"):
			self.assertEqual(fit_value(link, "mowers"), ("Mowers", None))
		with patch.object(frappe.db, "exists", return_value=None):
			self.assertIsNotNone(fit_value(link, "Gone")[1])
