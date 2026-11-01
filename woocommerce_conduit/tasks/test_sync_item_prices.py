# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

from datetime import date
from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit import hooks
from woocommerce_conduit.tasks import sync_item_prices
from woocommerce_conduit.tasks.sync_item_prices import (
	SynchroniseItemPrice,
	pick_sale,
	run_item_price_sync_in_background,
	update_item_price_for_woocommerce_item_from_hook,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)


def make_product(**values) -> WooCommerceProduct:
	with patch.object(WooCommerceProduct, "_init_api", return_value=[]):
		return frappe.get_doc(
			{"doctype": "WooCommerce Product", "name": "one.example.com~12", "type": "simple", **values}
		)  # type: ignore


class TestItemPriceHook(IntegrationTestCase):
	def run_hook(self, doc, enabled=True):
		with (
			patch.object(frappe, "in_test", False),
			patch.object(sync_item_prices, "price_sync_is_enabled", return_value=enabled),
			patch.object(sync_item_prices, "enqueue_after_commit_unless_queued") as enqueue,
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
		self.assertEqual(
			enqueue.call_args.kwargs, {"job_id": "woocommerce_price_sync::MUG-1", "item_code": "MUG-1"}
		)

	def test_a_price_pulled_from_woocommerce_is_not_pushed_back(self):
		self.run_hook(self.make_item_price(created_by_sync=True)).assert_not_called()

	def test_nothing_is_pushed_while_price_sync_is_off(self):
		self.run_hook(self.make_item_price(), enabled=False).assert_not_called()


class TestPricePush(IntegrationTestCase):
	def push(self, price_list_rate, product, sale=None, **server):
		server = _dict(
			{
				"name": "one.example.com",
				"price_list": "Standard Selling",
				"enable_sales_price_list_sync": 0,
				"sales_price_list": None,
				"price_list_delay_per_item": 0,
				**server,
			}
		)
		sync = SynchroniseItemPrice(servers=[server])
		sync.wc_server = server
		sync.item_price_list = [
			_dict(woocommerce_server="one.example.com", woocommerce_id="12", price_list_rate=price_list_rate)
		]
		sync.sale_price_map = {"12": sale} if sale else {}

		with (
			patch("frappe.get_doc", return_value=product),
			patch.object(product, "load_from_db"),
			patch.object(product, "write", side_effect=lambda changes: changes) as write,
			patch.object(sync_item_prices, "record_sync_time") as record_sync_time,
		):
			sync.sync_items_with_woocommerce_products()
		return write, record_sync_time

	def test_a_changed_price_is_written(self):
		write, record_sync_time = self.push(15.0, make_product(regular_price="12.00"))

		write.assert_called_once_with({"regular_price": 15.0})
		record_sync_time.assert_called_once_with("one.example.com")

	def test_an_unchanged_price_is_not_written(self):
		write, record_sync_time = self.push(12.0, make_product(regular_price="12.00"))

		write.assert_not_called()
		record_sync_time.assert_not_called()

	def test_only_the_changed_part_of_a_sale_is_written(self):
		product = make_product(
			regular_price="12.00", sale_price="9.00", date_on_sale_from=None, date_on_sale_to=None
		)
		sale = _dict(price_list_rate=9.0, valid_from=None, valid_upto="2026-11-30")

		write, _record_sync_time = self.push(
			12.0, product, sale=sale, enable_sales_price_list_sync=1, sales_price_list="Sale"
		)

		write.assert_called_once_with({"date_on_sale_to": "2026-11-30T00:00:00"})

	def test_a_withdrawn_sale_is_cleared(self):
		product = make_product(regular_price="12.00", sale_price="9.00")

		write, _record_sync_time = self.push(
			12.0, product, enable_sales_price_list_sync=1, sales_price_list="Sale"
		)

		write.assert_called_once_with({"sale_price": 0})


class TestPriceChoice(IntegrationTestCase):
	def test_the_regular_price_is_the_one_erpnext_would_charge(self):
		server = _dict(
			name="one.example.com", enabled=1, enable_price_list_sync=1, price_list="Standard Selling"
		)
		sync = SynchroniseItemPrice(servers=[server])
		sync.wc_server = server
		connections = [
			_dict(item_code="MUG-1", woocommerce_server="one.example.com", woocommerce_id="12"),
			_dict(item_code="CUP-1", woocommerce_server="one.example.com", woocommerce_id="13"),
		]
		with (
			patch.object(sync, "get_connections", return_value=connections),
			patch.object(sync_item_prices, "today", return_value="2026-11-01"),
			patch.object(
				sync_item_prices,
				"get_item_prices_for_stock_uom",
				return_value={"MUG-1": _dict(price_list_rate=15.0)},
			) as get_item_prices,
		):
			sync.get_erpnext_item_prices()

		pctx, item_codes = get_item_prices.call_args.args
		self.assertEqual(pctx, {"price_list": "Standard Selling", "transaction_date": "2026-11-01"})
		self.assertEqual(item_codes, ["MUG-1", "CUP-1"])
		# CUP-1 has no price in the list, so its product is left alone
		self.assertEqual(
			sync.item_price_list,
			[
				_dict(
					item_code="MUG-1",
					woocommerce_server="one.example.com",
					woocommerce_id="12",
					price_list_rate=15.0,
				)
			],
		)

	def test_an_item_that_sells_in_its_sales_uom_is_priced_in_it(self):
		server = _dict(
			name="one.example.com",
			enabled=1,
			enable_price_list_sync=1,
			price_list="Standard Selling",
			sync_in_sales_uom=1,
		)
		sync = SynchroniseItemPrice(servers=[server])
		sync.wc_server = server
		connections = [
			_dict(item_code="MUG-1", woocommerce_server="one.example.com", woocommerce_id="12"),
			_dict(item_code="CUP-1", woocommerce_server="one.example.com", woocommerce_id="13"),
		]
		unit = _dict(uom="Box", stock_uom="Nos", factor=12)
		with (
			patch.object(sync, "get_connections", return_value=connections),
			patch.object(sync_item_prices, "today", return_value="2026-11-01"),
			patch.object(sync_item_prices, "get_sales_units", return_value={"MUG-1": unit}),
			patch.object(
				sync_item_prices,
				"get_item_prices_for_stock_uom",
				return_value={"CUP-1": _dict(price_list_rate=3.0)},
			) as get_item_prices,
			patch.object(sync_item_prices, "get_price_list_rate_for", return_value=120.0) as get_rate,
		):
			sync.get_erpnext_item_prices()

		self.assertEqual(get_item_prices.call_args.args[1], ["CUP-1"])
		ctx, item_code = get_rate.call_args.args
		self.assertEqual(item_code, "MUG-1")
		self.assertEqual((ctx["uom"], ctx["conversion_factor"], ctx["qty"]), ("Box", 12, 1))
		self.assertEqual(
			{row.item_code: row.price_list_rate for row in sync.item_price_list},
			{"MUG-1": 120.0, "CUP-1": 3.0},
		)

	def test_a_sale_of_the_stock_uom_goes_times_the_sales_uom(self):
		sync = SynchroniseItemPrice(servers=[_dict(name="one.example.com")])
		sync.sales_units = {"MUG-1": _dict(uom="Box", stock_uom="Nos", factor=12)}

		self.assertEqual(sync.get_sale_rate("MUG-1", _dict(price_list_rate=2.0, uom="")), 24.0)
		self.assertEqual(sync.get_sale_rate("MUG-1", _dict(price_list_rate=20.0, uom="Box")), 20.0)
		self.assertEqual(sync.get_sale_rate("CUP-1", _dict(price_list_rate=2.0, uom="")), 2.0)

	def test_the_running_sale_that_started_last_is_shown(self):
		rows = [
			_dict(name="old", valid_from=date(2026, 10, 1)),
			_dict(name="current", valid_from=date(2026, 10, 20)),
			_dict(name="next", valid_from=date(2026, 12, 1)),
		]
		self.assertEqual(pick_sale(rows, date(2026, 11, 1)).name, "current")

	def test_without_a_running_sale_the_next_one_is_scheduled(self):
		rows = [
			_dict(name="later", valid_from=date(2027, 1, 1)),
			_dict(name="next", valid_from=date(2026, 12, 1)),
		]
		self.assertEqual(pick_sale(rows, date(2026, 11, 1)).name, "next")

	def test_a_sale_without_a_start_is_running(self):
		self.assertEqual(pick_sale([_dict(name="always", valid_from=None)], date(2026, 11, 1)).name, "always")


class TestDailyPricePush(IntegrationTestCase):
	def test_the_items_are_pushed_a_hundred_a_job(self):
		item_codes = [f"ITEM-{i:03}" for i in range(150)]
		with (
			patch.object(sync_item_prices, "price_sync_is_enabled", return_value=True),
			patch("frappe.get_all", return_value=item_codes),
			patch("frappe.enqueue") as enqueue,
		):
			run_item_price_sync_in_background()

		self.assertEqual([len(call.kwargs["item_codes"]) for call in enqueue.call_args_list], [100, 50])


class TestSchedule(IntegrationTestCase):
	def test_products_are_not_pulled_on_a_schedule(self):
		scheduled = [job for jobs in hooks.scheduler_events.values() for job in jobs]

		# Products come in with the orders that need them, or from the server's Import Products
		self.assertFalse([job for job in scheduled if "sync_items" in job])
		self.assertIn(
			"woocommerce_conduit.tasks.stock_update.update_stock_levels_for_all_enabled_items_in_background",
			hooks.scheduler_events["daily_long"],
		)
