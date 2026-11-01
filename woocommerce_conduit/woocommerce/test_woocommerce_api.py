import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase
from woocommerce import API

from woocommerce_conduit.woocommerce import woocommerce_api
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	WooCommerceAPI,
	clear_api_clients,
	get_api_clients,
)

DATES = {"date_created": "2025-01-01T10:00:00", "date_modified": "2025-01-02T10:00:00"}


class PagedAPI:
	"""
	A server with `count` orders, which honours per_page and offset like WooCommerce
	"""

	def __init__(self, server: str, count: int):
		self.woocommerce_server = server
		self.woocommerce_server_url = f"https://{server}"
		self.records = [{"id": i, **DATES} for i in range(1, count + 1)]
		self.requests = []

	def get(self, endpoint, params=None, **kwargs):
		params = dict(params or {})
		self.requests.append(params)
		offset = params.get("offset", 0)
		page = self.records[offset : offset + params.get("per_page", 10)]
		return MagicMock(
			status_code=200,
			headers={"x-wp-total": str(len(self.records))},
			json=lambda: [dict(record) for record in page],
		)


class TestPaging(IntegrationTestCase):
	def get_list(self, apis: list[PagedAPI], **args) -> list[str]:
		with patch.object(WooCommerceOrder, "_init_api", return_value=apis):
			records = WooCommerceOrder.get_list_of_records({"fields": ["name"], **args})
		return [record["name"] for record in records]

	def test_a_page_never_has_more_records_than_asked_for(self):
		one, two = PagedAPI("one.example.com", 15), PagedAPI("two.example.com", 15)

		names = self.get_list([one, two], page_length=10)

		self.assertEqual(names, [f"one.example.com~{i}" for i in range(1, 11)])
		# The page was full after the first server
		self.assertEqual(two.requests, [])

	def test_a_server_before_the_offset_is_skipped_by_its_total(self):
		one, two = PagedAPI("one.example.com", 15), PagedAPI("two.example.com", 15)

		names = self.get_list([one, two], page_length=10, start=20)

		self.assertEqual(names, [f"two.example.com~{i}" for i in range(6, 16)])
		self.assertEqual(len(one.requests), 1)

	def test_a_page_runs_on_into_the_next_server(self):
		one, two = PagedAPI("one.example.com", 15), PagedAPI("two.example.com", 15)

		names = self.get_list([one, two], page_length=10, start=10)

		self.assertEqual(
			names,
			[f"one.example.com~{i}" for i in range(11, 16)] + [f"two.example.com~{i}" for i in range(1, 6)],
		)

	def test_more_than_one_request_holds_is_fetched_page_by_page(self):
		one = PagedAPI("one.example.com", 250)

		names = self.get_list([one], page_length=230)

		self.assertEqual(len(names), 230)
		self.assertEqual([params["per_page"] for params in one.requests], [100, 100, 30])
		self.assertEqual([params.get("offset") for params in one.requests], [None, 100, 200])

	def test_only_the_servers_asked_for_are_read(self):
		one, two = PagedAPI("one.example.com", 3), PagedAPI("two.example.com", 3)

		names = self.get_list([one, two], servers=["two.example.com"])

		self.assertEqual(names, [f"two.example.com~{i}" for i in range(1, 4)])
		self.assertEqual(one.requests, [])


class TestVariations(IntegrationTestCase):
	def test_variations_are_asked_of_their_parents_server_only(self):
		parent = {
			"id": 7,
			"type": "variable",
			"woocommerce_server": "one.example.com",
			"woocommerce_name": "Mug",
		}
		settings = _dict(fetch_variations=1, max_variations=50, variation_batch_size=20)
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(WooCommerceProduct, "get_list_of_records", side_effect=[[parent], []]) as get_list,
		):
			WooCommerceProduct.get_list({"filters": [["WooCommerce Product", "status", "=", "publish"]]})

		args = get_list.call_args_list[1].args[0]
		self.assertEqual(args["servers"], ["one.example.com"])
		self.assertEqual(args["endpoint"], "products/7/variations")
		self.assertNotIn("filters", args)
		self.assertEqual((args["page_length"], args["per_page"]), (50, 20))

	def test_a_variation_has_its_image(self):
		record = WooCommerceProduct.pre_init_document(
			{"id": 9, "parent_id": 7, "image": {"src": "https://one.example.com/red.jpg"}, **DATES},
			woocommerce_server_url="https://one.example.com",
		)

		self.assertEqual(record["image"], "https://one.example.com/red.jpg")
		self.assertEqual(json.loads(record["images"]), [{"src": "https://one.example.com/red.jpg"}])


class TestLoading(IntegrationTestCase):
	def load(self, response):
		api = MagicMock(
			woocommerce_server="one.example.com", woocommerce_server_url="https://one.example.com"
		)
		api.get.return_value = response
		with (
			patch.object(WooCommerceOrder, "_init_api", return_value=[api]),
			patch.object(frappe, "log_error") as log_error,
		):
			try:
				frappe.get_doc("WooCommerce Order", "one.example.com~12")
			finally:
				self.log_error = log_error

	def test_a_missing_record_is_not_found_and_not_an_error(self):
		with self.assertRaises(frappe.DoesNotExistError):
			self.load(MagicMock(status_code=404))
		self.log_error.assert_not_called()

	def test_a_failed_request_is_logged_once(self):
		with self.assertRaises(frappe.ValidationError):
			self.load(MagicMock(status_code=500, text="boom", request=MagicMock(url="u", body=None)))
		self.log_error.assert_called_once()


class TestChanges(IntegrationTestCase):
	def test_values_are_compared_by_value(self):
		with patch.object(WooCommerceProduct, "_init_api", return_value=[]):
			product = frappe.get_doc(
				{
					"doctype": "WooCommerce Product",
					"name": "one.example.com~1",
					"regular_price": "10",
					"sale_price": "",
					"stock_quantity": 3,
					"date_on_sale_from": None,
				}
			)

		changes = product.get_changes(
			{"regular_price": 10.0, "sale_price": 0, "date_on_sale_from": "", "stock_quantity": 5}
		)

		self.assertEqual(changes, {"stock_quantity": 5})


class TestApiClients(IntegrationTestCase):
	def test_clients_are_built_once_per_request(self):
		server = _dict(
			name="one.example.com", woocommerce_server_url="https://one.example.com", log_all_requests=0
		)
		clear_api_clients()
		try:
			with (
				patch.object(frappe, "get_all", return_value=[server]) as get_all,
				patch.object(woocommerce_api, "get_decrypted_password", return_value="secret"),
			):
				first, second = get_api_clients(), get_api_clients()
				clear_api_clients()
				third = get_api_clients()
		finally:
			clear_api_clients()

		self.assertIs(first, second)
		self.assertIsNot(first, third)
		self.assertEqual(get_all.call_count, 2)
		self.assertEqual(first[0].woocommerce_server, "one.example.com")


def make_response(status_code: int, text: str = "{}") -> MagicMock:
	return MagicMock(
		status_code=status_code,
		ok=status_code < 400,
		reason="Not Found" if status_code == 404 else "OK",
		text=text,
		elapsed=timedelta(seconds=0.25),
	)


class TestRequestLog(IntegrationTestCase):
	def request(self, response=None, error=None, **client) -> MagicMock:
		"""
		Make a GET with the client, and return the deferred_insert that the log went through
		"""
		api = WooCommerceAPI(
			"https://shop.example.com", "ck", "cs", woocommerce_server="shop.example.com", **client
		)
		with (
			patch.object(API, "_API__request", return_value=response, side_effect=error),
			patch.object(woocommerce_api, "deferred_insert") as deferred_insert,
			patch.object(frappe, "in_test", False),
		):
			try:
				api.get("orders/12", params={"_fields": "id"})
			except ConnectionError:
				pass
		return deferred_insert

	def logged(self, deferred_insert: MagicMock) -> dict:
		deferred_insert.assert_called_once()
		doctype, records = deferred_insert.call_args.args
		self.assertEqual(doctype, "Integration Request")
		return records[0]

	def test_a_successful_request_is_not_logged(self):
		self.request(make_response(200)).assert_not_called()

	def test_a_failed_request_is_logged_against_its_server(self):
		log = self.logged(self.request(make_response(404, '{"code": "not_found"}')))

		self.assertEqual(log["status"], "Failed")
		self.assertEqual(log["integration_request_service"], "WooCommerce")
		self.assertEqual(log["request_description"], "GET orders/12 (0.25 s)")
		self.assertEqual(log["url"], "https://shop.example.com/wp-json/wc/v3/orders/12")
		self.assertEqual(log["error"], "404 Not Found")
		self.assertEqual(log["output"], '{"code": "not_found"}')
		self.assertEqual(
			(log["reference_doctype"], log["reference_docname"]), ("WooCommerce Server", "shop.example.com")
		)

	def test_a_request_that_raises_is_logged_with_its_traceback(self):
		log = self.logged(self.request(error=ConnectionError("unreachable")))

		self.assertEqual(log["status"], "Failed")
		self.assertIn("ConnectionError: unreachable", log["error"])
		self.assertIsNone(log["output"])

	def test_every_request_is_logged_when_the_server_asks_for_it(self):
		log = self.logged(self.request(make_response(200), log_all_requests=True))

		self.assertEqual(log["status"], "Completed")
		self.assertIsNone(log["error"])

	def test_a_long_response_is_cut_short(self):
		log = self.logged(
			self.request(make_response(500, "x" * (woocommerce_api.LOGGED_RESPONSE_LENGTH + 10)))
		)

		self.assertEqual(len(log["output"]), woocommerce_api.LOGGED_RESPONSE_LENGTH)
