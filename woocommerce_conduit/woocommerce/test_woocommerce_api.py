from datetime import timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from woocommerce import API

from woocommerce_conduit.woocommerce import woocommerce_api
from woocommerce_conduit.woocommerce.woocommerce_api import WooCommerceAPI


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
		log = self.logged(self.request(make_response(500, "x" * (woocommerce_api.LOGGED_RESPONSE_LENGTH + 10))))

		self.assertEqual(len(log["output"]), woocommerce_api.LOGGED_RESPONSE_LENGTH)
