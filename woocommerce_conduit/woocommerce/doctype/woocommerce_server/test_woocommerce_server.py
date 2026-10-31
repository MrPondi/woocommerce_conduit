# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	normalise_server_url,
)

# The servers are only made in memory, so the records of what they link to aren't needed
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"User",
	"UOM",
	"Item Group",
	"Price List",
	"Company",
	"Warehouse",
	"Account",
	"Sales Taxes and Charges Template",
	"Shipping Rule",
	"Mode of Payment",
	"Bank Account",
]


def new_server(**fields):
	return frappe.get_doc(
		{
			"doctype": "WooCommerce Server",
			"woocommerce_server_url": "shop.example.com",
			"api_consumer_key": "ck_typed",
			"api_consumer_secret": "cs_typed",
			**fields,
		}
	)


class TestWooCommerceServer(IntegrationTestCase):
	def test_url_without_scheme_gets_https(self):
		self.assertEqual(normalise_server_url("shop.example.com"), "https://shop.example.com")
		self.assertEqual(normalise_server_url("http://shop.example.com/shop/"), "http://shop.example.com")
		self.assertIsNone(normalise_server_url(""))

	def test_api_of_an_unsaved_server_uses_the_typed_values(self):
		with patch(
			"woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server.WooCommerceAPI"
		) as api:
			new_server().get_api()

		kwargs = api.call_args.kwargs
		self.assertEqual(kwargs["url"], "https://shop.example.com")
		self.assertEqual(kwargs["consumer_key"], "ck_typed")
		self.assertEqual(kwargs["consumer_secret"], "cs_typed")

	def test_masked_password_is_read_back(self):
		server = new_server(api_consumer_key="********")
		with patch.object(type(server), "get_password", return_value="ck_stored") as get_password:
			self.assertEqual(server._credential("api_consumer_key"), "ck_stored")
			self.assertEqual(server._credential("api_consumer_secret"), "cs_typed")
		get_password.assert_called_once()

	def test_missing_statuses_come_with_the_fetch_error(self):
		server = new_server()
		api = MagicMock()
		api.get.side_effect = ConnectionError("unreachable")
		with patch.object(type(server), "get_api", return_value=api), patch.object(frappe, "log_error"):
			result = server.get_missing_order_status_rows()

		self.assertEqual(result["error"], "unreachable")
		self.assertIn("pending", [row["woocommerce_status"] for row in result["rows"]])

	def test_missing_statuses_include_the_shops_own(self):
		server = new_server()
		response = MagicMock(status_code=200)
		response.json.return_value = [
			{"slug": "pending", "name": "Oczekujące na płatność"},
			{"slug": "ready-pickup", "name": "Do odbioru"},
		]
		api = MagicMock()
		api.get.return_value = response
		with patch.object(type(server), "get_api", return_value=api):
			result = server.get_missing_order_status_rows()

		self.assertIsNone(result["error"])
		labels = {row["woocommerce_status"]: row["woocommerce_status_label"] for row in result["rows"]}
		self.assertEqual(labels["pending"], "Oczekujące na płatność")
		self.assertEqual(labels["ready-pickup"], "Do odbioru")
