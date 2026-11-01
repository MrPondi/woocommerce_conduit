# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

import json
from unittest.mock import patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks import order_lines, sync_sales_orders
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import (
	WooCommerceOrder,
	get_status_options,
)

# The orders are made in memory, and the Sales Order they link to is mocked, so ERPNext's Sales Order test
# records (and all theirs) aren't needed
IGNORE_TEST_RECORD_DEPENDENCIES = ["Sales Order"]

SERVER = "one.example.com"


def make_order(**values) -> WooCommerceOrder:
	with patch.object(WooCommerceOrder, "_init_api", return_value=[]):
		return frappe.get_doc(
			{
				"doctype": "WooCommerce Order",
				"name": f"{SERVER}~12",
				"woocommerce_server": SERVER,
				"woocommerce_id": "12",
				"status": "processing",
				"line_items": json.dumps(
					[
						{"id": 1, "product_id": 10, "variation_id": 11, "quantity": 1, "total": "5"},
						{"id": 2, "product_id": 20, "variation_id": 0, "quantity": 1, "total": "5"},
					]
				),
				**values,
			}
		)  # type: ignore


class TestWooCommerceOrderForm(IntegrationTestCase):
	def test_form_shows_the_sales_order_and_the_items(self):
		order = make_order()
		item_codes = {"11": "MUG-RED", "20": None}
		with (
			patch.object(sync_sales_orders, "find_sales_order", return_value="SAL-ORD-1") as find_sales_order,
			patch.object(order_lines, "find_item_code", side_effect=lambda server, pid: item_codes[pid]),
		):
			order.onload()

		self.assertEqual(order.sales_order, "SAL-ORD-1")
		find_sales_order.assert_called_once_with(SERVER, "12")
		self.assertEqual(order.get_onload("item_codes"), item_codes)

	def test_changed_status_syncs_the_sales_order(self):
		order = make_order(status="completed")
		order._doc_before_save = make_order(status="processing")
		with patch.object(sync_sales_orders, "enqueue_sales_order_sync") as enqueue:
			order.on_update()
		enqueue.assert_called_once_with(woocommerce_order_name=f"{SERVER}~12")

		order._doc_before_save = make_order(status="completed")
		with patch.object(sync_sales_orders, "enqueue_sales_order_sync") as enqueue:
			order.on_update()
		enqueue.assert_not_called()

	def test_a_status_pushed_from_the_sales_order_is_not_pulled_back(self):
		order = make_order(status="completed")
		order._doc_before_save = make_order(status="processing")
		order.flags.created_by_sync = True
		with patch.object(sync_sales_orders, "enqueue_sales_order_sync") as enqueue:
			order.on_update()
		enqueue.assert_not_called()

	def test_only_the_status_is_written(self):
		order = make_order()
		self.assertEqual(
			order.before_db_update({"status": "completed", "customer_note": "Hi", "total": 1}),
			{"status": "completed"},
		)

	def test_status_options_come_from_the_status_map(self):
		server = frappe.get_doc(
			{
				"doctype": "WooCommerce Server",
				"name": SERVER,
				"sales_order_status_map": [
					{"woocommerce_status": "processing", "woocommerce_status_label": "Processing"},
					{"woocommerce_status": "ready-pickup", "woocommerce_status_label": None},
				],
			}
		)
		with patch.object(frappe, "get_cached_doc", return_value=server):
			options = get_status_options(SERVER)

		self.assertEqual(
			options,
			[
				{"value": "processing", "label": "Processing"},
				{"value": "ready-pickup", "label": "ready-pickup"},
			],
		)

	def test_line_items_are_indented_for_the_form(self):
		order = make_order()
		record = order.after_load_from_db({"line_items": '[{"id": 1}]', "tax_lines": '[{"id": 2}]'})
		self.assertIn("\n", record["line_items"])
		self.assertIn("\n", record["tax_lines"])
