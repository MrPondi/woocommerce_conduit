# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit import hooks
from woocommerce_conduit.tasks import sync_item_prices
from woocommerce_conduit.tasks.sync_item_prices import (
	SynchroniseItemPrice,
	update_item_price_for_woocommerce_item_from_hook,
)


class TestItemPriceHook(IntegrationTestCase):
	def run_hook(self, doc, enabled=True):
		with (
			patch.dict(frappe.flags, {"in_test": False}),
			patch.object(sync_item_prices, "price_sync_is_enabled", return_value=enabled),
			patch("frappe.enqueue") as enqueue,
		):
			update_item_price_for_woocommerce_item_from_hook(doc)
		return enqueue

	@staticmethod
	def make_item_price(**flags):
		doc = frappe.new_doc("Item Price")
		doc.item_code = "MUG-1"
		doc.flags.update(flags)
		return doc

	def test_only_the_item_code_is_enqueued_after_commit(self):
		enqueue = self.run_hook(self.make_item_price())

		enqueue.assert_called_once()
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])
		self.assertEqual(enqueue.call_args.kwargs["item_code"], "MUG-1")
		self.assertNotIn("item_price_doc", enqueue.call_args.kwargs)

	def test_a_price_pulled_from_woocommerce_is_not_pushed_back(self):
		self.run_hook(self.make_item_price(created_by_sync=True)).assert_not_called()

	def test_nothing_is_pushed_while_price_sync_is_off(self):
		self.run_hook(self.make_item_price(), enabled=False).assert_not_called()


class TestPricePush(IntegrationTestCase):
	def test_a_changed_price_is_written(self):
		server = _dict(name="one.example.com", price_list="Standard Selling", enable_sales_price_list_sync=0)
		sync = SynchroniseItemPrice(servers=[server])
		sync.wc_server = server
		sync.item_price_list = [
			_dict(woocommerce_server="one.example.com", woocommerce_id="12", price_list_rate=15.0)
		]
		product = MagicMock(regular_price="12.00")

		with (
			patch("frappe.get_doc", return_value=product),
			patch.object(sync_item_prices, "record_sync_time") as record_sync_time,
		):
			sync.sync_items_with_woocommerce_products()

		self.assertEqual(product.regular_price, 15.0)
		product.save.assert_called_once()
		record_sync_time.assert_called_once_with("one.example.com")

	def test_an_unchanged_price_is_not_written(self):
		server = _dict(name="one.example.com", price_list="Standard Selling", enable_sales_price_list_sync=0)
		sync = SynchroniseItemPrice(servers=[server])
		sync.wc_server = server
		sync.item_price_list = [
			_dict(woocommerce_server="one.example.com", woocommerce_id="12", price_list_rate=12.0)
		]
		product = MagicMock(regular_price="12.00")

		with (
			patch("frappe.get_doc", return_value=product),
			patch.object(sync_item_prices, "record_sync_time") as record_sync_time,
		):
			sync.sync_items_with_woocommerce_products()

		product.save.assert_not_called()
		record_sync_time.assert_not_called()


class TestSchedule(IntegrationTestCase):
	def test_products_are_not_pulled_on_a_schedule(self):
		scheduled = [job for jobs in hooks.scheduler_events.values() for job in jobs]

		self.assertNotIn(
			"woocommerce_conduit.tasks.sync_items.sync_woocommerce_products_modified_since", scheduled
		)
		self.assertIn(
			"woocommerce_conduit.tasks.stock_update.update_stock_levels_for_all_enabled_items_in_background",
			hooks.scheduler_events["daily_long"],
		)
