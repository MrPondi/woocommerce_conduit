# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks import stock_update
from woocommerce_conduit.tasks.stock_update import (
	PENDING_ITEMS_FLAG,
	apply_stock_value_hooks,
	enqueue_stock_update,
	forget_pending_stock_updates,
	get_warehouses_with_descendants,
	get_woocommerce_stock_values,
	push_pending_stock_updates,
	update_stock_levels_for_all_enabled_items_in_background,
	update_stock_levels_for_woocommerce_item,
	update_stock_levels_from_ledger,
	update_stock_levels_on_woocommerce_site,
)

BINS = [
	_dict(warehouse="Stores", actual_qty=7.6, reserved_qty=2),
	_dict(warehouse="Shop Floor", actual_qty=3, reserved_qty=0),
	_dict(warehouse="Elsewhere", actual_qty=40, reserved_qty=0),
]


def make_server(**values):
	return _dict(
		{
			"name": "one.example.com",
			"enabled": 1,
			"enable_stock_level_synchronisation": 1,
			"warehouses": [_dict(warehouse="Stores"), _dict(warehouse="Shop Floor")],
			"subtract_reserved_stock": 0,
			"push_zero_stock_for_disabled_items": 0,
			**values,
		}
	)


def add_backorders(item, wc_server, bins, stock_values):
	# An extension in the way a supplier app would write one
	if stock_values["stock_quantity"] <= 0:
		return {**stock_values, "backorders": "notify"}
	return None


def stock_values(server, bins=BINS, item_disabled=False):
	# The warehouses aren't groups, so they stand for themselves
	with patch.object(
		stock_update,
		"get_warehouses_with_descendants",
		side_effect=lambda rows: {row.warehouse for row in rows or []},
	):
		return get_woocommerce_stock_values(server, bins, item_disabled=item_disabled)


class TestStockValues(IntegrationTestCase):
	def test_the_stock_of_the_servers_warehouses_is_rounded_down(self):
		self.assertEqual(stock_values(make_server()), {"manage_stock": 1, "stock_quantity": 10})

	def test_reserved_stock_can_be_subtracted(self):
		values = stock_values(make_server(subtract_reserved_stock=1))

		self.assertEqual(values["stock_quantity"], 8)

	def test_the_quantity_is_never_negative(self):
		bins = [_dict(warehouse="Stores", actual_qty=1, reserved_qty=3)]

		values = stock_values(make_server(subtract_reserved_stock=1), bins=bins)

		self.assertEqual(values["stock_quantity"], 0)

	def test_a_disabled_item_has_no_stock(self):
		self.assertEqual(stock_values(make_server(), item_disabled=True)["stock_quantity"], 0)

	def test_the_shops_backorder_setting_is_left_alone(self):
		self.assertNotIn("backorders", stock_values(make_server(), bins=BINS[2:]))


class TestWarehouses(IntegrationTestCase):
	def test_a_group_counts_with_the_warehouses_under_it(self):
		# As ERPNext's get_child_warehouses has them: the warehouses under it, and itself
		tree = {"All Stores": ["Stores", "Shop Floor", "All Stores"], "Elsewhere": ["Elsewhere"]}
		rows = [_dict(warehouse="All Stores"), _dict(warehouse="Elsewhere"), _dict(warehouse=None)]

		with patch.object(stock_update, "get_child_warehouses", side_effect=lambda name: tree[name]):
			warehouses = get_warehouses_with_descendants(rows)

		self.assertEqual(warehouses, {"All Stores", "Stores", "Shop Floor", "Elsewhere"})


class TestStockValueHooks(IntegrationTestCase):
	def apply(self, values):
		with patch(
			"frappe.get_hooks",
			return_value=["woocommerce_conduit.tasks.test_stock_update.add_backorders"],
		):
			return apply_stock_value_hooks(_dict(), make_server(), [], values)

	def test_a_hook_can_change_the_values(self):
		values = self.apply({"manage_stock": 1, "stock_quantity": 0})

		self.assertEqual(values, {"manage_stock": 1, "stock_quantity": 0, "backorders": "notify"})

	def test_a_hook_returning_none_keeps_the_values(self):
		values = self.apply({"manage_stock": 1, "stock_quantity": 5})

		self.assertEqual(values, {"manage_stock": 1, "stock_quantity": 5})


class TestStockPush(IntegrationTestCase):
	def push(self, item, server, hooks=(), product=None):
		product = product or MagicMock()
		with (
			patch("frappe.get_doc", return_value=item),
			patch("frappe.get_all", return_value=BINS),
			patch("frappe.get_cached_doc", return_value=server),
			patch("frappe.get_hooks", return_value=list(hooks)),
			patch.object(stock_update, "get_warehouses_with_descendants", return_value={"Stores"}),
			patch.object(stock_update, "load_wc_product", return_value=product) as load_wc_product,
			patch.object(stock_update, "record_sync_time") as record_sync_time,
		):
			result = update_stock_levels_on_woocommerce_site("MUG-1")
		self.record_sync_time = record_sync_time
		return result, load_wc_product, product

	@staticmethod
	def make_item(**values):
		return _dict(
			{
				"name": "MUG-1",
				"is_stock_item": 1,
				"has_variants": 0,
				"disabled": 0,
				"woocommerce_servers": [
					_dict(woocommerce_server="one.example.com", woocommerce_id="12", enable_sync=1)
				],
				**values,
			}
		)

	def test_the_stock_is_written_to_the_linked_product(self):
		result, load_wc_product, product = self.push(self.make_item(), make_server())

		self.assertTrue(result)
		load_wc_product.assert_called_once_with("one.example.com~12")
		product.get_changes.assert_called_once_with({"manage_stock": 1, "stock_quantity": 7})
		product.write.assert_called_once_with(product.get_changes.return_value)
		self.record_sync_time.assert_called_once_with("one.example.com")

	def test_a_product_that_has_the_stock_already_is_not_written(self):
		product = MagicMock()
		product.get_changes.return_value = {}

		self.push(self.make_item(), make_server(), product=product)

		product.write.assert_not_called()
		self.record_sync_time.assert_not_called()

	def test_a_template_is_skipped(self):
		result, load_wc_product, _product = self.push(self.make_item(has_variants=1), make_server())

		self.assertFalse(result)
		load_wc_product.assert_not_called()
		self.record_sync_time.assert_not_called()

	def test_a_connection_left_out_of_the_sync_is_skipped(self):
		item = self.make_item(
			woocommerce_servers=[
				_dict(woocommerce_server="one.example.com", woocommerce_id="12", enable_sync=0)
			]
		)

		_result, load_wc_product, _product = self.push(item, make_server())

		load_wc_product.assert_not_called()

	def test_a_server_without_stock_sync_is_skipped(self):
		_result, load_wc_product, _product = self.push(
			self.make_item(), make_server(enable_stock_level_synchronisation=0)
		)

		load_wc_product.assert_not_called()

	def test_a_disabled_item_is_only_pushed_when_the_server_asks(self):
		_result, load_wc_product, _product = self.push(self.make_item(disabled=1), make_server())
		load_wc_product.assert_not_called()

		_result, _load, product = self.push(
			self.make_item(disabled=1), make_server(push_zero_stock_for_disabled_items=1)
		)
		product.get_changes.assert_called_once_with({"manage_stock": 1, "stock_quantity": 0})

	def test_the_values_adjusted_by_a_hook_are_pushed(self):
		_result, _load, product = self.push(
			self.make_item(disabled=1),
			make_server(push_zero_stock_for_disabled_items=1),
			hooks=["woocommerce_conduit.tasks.test_stock_update.add_backorders"],
		)

		product.get_changes.assert_called_once_with(
			{"manage_stock": 1, "stock_quantity": 0, "backorders": "notify"}
		)


class TestStockHook(IntegrationTestCase):
	def tearDown(self):
		forget_pending_stock_updates()

	def run_hook(self, *docs, linked=("CUP-1", "MUG-1"), reserved_stock_counts=False, enabled=True):
		"""
		Run the hooks for the documents and then what runs after the commit, and return the push jobs
		"""
		with (
			patch.dict(frappe.flags, {"in_test": False}),
			patch.object(stock_update, "stock_sync_is_enabled", return_value=enabled),
			patch.object(stock_update, "reserved_stock_is_subtracted", return_value=reserved_stock_counts),
			patch.object(frappe.db, "after_commit") as after_commit,
			patch.object(frappe.db, "after_rollback"),
			patch.object(
				stock_update,
				"get_linked_items",
				side_effect=lambda codes: sorted(set(codes or ()) & set(linked)),
			),
			patch.object(stock_update, "enqueue_unless_queued") as enqueue,
		):
			for doc in docs:
				if doc.doctype == "Stock Ledger Entry":
					update_stock_levels_from_ledger(doc)
				else:
					update_stock_levels_for_woocommerce_item(doc)
			push_pending_stock_updates()
		self.after_commit = after_commit
		return enqueue

	@staticmethod
	def pushed(enqueue) -> list[str]:
		return [call.kwargs["item_code"] for call in enqueue.call_args_list]

	@staticmethod
	def ledger(item_code):
		return _dict(doctype="Stock Ledger Entry", item_code=item_code)

	def test_each_item_of_a_transaction_is_pushed_once_after_commit(self):
		# A transfer moves MUG-1 out of one warehouse and into another, then CUP-1 is delivered
		enqueue = self.run_hook(self.ledger("MUG-1"), self.ledger("MUG-1"), self.ledger("CUP-1"))

		self.assertEqual(self.pushed(enqueue), ["CUP-1", "MUG-1"])
		self.after_commit.add.assert_called_once_with(push_pending_stock_updates)
		self.assertEqual(enqueue.call_args.kwargs["job_id"], "woocommerce_stock_update::MUG-1")

	def test_an_item_that_is_not_linked_to_woocommerce_is_not_pushed(self):
		self.run_hook(self.ledger("BOLT-1")).assert_not_called()

	def test_nothing_is_pushed_while_stock_sync_is_off(self):
		self.run_hook(self.ledger("MUG-1"), enabled=False).assert_not_called()

	def test_a_sales_order_only_counts_where_reserved_stock_is_subtracted(self):
		doc = _dict(doctype="Sales Order", items=[_dict(item_code="MUG-1")])

		self.run_hook(doc).assert_not_called()
		self.assertEqual(self.pushed(self.run_hook(doc, reserved_stock_counts=True)), ["MUG-1"])

	def test_other_apps_can_request_a_push_after_commit(self):
		with (
			patch.object(frappe.db, "after_commit") as after_commit,
			patch.object(frappe.db, "after_rollback"),
		):
			enqueue_stock_update("MUG-1")

		after_commit.add.assert_called_once_with(push_pending_stock_updates)
		self.assertEqual(frappe.flags[PENDING_ITEMS_FLAG], {"MUG-1"})

	def test_a_rollback_drops_the_items(self):
		with (
			patch.object(frappe.db, "after_commit"),
			patch.object(frappe.db, "after_rollback") as after_rollback,
		):
			enqueue_stock_update("MUG-1")
		after_rollback.add.assert_called_once_with(forget_pending_stock_updates)
		forget_pending_stock_updates()

		self.assertNotIn(PENDING_ITEMS_FLAG, frappe.flags)


class TestDailyStockPush(IntegrationTestCase):
	def test_the_items_are_pushed_a_hundred_a_job(self):
		item_codes = [f"ITEM-{i:03}" for i in range(250)]
		with (
			patch.object(stock_update, "stock_sync_is_enabled", return_value=True),
			patch("frappe.get_all", return_value=item_codes),
			patch("frappe.enqueue") as enqueue,
		):
			update_stock_levels_for_all_enabled_items_in_background()

		self.assertEqual([len(call.kwargs["item_codes"]) for call in enqueue.call_args_list], [100, 100, 50])
