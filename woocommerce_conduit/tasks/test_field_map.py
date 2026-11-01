from unittest.mock import patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks import field_map
from woocommerce_conduit.tasks.field_map import (
	LOGGED_PROBLEMS_FLAG,
	fit_value,
	get_first_field_names,
	get_mapped_values,
	get_transforms,
	parse_path,
	validate_map_row,
)


def field(fieldtype, **values):
	return _dict(
		{"fieldname": "target", "fieldtype": fieldtype, "reqd": 0, "length": 0, "options": None, **values}
	)


def join_names(value, row, record):
	return ", ".join(entry["name"] for entry in value)


def no_value(value, row, record):
	return None


def broken(value, row, record):
	raise ValueError("broken")


TRANSFORMS = {name: f"{__name__}.{name}" for name in ("join_names", "no_value", "broken")}


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


class TestTransforms(IntegrationTestCase):
	def test_the_registry_takes_the_app_loaded_last(self):
		with patch(
			"frappe.get_hooks", return_value={"one": ["app_a.one"], "two": ["app_a.two", "app_b.two"]}
		):
			self.assertEqual(get_transforms(), {"one": "app_a.one", "two": "app_b.two"})

	def test_without_the_hook_there_are_no_transforms(self):
		with patch("frappe.get_hooks", side_effect=lambda hook, default=None: default):
			self.assertEqual(get_transforms(), {})

	def map_tags(self, transform):
		row = _dict(
			erpnext_field_name="item_name | Item Name", woocommerce_field_name="$.tags", transform=transform
		)
		record = {"name": "one.example.com~1", "tags": [{"name": "Garden &amp; Patio"}, {"name": "Tools"}]}
		frappe.flags.pop(LOGGED_PROBLEMS_FLAG, None)
		with (
			patch.object(field_map, "get_transforms", return_value=TRANSFORMS),
			patch.object(frappe, "log_error") as log_error,
		):
			values = get_mapped_values([row], record, frappe.get_meta("Item"), record["name"])
		return values, log_error

	def test_a_transform_gets_the_value_before_it_is_unescaped(self):
		values, _log_error = self.map_tags("join_names")

		# A list such as the tags only becomes text in the transform, so the text is unescaped after it
		self.assertEqual(values, {"item_name": "Garden & Patio, Tools"})

	def test_a_transform_that_returns_nothing_leaves_the_field(self):
		values, log_error = self.map_tags("no_value")

		self.assertEqual(values, {})
		log_error.assert_not_called()

	def test_a_transform_that_fails_or_is_gone_leaves_the_field_and_is_logged(self):
		for transform in ("broken", "uninstalled"):
			with self.subTest(transform=transform):
				values, log_error = self.map_tags(transform)

				self.assertEqual(values, {})
				log_error.assert_called_once()

	def test_a_row_names_a_transform_an_app_registers(self):
		row = _dict(
			erpnext_field_name="item_name | Item Name",
			woocommerce_field_name="$.sku",
			transform="uninstalled",
			idx=1,
		)
		with (
			patch.object(field_map, "get_transforms", return_value=TRANSFORMS),
			self.assertRaises(frappe.ValidationError),
		):
			validate_map_row(row, "Item Field Map", "Item", {"item_name"})
