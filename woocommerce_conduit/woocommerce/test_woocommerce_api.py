import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http import HTTPStatus
from unittest.mock import MagicMock, patch

import frappe
import requests
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
	get_servers_from_filters,
	map_frappe_filters_to_wc_params,
	parse_retry_after,
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


def make_response(status_code: int, text: str = "{}", headers: dict | None = None) -> MagicMock:
	return MagicMock(
		status_code=status_code,
		ok=status_code < 400,
		reason=HTTPStatus(status_code).phrase,
		text=text,
		elapsed=timedelta(seconds=0.25),
		headers=headers or {},
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
			self.request(make_response(400, "x" * (woocommerce_api.LOGGED_RESPONSE_LENGTH + 10)))
		)

		self.assertEqual(len(log["output"]), woocommerce_api.LOGGED_RESPONSE_LENGTH)


def get_order(api: WooCommerceAPI):
	return api.get("orders/12")


def post_refund(api: WooCommerceAPI):
	return api.post("orders/12/refunds", {"amount": "10.00"})


class TestRetries(IntegrationTestCase):
	def send(self, *answers, request=get_order, in_web_request: bool = False):
		"""
		Send a request that the shop answers with each of `answers` in turn, a response or an error, and
		return what the client returns. `tries` counts the requests sent, `waits` the seconds waited before
		each retry.
		"""
		api = WooCommerceAPI("https://shop.example.com", "ck", "cs", woocommerce_server="shop.example.com")
		with (
			patch.object(API, "_API__request", side_effect=answers) as sent,
			patch.object(woocommerce_api, "sleep") as sleep,
			# Without the random part of the wait
			patch.object(woocommerce_api.random, "uniform", return_value=1),
			patch.object(frappe, "request", MagicMock(), create=True) if in_web_request else nullcontext(),
		):
			try:
				return request(api)
			finally:
				self.tries = sent.call_count
				self.waits = [call.args[0] for call in sleep.call_args_list]

	def test_a_request_the_shop_turned_away_is_sent_again(self):
		response = self.send(make_response(429), make_response(200))

		self.assertEqual(response.status_code, 200)
		self.assertEqual(self.waits, [1])

	def test_a_failing_shop_is_tried_three_more_times_with_longer_waits(self):
		response = self.send(*[make_response(503)] * 5)

		self.assertEqual(response.status_code, 503)
		self.assertEqual(self.tries, 4)
		self.assertEqual(self.waits, [1, 2, 4])

	def test_the_wait_the_shop_asks_for_is_kept(self):
		self.send(make_response(429, headers={"Retry-After": "7"}), make_response(200))

		self.assertEqual(self.waits, [7])

	def test_the_shop_can_ask_to_wait_until_a_date(self):
		in_a_minute = format_datetime(datetime.now(UTC) + timedelta(minutes=1), usegmt=True)

		self.send(make_response(503, headers={"Retry-After": in_a_minute}), make_response(200))

		self.assertAlmostEqual(self.waits[0], 60, delta=2)

	def test_a_shop_that_asks_for_a_long_wait_is_not_waited_for(self):
		# As WordPress answers while it is being updated
		response = self.send(make_response(503, headers={"Retry-After": "600"}), make_response(200))

		self.assertEqual(response.status_code, 503)
		self.assertEqual(self.waits, [])

	def test_a_person_waiting_for_a_page_is_not_kept_waiting_as_long(self):
		answers = (make_response(429, headers={"Retry-After": "30"}), make_response(200))

		self.assertEqual(self.send(*answers).status_code, 200)
		self.assertEqual(self.send(*answers, in_web_request=True).status_code, 429)
		self.assertEqual(self.waits, [])

	def test_an_answer_another_try_would_not_change_is_kept(self):
		for status_code in (400, 401, 404):
			with self.subTest(status_code=status_code):
				response = self.send(make_response(status_code), make_response(200))

				self.assertEqual(response.status_code, status_code)
				self.assertEqual(self.tries, 1)

	def test_a_post_is_only_sent_again_when_the_shop_turned_it_away(self):
		# After a 5xx the shop may have made the refund already
		self.assertEqual(
			self.send(make_response(500), make_response(201), request=post_refund).status_code, 500
		)
		self.assertEqual(
			self.send(make_response(429), make_response(201), request=post_refund).status_code, 201
		)

	def test_a_dropped_connection_is_tried_again(self):
		response = self.send(
			requests.exceptions.ConnectionError("Connection reset by peer"), make_response(200)
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(self.waits, [1])

	def test_a_timeout_a_bad_certificate_or_a_post_that_lost_its_connection_is_not_tried_again(self):
		for error, request in (
			(requests.exceptions.ReadTimeout(), get_order),
			(requests.exceptions.ConnectTimeout(), get_order),
			(requests.exceptions.SSLError(), get_order),
			(requests.exceptions.ConnectionError(), post_refund),
		):
			with self.subTest(error=type(error).__name__, request=request.__name__):
				with self.assertRaises(type(error)):
					self.send(error, make_response(200), request=request)
				self.assertEqual(self.tries, 1)

	def test_each_failed_try_is_logged_as_sent_again(self):
		with (
			patch.object(woocommerce_api, "deferred_insert") as deferred_insert,
			patch.object(frappe, "in_test", False),
		):
			self.send(make_response(429), make_response(200))

		# The 200 isn't, as the server doesn't log every request
		deferred_insert.assert_called_once()
		log = deferred_insert.call_args.args[1][0]
		self.assertEqual(log["status"], "Failed")
		self.assertEqual(log["error"], "429 Too Many Requests\nSent again in 1.0 s")

	def test_retry_after_is_read_as_seconds_or_as_a_date(self):
		self.assertEqual(parse_retry_after("120"), 120)
		# A date that has passed means no wait
		self.assertEqual(parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0)
		self.assertIsNone(parse_retry_after(None))
		self.assertIsNone(parse_retry_after("soon"))


class TestFilters(IntegrationTestCase):
	def test_every_form_of_filter_is_understood(self):
		expected = {"status": ["processing"]}
		self.assertEqual(
			map_frappe_filters_to_wc_params({"status": "processing"}, "WooCommerce Order"), expected
		)
		self.assertEqual(
			map_frappe_filters_to_wc_params([["status", "=", "processing"]], "WooCommerce Order"), expected
		)
		self.assertEqual(
			map_frappe_filters_to_wc_params([["WooCommerce Order", "status", "=", "processing"]]), expected
		)

	def test_a_date_range_is_whole_days(self):
		filters = [["WooCommerce Order", "woocommerce_date_created", "Between", ["2026-10-01", "2026-10-31"]]]
		self.assertEqual(
			map_frappe_filters_to_wc_params(filters),
			{"after": "2026-10-01T00:00:00", "before": "2026-11-01T00:00:00"},
		)

	def test_the_fields_of_the_doctype_filter_by_date_too(self):
		filters = [["WooCommerce Order", "modified", ">", "2026-10-30 10:00:00.123456"]]
		self.assertEqual(map_frappe_filters_to_wc_params(filters), {"modified_after": "2026-10-30T10:00:00"})

	def test_names_are_asked_for_by_their_id(self):
		self.assertEqual(
			map_frappe_filters_to_wc_params([["WooCommerce Order", "name", "=", "one.example.com~12"]]),
			{"include": ["12"]},
		)
		self.assertEqual(
			map_frappe_filters_to_wc_params(
				[["WooCommerce Order", "name", "in", ["one.example.com~12", "one.example.com~13"]]]
			),
			{"include": "12,13"},
		)

	def test_a_filter_on_the_server_picks_the_servers(self):
		by_server = {"woocommerce_server": "two.example.com"}
		self.assertEqual(map_frappe_filters_to_wc_params(by_server, "WooCommerce Order"), {})
		self.assertEqual(get_servers_from_filters(by_server, "WooCommerce Order"), {"two.example.com"})
		self.assertEqual(
			get_servers_from_filters([["WooCommerce Order", "name", "=", "one.example.com~12"]]),
			{"one.example.com"},
		)
		self.assertIsNone(get_servers_from_filters([["WooCommerce Order", "status", "=", "processing"]]))

	def test_a_list_by_server_reads_only_that_server(self):
		one, two = PagedAPI("one.example.com", 3), PagedAPI("two.example.com", 3)
		with patch.object(WooCommerceOrder, "_init_api", return_value=[one, two]):
			WooCommerceOrder.get_list_of_records({"filters": {"woocommerce_server": "two.example.com"}})

		self.assertEqual(one.requests, [])
		self.assertEqual(len(two.requests), 1)
		self.assertNotIn("woocommerce_server", two.requests[0])

	def test_the_count_has_the_filters_of_the_list(self):
		one, two = PagedAPI("one.example.com", 3), PagedAPI("two.example.com", 5)
		with patch.object(WooCommerceOrder, "_init_api", return_value=[one, two]):
			count = WooCommerceOrder.get_count_of_records(
				{
					"filters": [
						["WooCommerce Order", "status", "=", "processing"],
						["WooCommerce Order", "woocommerce_server", "=", "two.example.com"],
					]
				}
			)

		self.assertEqual(count, 5)
		self.assertEqual(one.requests, [])
		self.assertEqual(two.requests[0]["status"], ["processing"])
