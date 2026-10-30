# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

from datetime import datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.exceptions import SyncDisabledError
from woocommerce_conduit.tasks import sync_sales_orders
from woocommerce_conduit.tasks.sync import SynchroniseWooCommerce
from woocommerce_conduit.tasks.sync_sales_orders import (
	SynchroniseSalesOrder,
	enqueue_sales_order_sync,
	get_names_of_wc_orders,
	sync_woocommerce_orders_modified_since,
	wc_order_changed,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
from woocommerce_conduit.woocommerce.woocommerce_api import map_frappe_filters_to_wc_params

SERVER = "one.example.com"


def make_sync(**kwargs) -> SynchroniseSalesOrder:
	# Neither the servers nor the settings are looked up
	with (
		patch.object(SynchroniseWooCommerce, "get_wc_servers", return_value=[]),
		patch.object(frappe, "get_cached_doc", return_value=_dict()),
	):
		return SynchroniseSalesOrder(**kwargs)


class TestFilterMapping(IntegrationTestCase):
	def test_id_filters_select_the_order(self):
		filters = [["WooCommerce Order", "id", "=", 12]]
		self.assertEqual(map_frappe_filters_to_wc_params(filters), {"include": [12]})

		filters = [["WooCommerce Order", "woocommerce_id", "in", [1, "2"]]]
		self.assertEqual(map_frappe_filters_to_wc_params(filters), {"include": "1,2"})

	def test_status_filters(self):
		filters = [["WooCommerce Order", "status", "in", ["pending", "processing"]]]
		self.assertEqual(map_frappe_filters_to_wc_params(filters), {"status": "pending,processing"})

		filters = [["WooCommerce Order", "status", "=", "pending"]]
		self.assertEqual(map_frappe_filters_to_wc_params(filters), {"status": ["pending"]})

	def test_unsupported_filter_is_an_error(self):
		with self.assertRaises(frappe.ValidationError):
			map_frappe_filters_to_wc_params([["WooCommerce Order", "customer_id", "=", 1]])


class TestStrictFetching(IntegrationTestCase):
	def fetch(self, **args):
		self.api = failing_api = MagicMock(
			woocommerce_server=SERVER, woocommerce_server_url=f"https://{SERVER}"
		)
		failing_api.get.return_value = _dict(status_code=500, text="boom", headers={})
		with (
			patch.object(WooCommerceOrder, "_init_api", return_value=[failing_api]),
			patch.object(frappe, "log_error"),
		):
			return WooCommerceOrder.get_list_of_records({"doctype": "WooCommerce Order", **args})

	def test_failing_server_is_skipped_by_default(self):
		self.assertEqual(self.fetch(), [])
		self.assertTrue(self.api.get.called)

	def test_failing_server_raises_when_strict(self):
		with self.assertRaises(ConnectionError):
			self.fetch(strict=True)

	def test_unsupported_filter_does_not_return_unrelated_orders(self):
		with self.assertRaises(frappe.ValidationError):
			self.fetch(filters=[["WooCommerce Order", "customer_id", "=", 1]])


class TestGetNamesOfOrders(IntegrationTestCase):
	def test_asks_for_the_modified_orders_with_a_status(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_dict(minimum_creation_date=None)),
			patch.object(
				WooCommerceOrder, "get_list_of_records", return_value=[{"name": "a~1"}, {"name": "b~2"}]
			) as get_list_of_records,
		):
			names = get_names_of_wc_orders("2026-10-30 10:00:00.123456", ["pending", "processing"])

		self.assertEqual(names, ["a~1", "b~2"])
		args = get_list_of_records.call_args.args[0]
		self.assertTrue(args["strict"])
		self.assertIn(["WooCommerce Order", "date_modified", ">", "2026-10-30T10:00:00"], args["filters"])
		self.assertIn(["WooCommerce Order", "status", "in", ["pending", "processing"]], args["filters"])


class TestSyncHash(IntegrationTestCase):
	def test_order_changed(self):
		hash_ = datetime(2026, 10, 30, 10, 0, 0)
		self.assertTrue(wc_order_changed("2026-10-30T10:00:00", None))
		self.assertFalse(wc_order_changed("2026-10-30T10:00:00", hash_))
		self.assertFalse(wc_order_changed("2026-10-30T09:59:59", hash_))
		self.assertTrue(wc_order_changed("2026-10-30T10:00:01", hash_))

	def sync_with(self, last_sync_hash):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		# A cancelled Sales Order skips the payment entry
		sync.sales_order = _dict(docstatus=2, woocommerce_last_sync_hash=last_sync_hash)
		sync.woocommerce_order = _dict(woocommerce_date_modified="2026-10-30T10:00:00")
		return sync

	def test_unchanged_order_is_not_pulled(self):
		sync = self.sync_with(datetime(2026, 10, 30, 10, 0, 0))
		with patch.object(sync, "update_sales_order") as update_sales_order:
			sync.sync_wc_order_with_erpnext_order()
		update_sales_order.assert_not_called()

	def test_changed_order_is_pulled(self):
		sync = self.sync_with(datetime(2026, 10, 30, 9, 0, 0))
		with patch.object(sync, "update_sales_order") as update_sales_order:
			sync.sync_wc_order_with_erpnext_order()
		update_sales_order.assert_called_once()

	def test_hash_is_stored_when_nothing_else_changed(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.sales_order = MagicMock(
			docstatus=1,
			woocommerce_status="Processing",
			woocommerce_customer_note=None,
			woocommerce_payment_method="Card",
			woocommerce_payment_entry="ACC-PAY-1",
			flags=_dict(),
		)
		sync.woocommerce_order = _dict(
			status="processing",
			customer_note=None,
			payment_method_title="Card",
			payment_method="stripe",
			woocommerce_date_modified="2026-10-30T10:00:00",
		)
		sync.update_sales_order()

		self.assertEqual(sync.sales_order.woocommerce_last_sync_hash, "2026-10-30T10:00:00")
		self.assertTrue(sync.sales_order.flags.created_by_sync)
		sync.sales_order.save.assert_called_once()


class TestOrderLookup(IntegrationTestCase):
	def test_sales_order_loads_its_own_woocommerce_order(self):
		sync = make_sync(sales_order_name="SAL-ORD-1")
		sync.sales_order = _dict(name="SAL-ORD-1", woocommerce_server=SERVER, woocommerce_id="12")
		loaded = _dict(name=f"{SERVER}~12")

		with (
			patch.object(frappe, "get_cached_doc", return_value=_dict(enabled=1)),
			patch.object(sync_sales_orders, "load_wc_order", return_value=loaded) as load_wc_order,
		):
			sync.get_corresponding_sales_order_or_woocommerce_order()

		load_wc_order.assert_called_once_with(f"{SERVER}~12")
		self.assertIs(sync.woocommerce_order, loaded)

	def test_disabled_server_is_not_synced(self):
		sync = make_sync(sales_order_name="SAL-ORD-1")
		sync.sales_order = _dict(name="SAL-ORD-1", woocommerce_server=SERVER, woocommerce_id="12")

		with (
			patch.object(frappe, "get_cached_doc", return_value=_dict(enabled=0)),
			self.assertRaises(SyncDisabledError),
		):
			sync.get_corresponding_sales_order_or_woocommerce_order()

	def test_unlinked_sales_order_is_an_error(self):
		sync = make_sync(sales_order_name="SAL-ORD-1")
		sync.sales_order = _dict(name="SAL-ORD-1", woocommerce_server=None, woocommerce_id=None)

		with self.assertRaises(frappe.ValidationError):
			sync.get_corresponding_sales_order_or_woocommerce_order()


class TestJobs(IntegrationTestCase):
	def test_jobs_get_names_and_are_deduplicated(self):
		with patch.object(frappe, "enqueue") as enqueue:
			enqueue_sales_order_sync(woocommerce_order_name=f"{SERVER}~12")

		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], f"woocommerce_order_sync::{SERVER}~12")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["woocommerce_order_name"], f"{SERVER}~12")
		self.assertIsNone(kwargs["sales_order_name"])

	def test_both_sides_of_an_order_share_a_lock(self):
		from_woocommerce = make_sync(woocommerce_order_name=f"{SERVER}~12")
		from_erpnext = make_sync(sales_order_name="SAL-ORD-1")

		with patch.object(
			frappe.db, "get_value", return_value=_dict(woocommerce_server=SERVER, woocommerce_id="12")
		):
			self.assertEqual(from_erpnext.get_lock_name(), from_woocommerce.get_lock_name())

		self.assertEqual(from_woocommerce.get_lock_name(), f"woocommerce_order_{SERVER}~12")

	def test_lock_name_is_a_safe_file_name(self):
		sync = make_sync(woocommerce_order_name="../shop:8080~1")
		self.assertNotIn("/", sync.get_lock_name())
		self.assertNotIn(":", sync.get_lock_name())


class TestHourlySync(IntegrationTestCase):
	def run_hourly(self, names):
		settings = MagicMock(wc_last_sync_date_orders="2026-10-30 09:00:00")
		seen = {}

		def get_names(**kwargs):
			seen.update(kwargs)
			# The cursor only moves once the orders are fetched
			seen["cursor_while_fetching"] = settings.wc_last_sync_date_orders
			return names

		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(sync_sales_orders, "now", return_value="2026-10-30 11:00:00"),
			patch.object(sync_sales_orders, "get_names_of_wc_orders", side_effect=get_names),
			patch.object(sync_sales_orders, "enqueue_sales_order_sync") as enqueue,
		):
			sync_woocommerce_orders_modified_since()
		return settings, seen, enqueue

	def test_syncs_since_the_last_run_and_moves_the_cursor(self):
		settings, seen, enqueue = self.run_hourly(names=["a~1", "b~2"])

		self.assertEqual(seen["date_time_from"], "2026-10-30 09:00:00")
		self.assertEqual(seen["cursor_while_fetching"], "2026-10-30 09:00:00")
		self.assertEqual(
			[c.kwargs for c in enqueue.call_args_list],
			[
				{"woocommerce_order_name": "a~1"},
				{"woocommerce_order_name": "b~2"},
			],
		)
		# The time before fetching, so nothing modified in the meantime is skipped
		self.assertEqual(settings.wc_last_sync_date_orders, "2026-10-30 11:00:00")
		settings.save.assert_called_once()

	def test_failed_fetch_keeps_the_cursor(self):
		settings = MagicMock(wc_last_sync_date_orders="2026-10-30 09:00:00")
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(sync_sales_orders, "get_names_of_wc_orders", side_effect=ConnectionError),
			patch.object(sync_sales_orders, "enqueue_sales_order_sync") as enqueue,
			self.assertRaises(ConnectionError),
		):
			sync_woocommerce_orders_modified_since()

		self.assertEqual(settings.wc_last_sync_date_orders, "2026-10-30 09:00:00")
		settings.save.assert_not_called()
		enqueue.assert_not_called()
