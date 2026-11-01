# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks import sync_items
from woocommerce_conduit.tasks.sync_items import (
	ERPNextItemToSync,
	SynchroniseItem,
	find_item_by_sku,
	get_primary_connection,
	to_item_name,
	validate_primary_connection,
	values_match,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)


def make_sync(**kwargs) -> SynchroniseItem:
	return SynchroniseItem(**kwargs)


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
	def run_sync(
		self,
		item_modified: str,
		date_modified: str,
		date_modified_gmt: str,
		last_sync_hash=None,
		force_update=True,
	):
		item = _dict(
			modified=item_modified,
			woocommerce_servers=[_dict(idx=1, woocommerce_last_sync_hash=last_sync_hash)],
		)
		product = _dict(woocommerce_date_modified=date_modified, date_modified_gmt=date_modified_gmt)
		sync = make_sync(
			item=ERPNextItemToSync(item=item, item_woocommerce_server_idx=1),  # type: ignore
			force_update=force_update,
		)
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

	def test_a_linked_item_is_only_pulled_on_request(self):
		# The product is newer, but only a manual sync updates an Item that already exists
		update_item = self.run_sync(
			"2025-01-10 10:30:00", "2025-01-10T09:45:00", "2025-01-10T09:45:00", force_update=False
		)

		update_item.assert_not_called()


class TestItemCreation(IntegrationTestCase):
	def create_prices(self, product, server):
		item = _dict(name="MUG-1")
		sync = make_sync(
			item=ERPNextItemToSync(item=item, item_woocommerce_server_idx=1),  # type: ignore
			woocommerce_product=product,
		)
		with patch.object(SynchroniseItem, "_insert_item_price") as insert_item_price:
			sync.create_item_prices(server)  # type: ignore
		return insert_item_price

	def test_prices_are_pulled_into_the_price_lists(self):
		product = make_product(
			type="simple",
			regular_price="12.50",
			sale_price="10",
			date_on_sale_from="2025-01-10T00:00:00",
			date_on_sale_to=None,
		)
		server = _dict(price_list="Standard Selling", sales_price_list="Sale")

		insert_item_price = self.create_prices(product, server)

		self.assertEqual(
			[c.args for c in insert_item_price.call_args_list], [("Standard Selling", 12.5), ("Sale", 10.0)]
		)
		self.assertEqual(
			insert_item_price.call_args_list[1].kwargs["valid_from"], frappe.utils.getdate("2025-01-10")
		)

	def test_no_sale_and_no_price_list_create_nothing(self):
		product = make_product(type="simple", regular_price="12.50", sale_price="")

		insert_item_price = self.create_prices(product, _dict(price_list=None, sales_price_list="Sale"))

		insert_item_price.assert_not_called()

	def test_a_variable_product_has_no_prices_of_its_own(self):
		product = make_product(type="variable", regular_price="12.50")

		insert_item_price = self.create_prices(product, _dict(price_list="Standard Selling"))

		insert_item_price.assert_not_called()

	def insert_price(self, existing_price):
		sync = make_sync(item=ERPNextItemToSync(item=_dict(name="MUG-1"), item_woocommerce_server_idx=1))  # type: ignore
		with (
			patch.object(frappe.db, "exists", return_value=existing_price),
			patch.object(frappe, "get_doc") as get_doc,
		):
			sync._insert_item_price("Standard Selling", 12.5)
		return get_doc

	def test_a_price_list_without_a_price_takes_the_products(self):
		get_doc = self.insert_price(existing_price=None)

		self.assertEqual(get_doc.call_args.args[0]["price_list_rate"], 12.5)
		# Not pushed straight back to the shop it came from
		self.assertTrue(get_doc.return_value.flags.created_by_sync)
		get_doc.return_value.insert.assert_called_once()

	def test_a_price_list_with_a_price_keeps_it(self):
		self.insert_price(existing_price="PRICE-1").assert_not_called()

	def test_a_variation_without_its_parent_is_not_created(self):
		product = make_product(type="variation", parent_id=5, attributes="[]")
		sync = make_sync(woocommerce_product=product)

		with (
			patch.object(SynchroniseItem, "create_or_update_item_attributes"),
			patch.object(SynchroniseItem, "_get_or_create_parent_item", return_value=None),
			self.assertRaises(frappe.ValidationError),
		):
			sync._handle_product_variants(frappe.new_doc("Item"))  # type: ignore


class TestItemOfTheSku(IntegrationTestCase):
	def find(self, sku="MOWER-1", has_variants=False, disabled=(), templates=(), linked=()):
		"""
		find_item_by_sku when an Item has the SKU as its code and another as its barcode. `linked` are linked
		to a product of the server already.
		"""

		def exists(doctype, filters):
			# An Item's name, as the Item has it
			if doctype == "Item":
				return filters
			return filters["parent"] in linked

		def get_value(doctype, item_code, fields, as_dict=False):
			return _dict(disabled=int(item_code in disabled), has_variants=int(item_code in templates))

		with (
			patch.object(frappe.db, "exists", side_effect=exists),
			patch.object(frappe, "get_all", return_value=["BARCODED"]),
			patch.object(frappe.db, "get_value", side_effect=get_value),
		):
			return find_item_by_sku("one.example.com", sku, has_variants=has_variants)

	def test_the_item_of_the_code_comes_before_the_one_of_the_barcode(self):
		self.assertEqual(self.find(sku=" MOWER-1 "), "MOWER-1")
		self.assertEqual(self.find(disabled={"MOWER-1"}), "BARCODED")
		self.assertEqual(self.find(linked={"MOWER-1"}), "BARCODED")
		self.assertIsNone(self.find(linked={"MOWER-1", "BARCODED"}))

	def test_a_template_only_for_a_variable_product(self):
		self.assertEqual(self.find(templates={"MOWER-1"}), "BARCODED")
		self.assertEqual(self.find(has_variants=True, templates={"BARCODED"}), "BARCODED")
		self.assertIsNone(self.find(has_variants=True))

	def test_nothing_without_a_sku(self):
		self.assertIsNone(find_item_by_sku("one.example.com", " "))


class TestItemCodeTaken(IntegrationTestCase):
	def create(self, name_by, found=None, **product):
		"""
		create_item, with all but the choice between linking an Item and making one mocked
		"""
		sync = make_sync(woocommerce_product=make_product(sku="MOWER-1", **product))
		server = _dict(name="one.example.com", name_by=name_by)
		item = MagicMock(woocommerce_servers=[_dict(idx=1, woocommerce_server="one.example.com")])
		with (
			patch.object(frappe, "get_cached_doc", return_value=server),
			patch.object(sync_items, "find_item_by_sku", return_value=found) as find_item_by_sku,
			patch.object(SynchroniseItem, "link_item") as link_item,
			patch.object(frappe, "new_doc", return_value=item),
			patch.object(SynchroniseItem, "_handle_product_variants"),
			patch.object(SynchroniseItem, "_set_core_item_fields"),
			patch.object(SynchroniseItem, "_set_item_tax_template"),
			patch.object(SynchroniseItem, "set_item_fields", return_value=(False, item)),
			patch.object(SynchroniseItem, "create_item_prices"),
			patch.object(SynchroniseItem, "set_sync_hash"),
		):
			sync.create_item()
		return find_item_by_sku, link_item, item

	def test_a_product_is_linked_to_the_item_of_its_sku(self):
		find_item_by_sku, link_item, item = self.create("Product SKU", found="MOWER-1", type="simple")

		find_item_by_sku.assert_called_once_with("one.example.com", "MOWER-1", has_variants=False)
		self.assertEqual(link_item.call_args.args[0], "MOWER-1")
		item.insert.assert_not_called()

	def test_a_variable_product_is_linked_to_a_template(self):
		find_item_by_sku, *_ = self.create("Product SKU", type="variable")

		self.assertTrue(find_item_by_sku.call_args.kwargs["has_variants"])

	def test_without_an_item_of_the_sku_one_is_made(self):
		_find_item_by_sku, link_item, item = self.create("Product SKU", type="simple")

		link_item.assert_not_called()
		item.insert.assert_called_once()

	def test_items_named_by_id_are_not_linked_by_sku(self):
		find_item_by_sku, link_item, item = self.create("WooCommerce ID", found="MOWER-1", type="simple")

		find_item_by_sku.assert_not_called()
		link_item.assert_not_called()
		item.insert.assert_called_once()

	def test_a_linked_item_takes_the_prices_it_lacks(self):
		sync = make_sync(woocommerce_product=make_product(sku="MOWER-1"))
		server = _dict(name="one.example.com")
		item = MagicMock(flags=_dict())
		item.append.return_value = _dict(idx=2)
		with (
			patch.object(frappe, "get_doc", return_value=item),
			patch.object(SynchroniseItem, "create_item_prices") as create_item_prices,
			patch.object(SynchroniseItem, "set_sync_hash") as set_sync_hash,
		):
			sync.link_item("MOWER-1", server)  # type: ignore

		item.append.assert_called_once_with(
			"woocommerce_servers",
			{"woocommerce_server": "one.example.com", "woocommerce_id": "1", "enable_sync": 1},
		)
		item.save.assert_called_once()
		self.assertTrue(item.flags.created_by_sync)
		self.assertEqual((sync.item.item, sync.item.item_woocommerce_server_idx), (item, 2))  # type: ignore
		create_item_prices.assert_called_once_with(server)
		set_sync_hash.assert_called_once()

	def test_a_taken_code_gets_a_number(self):
		for name_by, sku, code in (
			("WooCommerce ID", "MOWER-1", "1"),
			("Product SKU", " MOWER-1 ", "MOWER-1"),
			("Product SKU", "", "1"),
		):
			item = MagicMock()
			sync = make_sync(woocommerce_product=make_product(sku=sku))
			server = _dict(name="one.example.com", name_by=name_by, uom="Nos", item_group="Products")
			with patch.object(
				sync_items, "append_number_if_name_exists", return_value=f"{code}-1"
			) as append_number_if_name_exists:
				sync._set_core_item_fields(item, server)  # type: ignore

			append_number_if_name_exists.assert_called_once_with("Item", code)
			self.assertEqual(item.item_code, f"{code}-1")


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


class TestItemTaxTemplate(IntegrationTestCase):
	def server(self, **values):
		return frappe.get_doc({"doctype": "WooCommerce Server", **values})

	def net_server(self, **values):
		return self.server(
			**values,
			tax_class_map=[
				{"woocommerce_tax_class": "standard", "item_tax_template": "VAT 23%"},
				{"woocommerce_tax_class": "zerowa-stawka", "item_tax_template": "VAT 0%"},
				{"woocommerce_tax_class": "obnizona-stawka"},
			],
		)

	def set_template(self, server, item=None, **product_values):
		item = item or frappe.new_doc("Item")
		sync = make_sync(woocommerce_product=make_product(type="simple", **product_values))
		changed = sync._set_item_tax_template(item, server)
		return item, changed

	def templates(self, item):
		return [(row.item_tax_template, row.tax_category or None) for row in item.taxes]

	def test_items_get_the_template_of_their_class(self):
		server = self.net_server()
		# WooCommerce leaves the standard class empty
		self.assertEqual(self.templates(self.set_template(server, tax_class="")[0]), [("VAT 23%", None)])
		self.assertEqual(
			self.templates(self.set_template(server, tax_class="zerowa-stawka")[0]), [("VAT 0%", None)]
		)

	def test_a_class_without_a_template_gets_the_default_one(self):
		server = self.net_server(default_item_tax_template="VAT 8%")
		self.assertEqual(
			self.templates(self.set_template(server, tax_class="obnizona-stawka")[0]), [("VAT 8%", None)]
		)
		# A class that isn't in the map at all
		self.assertEqual(self.templates(self.set_template(server, tax_class="nowa")[0]), [("VAT 8%", None)])

	def test_without_any_template_the_item_is_left(self):
		item, changed = self.set_template(self.net_server(), tax_class="obnizona-stawka")
		self.assertFalse(changed)
		self.assertEqual(item.taxes, [])

	def test_only_the_row_without_a_tax_category_is_set(self):
		item = frappe.new_doc("Item")
		item.append("taxes", {"item_tax_template": "VAT 8%", "tax_category": "Export"})
		item.append("taxes", {"item_tax_template": "VAT 8%"})

		item, changed = self.set_template(self.net_server(), item=item, tax_class="")

		self.assertTrue(changed)
		self.assertEqual(self.templates(item), [("VAT 8%", "Export"), ("VAT 23%", None)])
		self.assertFalse(self.set_template(self.net_server(), item=item, tax_class="")[1])

	def test_a_variation_in_its_parents_class_gets_the_parents_template(self):
		item = frappe.new_doc("Item")
		item.variant_of = "MOWER"
		parent = _dict(taxes=[_dict(item_tax_template="VAT 0%", tax_category=None)])

		with patch.object(frappe, "get_cached_doc", return_value=parent):
			item, _changed = self.set_template(self.net_server(), item=item, tax_class="parent")

		self.assertEqual(self.templates(item), [("VAT 0%", None)])
