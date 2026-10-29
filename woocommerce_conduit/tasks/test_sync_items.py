# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks.sync_items import (
	ERPNextItemToSync,
	SynchroniseItem,
	get_primary_connection,
	to_item_name,
	validate_primary_connection,
	values_match,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)


def make_sync(**kwargs) -> SynchroniseItem:
	# A non-empty server list, so the sync doesn't look the servers up
	return SynchroniseItem(servers=[_dict()], **kwargs)


def make_product(**values) -> WooCommerceProduct:
	with patch.object(WooCommerceProduct, "_init_api", return_value=[]):
		return frappe.get_doc(
			{
				"doctype": "WooCommerce Product",
				"name": "one.example.com~1",
				"woocommerce_server": "one.example.com",
				"woocommerce_id": "1",
				"woocommerce_name": "Mug",
				"images": "[]",
				"attributes": "[]",
				**values,
			}
		)  # type: ignore


class TestValueComparison(IntegrationTestCase):
	def test_woocommerce_strings_match_erpnext_numbers(self):
		self.assertTrue(values_match(10.0, "10.00"))
		self.assertTrue(values_match(0, ""))
		self.assertTrue(values_match(None, ""))
		self.assertFalse(values_match(10.0, "12"))
		self.assertFalse(values_match(1, "one"))
		self.assertFalse(values_match("Mug", "Cup"))

	def test_item_names_are_capped(self):
		self.assertEqual(len(to_item_name("M" * 200)), 140)
		self.assertEqual(to_item_name("Mug"), "Mug")
		self.assertIsNone(to_item_name(None))


class TestSyncDirection(IntegrationTestCase):
	def run_sync(self, item_modified: str, date_modified: str, date_modified_gmt: str, last_sync_hash=None):
		item = _dict(
			modified=item_modified,
			woocommerce_servers=[_dict(idx=1, enable_sync=1, woocommerce_last_sync_hash=last_sync_hash)],
		)
		product = _dict(woocommerce_date_modified=date_modified, date_modified_gmt=date_modified_gmt)
		sync = make_sync(item=ERPNextItemToSync(item=item, item_woocommerce_server_idx=1))  # type: ignore
		sync.woocommerce_product = product  # type: ignore

		with (
			patch("frappe.utils.data.get_system_timezone", return_value="Europe/Warsaw"),
			patch.object(SynchroniseItem, "update_item") as update_item,
		):
			sync.sync_wc_product_with_erpnext_item()
		return update_item

	def test_dates_are_compared_in_the_same_timezone(self):
		# The Item was saved at 10:30 in Warsaw (09:30 UTC), the product at 09:45 UTC in a UTC shop, so the
		# product is the newer one, although its local date_modified reads earlier than the Item's
		update_item = self.run_sync("2025-01-10 10:30:00", "2025-01-10T09:45:00", "2025-01-10T09:45:00")

		update_item.assert_called_once()

	def test_an_item_edited_in_erpnext_since_is_left_alone(self):
		update_item = self.run_sync("2025-01-10 11:00:00", "2025-01-10T09:45:00", "2025-01-10T09:45:00")

		update_item.assert_not_called()

	def test_a_matching_sync_hash_read_back_as_a_datetime_skips_the_sync(self):
		update_item = self.run_sync(
			"2025-01-10 10:30:00",
			"2025-01-10T09:45:00",
			"2025-01-10T09:45:00",
			last_sync_hash=frappe.utils.get_datetime("2025-01-10 09:45:00"),
		)

		update_item.assert_not_called()


class TestItemFields(IntegrationTestCase):
	def set_item_fields(self, item, product):
		server = _dict(
			item_field_map=[
				_dict(erpnext_field_name="description | Description", woocommerce_field_name="$.sku"),
				_dict(erpnext_field_name="weight_per_unit | Weight", woocommerce_field_name="$.weight"),
			]
		)
		sync = make_sync(woocommerce_product=product)
		with patch("frappe.get_cached_doc", return_value=server):
			return sync.set_item_fields(item)

	def test_an_unchanged_product_does_not_change_the_item(self):
		item = frappe.new_doc("Item")
		item.description = "MUG-1"
		item.weight_per_unit = 1.5

		modified, _item = self.set_item_fields(item, make_product(sku="MUG-1", weight="1.50"))

		self.assertFalse(modified)

	def test_changed_fields_are_copied(self):
		item = frappe.new_doc("Item")
		item.description = "MUG-1"
		item.weight_per_unit = 1.5

		modified, item = self.set_item_fields(item, make_product(sku="MUG-2", weight="1.50"))

		self.assertTrue(modified)
		self.assertEqual(item.description, "MUG-2")


class TestItemAttributes(IntegrationTestCase):
	def test_values_are_added_and_existing_ones_kept(self):
		attribute = frappe.new_doc("Item Attribute")
		attribute.append("item_attribute_values", {"attribute_value": "Red", "abbr": "Red"})
		attribute.append("item_attribute_values", {"attribute_value": "Dark Blue", "abbr": "DarkBlue"})

		added = make_sync()._add_missing_attribute_values(attribute, ["Red", "Green", "DarkBlue"])

		self.assertTrue(added)
		self.assertEqual(
			[(val.attribute_value, val.abbr) for val in attribute.item_attribute_values],
			[("Red", "Red"), ("Dark Blue", "DarkBlue"), ("Green", "Green"), ("DarkBlue", "DarkBlue2")],
		)

	def test_nothing_is_added_when_every_value_exists(self):
		attribute = frappe.new_doc("Item Attribute")
		attribute.append("item_attribute_values", {"attribute_value": "Red", "abbr": "Red"})

		self.assertFalse(make_sync()._add_missing_attribute_values(attribute, ["Red"]))


class TestPrimaryConnection(IntegrationTestCase):
	@staticmethod
	def item_with_connections(*primary_flags: int):
		item = frappe.new_doc("Item")
		item.item_name = "Mug"
		for idx, is_primary in enumerate(primary_flags, start=1):
			item.append(
				"woocommerce_servers",
				{
					"woocommerce_server": f"shop{idx}.example.com",
					"woocommerce_id": str(idx),
					"is_primary": is_primary,
					"idx": idx,
				},
			)
		return item

	def test_the_marked_connection_is_primary(self):
		item = self.item_with_connections(0, 1)

		self.assertEqual(get_primary_connection(item).idx, 2)

	def test_without_a_mark_the_first_connection_is_primary(self):
		item = self.item_with_connections(0, 0)

		self.assertEqual(get_primary_connection(item).idx, 1)
		validate_primary_connection(item)
		self.assertEqual([row.is_primary for row in item.woocommerce_servers], [1, 0])

	def test_only_one_connection_can_be_primary(self):
		with self.assertRaises(frappe.ValidationError):
			validate_primary_connection(self.item_with_connections(1, 1))

	def test_a_secondary_shop_does_not_change_the_item(self):
		item = self.item_with_connections(1, 0)
		product = make_product(woocommerce_name="Shop 2 Mug", woocommerce_server="shop2.example.com")
		sync = make_sync(
			item=ERPNextItemToSync(item=item, item_woocommerce_server_idx=2),  # type: ignore
			woocommerce_product=product,
		)

		with patch.object(SynchroniseItem, "set_sync_hash") as set_sync_hash:
			sync.update_item()

		self.assertEqual(item.item_name, "Mug")
		set_sync_hash.assert_called_once()
