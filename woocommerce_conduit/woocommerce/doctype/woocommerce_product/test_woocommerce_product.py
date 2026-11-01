# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.database.utils import DefaultOrderBy
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_conduit.woocommerce.woocommerce_api import parse_order_by, sort_records

TABLE = "`tabWooCommerce Product`"


class TestWooCommerceProductFields(IntegrationTestCase):
	def wc_fields(self, *frappe_fields: str) -> set[str]:
		return set(WooCommerceProduct.get_wc_fields(list(frappe_fields)).split(","))

	def test_only_the_requested_fields_are_asked_for(self):
		fields = self.wc_fields(f"{TABLE}.`sku`", f"{TABLE}.`status`")

		self.assertIn("sku", fields)
		self.assertIn("status", fields)
		self.assertNotIn("description", fields)
		self.assertNotIn("images", fields)

	def test_the_fields_needed_to_identify_a_product_are_always_asked_for(self):
		fields = self.wc_fields(f"{TABLE}.`sku`")

		self.assertTrue({"id", "name", "type", "parent_id", "date_created", "date_modified"} <= fields)

	def test_frappe_fieldnames_are_translated_to_woocommerce_ones(self):
		fields = self.wc_fields(f"{TABLE}.`woocommerce_name`", f"{TABLE}.`modified`", f"{TABLE}.`length`")

		self.assertIn("name", fields)
		self.assertIn("date_modified", fields)
		self.assertIn("dimensions", fields)
		self.assertNotIn("woocommerce_name", fields)
		self.assertNotIn("length", fields)

	def test_aliases_are_understood(self):
		self.assertIn("sku", self.wc_fields(f"{TABLE}.`sku` as code"))

	def test_fields_that_are_derived_on_our_side_are_not_asked_for(self):
		fields = self.wc_fields(f"{TABLE}.`woocommerce_server`", "count(*)")

		self.assertNotIn("woocommerce_server", fields)
		self.assertNotIn("count(*)", fields)

	def test_all_fields_are_asked_for_without_a_selection(self):
		for selection in (None, ["*"]):
			fields = set(WooCommerceProduct.get_wc_fields(selection).split(","))

			self.assertTrue({"description", "images", "attributes", "dimensions", "regular_price"} <= fields)

	def test_a_loaded_product_has_its_meta_data_categories_tags_and_gtin(self):
		# What the Item Field Map can reach, as it runs on the product as the doctype has it
		fields = set(WooCommerceProduct.get_wc_fields().split(","))

		self.assertTrue({"meta_data", "categories", "tags", "brands", "global_unique_id"} <= fields)

	def test_json_fields_are_indented_for_the_form(self):
		with patch.object(WooCommerceProduct, "_init_api", return_value=[]):
			product = frappe.new_doc("WooCommerce Product")
		dimensions = {"length": "", "width": "", "height": ""}
		record = product.after_load_from_db(
			{"dimensions": dimensions, "meta_data": '[{"key": "_gtin", "value": "590"}]', "tags": "[]"}
		)

		self.assertEqual(record["meta_data"], '[\n  {\n    "key": "_gtin",\n    "value": "590"\n  }\n]')
		self.assertEqual(record["tags"], "[]")

	def test_extra_fields_can_be_asked_for(self):
		fields = WooCommerceProduct.get_wc_fields([f"{TABLE}.`sku`"], ["attributes"]).split(",")

		self.assertIn("attributes", fields)


class TestWooCommerceProductListView(IntegrationTestCase):
	def test_frappe_can_call_get_count_and_get_stats(self):
		"""Frappe calls these as `controller.get_count(args=args, **args)`"""
		args = frappe._dict(doctype="WooCommerce Product", filters=[], fields=[])

		with patch.object(WooCommerceProduct, "get_count_of_records", return_value=7):
			self.assertEqual(frappe.call(WooCommerceProduct.get_count, args=args, **args), 7)

		self.assertIsNone(frappe.call(WooCommerceProduct.get_stats, args=args, **args))


class TestOrderBy(IntegrationTestCase):
	def test_order_by_as_sent_by_the_list_view_is_parsed(self):
		self.assertEqual(
			parse_order_by(f"{TABLE}.`modified` desc, sku asc, `tabWooCommerce Product`.`status`"),
			[("modified", True), ("sku", False), ("status", False)],
		)
		self.assertEqual(parse_order_by(None), [])

	def test_the_default_order_of_frappe_is_no_order(self):
		self.assertEqual(parse_order_by(DefaultOrderBy), [])

	def test_only_fields_woocommerce_can_order_by_are_translated(self):
		self.assertEqual(WooCommerceProduct.get_wc_orderby("woocommerce_name"), "title")
		self.assertEqual(WooCommerceProduct.get_wc_orderby("woocommerce_id"), "id")
		self.assertEqual(WooCommerceProduct.get_wc_orderby("price"), "price")
		self.assertEqual(WooCommerceProduct.get_wc_orderby("modified"), "modified")
		self.assertIsNone(WooCommerceProduct.get_wc_orderby("sku"))

	def test_records_are_sorted_on_the_first_clause_then_the_next(self):
		records = [
			{"sku": "b", "price": "10"},
			{"sku": "a", "price": "10"},
			{"sku": "c", "price": "2"},
		]

		ordered = sort_records(records, [("price", True), ("sku", False)])

		self.assertEqual([record["sku"] for record in ordered], ["a", "b", "c"])

	def test_numbers_are_sorted_as_numbers_and_missing_values_come_last(self):
		records = [{"id": "10"}, {"id": "9"}, {"id": None}, {"id": "100"}]

		ordered = sort_records(records, [("id", False)])

		self.assertEqual([record["id"] for record in ordered], ["9", "10", "100", None])

	def test_a_field_the_records_do_not_have_is_ignored(self):
		records = [{"sku": "b"}, {"sku": "a"}]

		self.assertEqual(sort_records(records, [("unknown", False)]), records)

	def test_names_are_ordered_by_their_id(self):
		records = [{"name": f"one.example.com~{i}", "id": i} for i in (10, 9, 100)]

		ordered = sort_records(records, [("name", False)])

		self.assertEqual([record["id"] for record in ordered], [9, 10, 100])

	def test_text_is_ordered_regardless_of_case(self):
		records = [{"woocommerce_name": name} for name in ("banana", "Cherry", "apple")]

		ordered = sort_records(records, [("woocommerce_name", False)])

		self.assertEqual([record["woocommerce_name"] for record in ordered], ["apple", "banana", "Cherry"])


class FakeWooCommerceAPI:
	def __init__(self, server: str, products: list[dict]):
		self.woocommerce_server = server
		self.woocommerce_server_url = f"https://{server}"
		self.products = products
		self.requests = []

	def get(self, endpoint, params=None, **kwargs):
		self.requests.append(params)
		return MagicMock(
			status_code=200,
			headers={"x-wp-total": str(len(self.products))},
			json=lambda: list(self.products),
		)


class TestListRequests(IntegrationTestCase):
	@staticmethod
	def product(product_id: int, name: str) -> dict:
		return {
			"id": product_id,
			"name": name,
			"type": "simple",
			"sku": f"sku-{product_id}",
			"status": "publish",
			"date_created": "2025-01-01T10:00:00",
			"date_modified": "2025-01-02T10:00:00",
		}

	def get_list(self, apis: list[FakeWooCommerceAPI], **args):
		args = {"doctype": "WooCommerce Product", "page_length": 20, **args}
		with patch.object(WooCommerceProduct, "_init_api", return_value=apis):
			return WooCommerceProduct.get_list_of_records(args)

	def test_only_requested_fields_and_the_ordering_are_sent_to_woocommerce(self):
		api = FakeWooCommerceAPI("one.example.com", [self.product(1, "A")])

		self.get_list([api], fields=[f"{TABLE}.`sku`"], order_by=f"{TABLE}.`woocommerce_id` desc")

		params = api.requests[0]
		self.assertEqual((params["orderby"], params["order"]), ("id", "desc"))
		self.assertIn("sku", params["_fields"].split(","))
		self.assertNotIn("description", params["_fields"].split(","))

	def test_a_field_woocommerce_cannot_order_by_asks_for_the_newest_first(self):
		api = FakeWooCommerceAPI("one.example.com", [self.product(1, "A")])

		self.get_list([api], order_by=f"{TABLE}.`sku` asc")

		params = api.requests[0]
		self.assertEqual((params["orderby"], params["order"]), ("date", "desc"))

	def test_the_field_a_list_is_ordered_by_is_asked_for_too(self):
		api = FakeWooCommerceAPI("one.example.com", [self.product(1, "A")])

		self.get_list([api], fields=[f"{TABLE}.`sku`"], order_by=f"{TABLE}.`price` desc")

		self.assertIn("price", api.requests[0]["_fields"].split(","))

	def test_every_server_is_listed_and_the_result_is_ordered_as_a_whole(self):
		apis = [
			FakeWooCommerceAPI("one.example.com", [self.product(1, "A"), self.product(5, "E")]),
			FakeWooCommerceAPI("two.example.com", [self.product(3, "C"), self.product(7, "G")]),
		]

		products = self.get_list(apis, order_by=f"{TABLE}.`woocommerce_id` asc")

		self.assertEqual([product["woocommerce_id"] for product in products], [1, 3, 5, 7])
		self.assertEqual(
			{product["woocommerce_server"] for product in products}, {"one.example.com", "two.example.com"}
		)


class FakeWritableWooCommerceAPI(FakeWooCommerceAPI):
	def __init__(self, server: str):
		super().__init__(server, [])
		self.puts = []

	def put(self, endpoint, data, **kwargs):
		self.puts.append((endpoint, data))
		return MagicMock(status_code=200, json=lambda: {"id": 1, "date_modified": "2025-02-01T10:00:00"})


class TestProductWrites(IntegrationTestCase):
	@staticmethod
	def record(**values) -> dict:
		return {
			"doctype": "WooCommerce Product",
			"name": "one.example.com~1",
			"woocommerce_server": "one.example.com",
			"woocommerce_id": "1",
			"woocommerce_name": "Mug",
			"type": "simple",
			"regular_price": "10",
			"sale_price": "",
			"weight": "1",
			"length": "10",
			"width": "5",
			"height": "8",
			"description": "<p>A mug</p>",
			"images": '[{"src": "https://one.example.com/mug.jpg"}]',
			"attributes": "[]",
			"woocommerce_date_modified": "2025-01-02T10:00:00",
			**values,
		}

	def save(self, before: dict, **changes):
		api = FakeWritableWooCommerceAPI("one.example.com")
		with patch.object(WooCommerceProduct, "_init_api", return_value=[api]):
			product: WooCommerceProduct = frappe.get_doc(self.record(**before))  # type: ignore
			product._doc_before_save = frappe.get_doc(self.record(**before))
			product.update(changes)
			product.db_update()
		return product, api

	def test_only_changed_prices_and_stock_are_sent(self):
		product, api = self.save({}, regular_price=12.5, stock_quantity="4")

		self.assertEqual(api.puts, [("products/1", {"regular_price": "12.5", "stock_quantity": 4})])
		self.assertEqual(product.woocommerce_date_modified, "2025-02-01T10:00:00")

	def test_the_shops_own_fields_are_never_sent(self):
		_product, api = self.save(
			{},
			woocommerce_name="Big Mug",
			description="<p>Another mug</p>",
			sku="MUG-1",
			width=6,
			images="[]",
			price=99,
		)

		self.assertEqual(api.puts, [])

	def test_nothing_is_sent_when_only_the_representation_differs(self):
		_product, api = self.save({}, regular_price=10.0, sale_price=0)

		self.assertEqual(api.puts, [])

	def test_a_sale_price_of_zero_clears_the_sale(self):
		_product, api = self.save({"sale_price": "8"}, sale_price=0)

		self.assertEqual(api.puts[0][1], {"sale_price": ""})

	def test_variations_are_written_through_their_parent(self):
		_product, api = self.save({"type": "variation", "parent_id": 7}, regular_price=11)

		self.assertEqual(api.puts[0][0], "products/7/variations/1")

	def test_a_variable_products_price_is_not_sent(self):
		_product, api = self.save({"type": "variable"}, regular_price=20, stock_quantity=3)

		self.assertEqual(api.puts[0][1], {"stock_quantity": 3})

	def test_long_names_and_unknown_statuses_do_not_block_a_price_update(self):
		product, _api = self.save({})
		product.woocommerce_name = "M" * 200
		product.status = "future"

		product._validate_length()
		product._validate_selects()
