# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

import re
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks import product_push
from woocommerce_conduit.tasks.product_push import push_to_products
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)

SERVER = "one.example.com"
ERROR_TITLE = "WooCommerce Error: Stock Sync"


def product(product_id: int, product_type="simple", parent_id=0, **values) -> dict:
	return {
		"id": product_id,
		"name": f"Product {product_id}",
		"type": product_type,
		"parent_id": parent_id,
		"date_created": "2026-10-01T10:00:00",
		"date_modified": "2026-10-02T10:00:00",
		"date_modified_gmt": "2026-10-02T09:00:00",
		"regular_price": "10.00",
		"manage_stock": True,
		"stock_quantity": 5,
		**values,
	}


def stock(quantity: int) -> dict:
	return {"manage_stock": 1, "stock_quantity": quantity}


class Shop:
	"""
	A shop with these products and the variations of each parent, which honours `include` and answers a
	batch per update as WooCommerce does, refusing the ids in `refused`. An endpoint in `failing` answers
	500. `requests` holds each request as (method, endpoint, its params or body).
	"""

	def __init__(self, products=(), variations=None, refused=(), failing=()):
		self.woocommerce_server = SERVER
		self.woocommerce_server_url = f"https://{SERVER}"
		self.products = {record["id"]: record for record in products}
		self.variations = {
			parent_id: {record["id"]: record for record in records}
			for parent_id, records in (variations or {}).items()
		}
		self.refused = set(refused)
		self.failing = set(failing)
		self.requests = []

	def records(self, endpoint: str) -> dict:
		if match := re.fullmatch(r"products/(\d+)/variations", endpoint):
			return self.variations.get(int(match[1]), {})
		return self.products

	def get(self, endpoint, params=None, **kwargs):
		params = dict(params or {})
		self.requests.append(("GET", endpoint, params))
		if endpoint in self.failing:
			return MagicMock(status_code=500, text="boom")
		records = self.records(endpoint)
		page = [dict(records[int(i)]) for i in params["include"].split(",") if int(i) in records]
		return MagicMock(status_code=200, headers={"x-wp-total": str(len(page))}, json=lambda: page)

	def post(self, endpoint, data, **kwargs):
		self.requests.append(("POST", endpoint, data))
		if endpoint in self.failing:
			return MagicMock(status_code=500, text="boom", request=MagicMock(url=endpoint, body=None))
		records = self.records(endpoint.removesuffix("/batch"))
		answers = []
		for update in data["update"]:
			if update["id"] in self.refused or update["id"] not in records:
				answers.append({"id": update["id"], "error": {"code": "woocommerce_rest_invalid_id"}})
			else:
				records[update["id"]].update(update)
				answers.append(dict(records[update["id"]]))
		return MagicMock(status_code=200, json=lambda: {"update": answers})

	def sent(self, method: str) -> list[tuple[str, list]]:
		return [
			(endpoint, body["update"] if method == "POST" else body["include"])
			for sent_method, endpoint, body in self.requests
			if sent_method == method
		]


class TestPushToProducts(IntegrationTestCase):
	def push(self, shop: Shop, values: dict, parents=None, on_their_own=(), delay=0) -> int:
		"""
		Push the values to the shop's products. ERPNext knows the variations in `parents` by their parent,
		and the products in `on_their_own` are the ones that load_wc_product finds.
		"""

		def load_wc_product(name):
			product_id = int(name.split("~")[1])
			record = next((record for record in on_their_own if record["id"] == product_id), None)
			if not record:
				raise frappe.DoesNotExistError
			return frappe.get_doc(
				WooCommerceProduct.pre_init_document(dict(record), shop.woocommerce_server_url)
			)

		with (
			patch.object(WooCommerceProduct, "_init_api", return_value=[shop]),
			patch.object(product_push, "get_variation_parents", return_value=parents or {}),
			patch.object(product_push, "load_wc_product", side_effect=load_wc_product) as load,
			patch.object(frappe, "log_error") as log_error,
		):
			written = push_to_products(SERVER, values, ERROR_TITLE, delay_per_product=delay)
		self.load_wc_product, self.log_error = load, log_error
		return written

	def logged(self) -> list[str]:
		"""
		The messages of the Error Logs with the push's own title
		"""
		return [call.args[1] for call in self.log_error.call_args_list if call.args[0] == ERROR_TITLE]

	def test_only_what_differs_is_written_under_each_products_id(self):
		shop = Shop([product(12), product(13), product(14, stock_quantity=3)])

		written = self.push(shop, {"12": stock(5), "13": stock(7), "14": stock(0)})

		self.assertEqual(written, 2)
		self.assertEqual(shop.sent("GET"), [("products", "12,13,14")])
		self.assertEqual(
			shop.sent("POST"),
			[("products/batch", [{"id": 13, "stock_quantity": 7}, {"id": 14, "stock_quantity": 0}])],
		)
		# Only the fields a push writes are read
		fields = shop.requests[0][2]["_fields"].split(",")
		self.assertIn("stock_quantity", fields)
		self.assertNotIn("meta_data", fields)

	def test_variations_are_read_and_written_through_their_parent(self):
		shop = Shop(
			[product(12)],
			variations={7: [product(71, "variation", parent_id=7), product(72, "variation", parent_id=7)]},
		)

		self.push(shop, {"71": stock(4), "72": stock(5), "12": stock(9)}, parents={"71": "7", "72": "7"})

		self.assertEqual(shop.sent("GET"), [("products/7/variations", "71,72"), ("products", "12")])
		self.assertEqual(
			shop.sent("POST"),
			[
				("products/7/variations/batch", [{"id": 71, "stock_quantity": 4}]),
				("products/batch", [{"id": 12, "stock_quantity": 9}]),
			],
		)

	def test_a_product_its_list_leaves_out_is_read_on_its_own(self):
		# A variation whose Item isn't a variant: ERPNext doesn't know its parent
		variation = product(81, "variation", parent_id=8)
		shop = Shop(variations={8: [variation]})

		written = self.push(shop, {"81": stock(2), "82": stock(1)}, on_their_own=[variation])

		self.assertEqual(written, 1)
		self.assertEqual(
			[call.args[0] for call in self.load_wc_product.call_args_list],
			["one.example.com~81", "one.example.com~82"],
		)
		self.assertEqual(
			shop.sent("POST"), [("products/8/variations/batch", [{"id": 81, "stock_quantity": 2}])]
		)
		# The other one has been deleted
		self.assertEqual(len(self.logged()), 1)
		self.assertIn("one.example.com~82", self.logged()[0])

	def test_a_product_woocommerce_refuses_is_logged_and_the_others_are_written(self):
		shop = Shop([product(12), product(13)], refused={13})

		written = self.push(shop, {"12": stock(1), "13": stock(2)})

		self.assertEqual(written, 1)
		self.assertEqual(len(self.logged()), 1)
		self.assertIn("one.example.com~13", self.logged()[0])
		self.assertIn("woocommerce_rest_invalid_id", self.logged()[0])

	def test_a_failed_request_is_logged_and_the_next_one_still_goes(self):
		shop = Shop(
			[product(12)],
			variations={7: [product(71, "variation", parent_id=7)]},
			failing={"products/7/variations/batch"},
		)

		written = self.push(shop, {"71": stock(4), "12": stock(9)}, parents={"71": "7"})

		self.assertEqual(written, 1)
		self.assertEqual(
			[endpoint for endpoint, _updates in shop.sent("POST")],
			["products/7/variations/batch", "products/batch"],
		)
		self.assertEqual(len(self.logged()), 1)
		self.assertIn("one.example.com~71", self.logged()[0])

	def test_products_the_shop_wont_list_are_logged_once_and_not_asked_for_again(self):
		shop = Shop([product(12), product(13)], failing={"products"})

		written = self.push(shop, {"12": stock(1), "13": stock(2)})

		self.assertEqual(written, 0)
		self.assertEqual(len(shop.sent("GET")), 1)
		self.assertEqual(shop.sent("POST"), [])
		self.load_wc_product.assert_not_called()
		self.assertEqual(len(self.logged()), 1)
		self.assertIn("one.example.com~12, one.example.com~13", self.logged()[0])

	def test_a_hundred_products_are_read_and_written_a_request(self):
		shop = Shop([product(product_id) for product_id in range(1, 251)])

		written = self.push(shop, {str(product_id): stock(0) for product_id in range(1, 251)})

		self.assertEqual(written, 250)
		self.assertEqual([len(ids.split(",")) for _endpoint, ids in shop.sent("GET")], [100, 100, 50])
		self.assertEqual([len(updates) for _endpoint, updates in shop.sent("POST")], [100, 100, 50])

	def test_nothing_is_sent_to_a_product_that_has_the_values(self):
		shop = Shop([product(12), product(13, "variable", regular_price="")])

		# A variable product's prices live on its variations
		written = self.push(shop, {"12": stock(5), "13": {"regular_price": 15.0}})

		self.assertEqual(written, 0)
		self.assertEqual(shop.sent("POST"), [])

	def test_the_delay_is_for_each_product_written(self):
		shop = Shop([product(12), product(13), product(14)], refused={14})

		with (
			patch.object(frappe, "in_test", False),
			patch.object(product_push.time, "sleep") as sleep,
		):
			self.push(shop, {"12": stock(1), "13": stock(2), "14": stock(3)}, delay=0.5)

		sleep.assert_called_once_with(1.0)
