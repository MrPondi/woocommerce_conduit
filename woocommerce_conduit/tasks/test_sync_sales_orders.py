# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

import base64
import hashlib
import hmac
import json
from contextlib import nullcontext
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import frappe
from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit import endpoint
from woocommerce_conduit.exceptions import SyncDisabledError
from woocommerce_conduit.patches.v1 import set_customer_woocommerce_server
from woocommerce_conduit.tasks import order_import, order_lines, sync_sales_orders
from woocommerce_conduit.tasks.field_map import LOGGED_PROBLEMS_FLAG
from woocommerce_conduit.tasks.order_customer import (
	clean_phone,
	get_addresses_linking_to,
	normalise_tax_id,
)
from woocommerce_conduit.tasks.order_lines import (
	build_charges,
	get_line_rate,
	get_product_id,
	rates_match,
	totals_differ,
)
from woocommerce_conduit.tasks.order_status import (
	enqueue_status_push_from_hook,
	get_order_invoices,
	get_order_payment_entries,
	get_status_to_push,
	get_statuses_to_sync,
)
from woocommerce_conduit.tasks.sync_sales_orders import (
	SynchroniseSalesOrder,
	enqueue_sales_order_sync,
	find_sales_order,
	get_changed_order_names,
	get_names_of_wc_orders,
	get_payment_method,
	get_sales_order_name,
	sync_woocommerce_orders_modified_since,
	wc_order_changed,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_CANCEL,
	ACTION_CLOSE,
	ACTION_HOLD,
	ACTION_SKIP,
	ACTION_SUBMIT,
	ACTIONS,
	get_default_status_rows,
	is_active_status,
	normalise_status,
)
from woocommerce_conduit.woocommerce.woocommerce_api import map_frappe_filters_to_wc_params

SERVER = "one.example.com"

# Actions that statuses tick together
SUBMIT = frozenset({ACTION_SUBMIT})
SUBMIT_HOLD = frozenset({ACTION_SUBMIT, ACTION_HOLD})


def ticked(row: dict) -> set[str]:
	"""
	The actions that a row of the status map, as a dict, ticks
	"""
	return {action for action in ACTIONS if row.get(action)}


def make_server(rows=None, push=True, **fields):
	"""
	A WooCommerce Server with a status map, which is all the sync asks of it. The default map unless given.
	"""
	return frappe.get_doc(
		{
			"doctype": "WooCommerce Server",
			"name": SERVER,
			"woocommerce_server_url": f"https://{SERVER}",
			"enabled": 1,
			"enabled_order_status": 1 if push else 0,
			"sales_order_status_map": get_default_status_rows() if rows is None else rows,
			**fields,
		}
	)


def make_sync(**kwargs) -> SynchroniseSalesOrder:
	return SynchroniseSalesOrder(**kwargs)


def invoice_row(name="ACC-SINV-1", docstatus=1, **values):
	"""
	A Sales Invoice of the Sales Order as get_order_invoices returns it, unpaid
	"""
	return _dict(
		{
			"name": name,
			"docstatus": docstatus,
			"is_return": 0,
			"outstanding_amount": 100,
			"grand_total": 100,
			"rounded_total": 100,
			**values,
		}
	)


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

	def test_asks_only_the_servers_given(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_dict(minimum_creation_date=None)),
			patch.object(WooCommerceOrder, "get_list_of_records", return_value=[]) as get_list_of_records,
		):
			get_names_of_wc_orders(servers=[SERVER])

		self.assertEqual(get_list_of_records.call_args.args[0]["servers"], [SERVER])

	def test_the_listed_orders_are_looked_through_for_those_waiting_for_an_import(self):
		records = [{"name": "a~1", "status": "completed", "number": "1001"}]
		with (
			patch.object(frappe, "get_cached_doc", return_value=_dict(minimum_creation_date=None)),
			patch.object(
				WooCommerceOrder, "get_list_of_records", return_value=records
			) as get_list_of_records,
			patch.object(order_import, "notify_orders_to_import") as notify_orders_to_import,
		):
			self.assertEqual(get_names_of_wc_orders(), ["a~1"])

		notify_orders_to_import.assert_called_once_with(records)
		# The status and number tell them, in the same requests
		self.assertEqual(get_list_of_records.call_args.args[0]["fields"], ["name", "status", "number"])

	def test_a_notice_that_fails_does_not_hold_up_the_sync(self):
		with (
			patch.object(frappe, "get_cached_doc", return_value=_dict(minimum_creation_date=None)),
			patch.object(WooCommerceOrder, "get_list_of_records", return_value=[{"name": "a~1"}]),
			patch.object(order_import, "notify_orders_to_import", side_effect=ValueError("broken")),
			patch.object(frappe, "log_error") as log_error,
		):
			self.assertEqual(get_names_of_wc_orders(), ["a~1"])

		log_error.assert_called_once()


class TestSyncHash(IntegrationTestCase):
	def test_order_changed(self):
		hash_ = datetime(2026, 10, 30, 10, 0, 0)
		self.assertTrue(wc_order_changed("2026-10-30T10:00:00", None))
		self.assertFalse(wc_order_changed("2026-10-30T10:00:00", hash_))
		self.assertFalse(wc_order_changed("2026-10-30T09:59:59", hash_))
		self.assertTrue(wc_order_changed("2026-10-30T10:00:01", hash_))

	def sync_with(self, last_sync_hash):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.sales_order = _dict(docstatus=2, woocommerce_last_sync_hash=last_sync_hash)
		sync.woocommerce_order = _dict(
			status="whatever", woocommerce_server=SERVER, woocommerce_date_modified="2026-10-30T10:00:00"
		)
		return sync

	def test_unchanged_order_is_not_pulled(self):
		sync = self.sync_with(datetime(2026, 10, 30, 10, 0, 0))
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(sync, "update_sales_order") as update_sales_order,
		):
			sync.sync_wc_order_with_erpnext_order()
		update_sales_order.assert_not_called()

	def test_changed_order_is_pulled(self):
		sync = self.sync_with(datetime(2026, 10, 30, 9, 0, 0))
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(sync, "update_sales_order") as update_sales_order,
		):
			sync.sync_wc_order_with_erpnext_order()
		# The map doesn't know the status, so it isn't active
		update_sales_order.assert_called_once_with(frozenset(), active=False)

	def test_hash_is_stored_when_nothing_else_changed(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.sales_order = MagicMock(
			docstatus=1,
			woocommerce_status="Processing",
			woocommerce_customer_note=None,
			woocommerce_payment_method="Card",
			flags=_dict(),
		)
		sync.woocommerce_order = _dict(
			status="processing",
			customer_note=None,
			payment_method_title="Card",
			payment_method="stripe",
			woocommerce_date_modified="2026-10-30T10:00:00",
		)
		# The lines of a mock can't be updated, and the failure would be logged as an Error Log
		with patch.object(sync, "update_sales_order_items"):
			sync.update_sales_order()

		sync.sales_order.db_set.assert_called_once()
		bookkeeping = sync.sales_order.db_set.call_args
		self.assertEqual(bookkeeping.args[0]["woocommerce_last_sync_hash"], datetime(2026, 10, 30, 10, 0, 0))
		self.assertEqual(bookkeeping.kwargs, {"update_modified": False})
		self.assertTrue(sync.sales_order.flags.created_by_sync)
		sync.sales_order.save.assert_not_called()


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

	def find(self, *rows):
		with patch.object(
			frappe, "get_all", return_value=[_dict(name=n, docstatus=d) for n, d in rows]
		) as get_all:
			name = find_sales_order(SERVER, 12)
		self.assertEqual(
			get_all.call_args.kwargs["filters"], {"woocommerce_server": SERVER, "woocommerce_id": "12"}
		)
		return name

	def test_the_amendment_of_a_cancelled_order_is_its_sales_order(self):
		# Newest first
		self.assertEqual(self.find(("SAL-ORD-1-1", 1), ("SAL-ORD-1", 2)), "SAL-ORD-1-1")

	def test_a_cancelled_order_is_found_so_it_is_not_made_again(self):
		self.assertEqual(self.find(("SAL-ORD-1", 2)), "SAL-ORD-1")
		self.assertIsNone(self.find())

	def test_a_product_the_shop_no_longer_has_is_left_to_the_placeholder(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(
			woocommerce_server=SERVER,
			line_items=json.dumps(
				[{"id": 1, "product_id": 10, "variation_id": 0}, {"id": 2, "product_id": 20}]
			),
		)
		with (
			patch.object(order_lines, "find_item_code", return_value=None),
			patch.object(
				order_lines, "run_item_sync", side_effect=[frappe.DoesNotExistError, (None, None)]
			) as run_item_sync,
		):
			sync.create_missing_items()

		self.assertEqual(run_item_sync.call_count, 2)

	def test_an_address_without_a_country_is_in_the_companys(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(woocommerce_server=SERVER)
		values = {
			("WooCommerce Server", SERVER, "company"): "Test Company",
			("Company", "Test Company", "country"): "Poland",
		}
		with (
			patch.object(frappe.db, "get_value", return_value=None),
			patch.object(frappe, "get_cached_value", side_effect=lambda *key: values[key]),
		):
			self.assertEqual(sync.get_country(""), "Poland")
			self.assertEqual(sync.get_country("XX"), "Poland")


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


class TestPausedSync(IntegrationTestCase):
	def paused(self, value=1):
		return patch.object(
			frappe,
			"get_cached_value",
			side_effect=lambda doctype, name, field: value if name == SERVER else 0,
		)

	def test_orders_of_a_paused_server_are_not_queued(self):
		with self.paused(), patch.object(frappe, "enqueue") as enqueue:
			enqueue_sales_order_sync(woocommerce_order_name=f"{SERVER}~12")
			enqueue_sales_order_sync(woocommerce_order_name="two.example.com~12")
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(enqueue.call_args.kwargs["woocommerce_order_name"], "two.example.com~12")

	def test_paused_server_is_not_synced(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		with self.paused(), patch.object(sync, "load_orders") as load_orders:
			sync.run()
		load_orders.assert_not_called()

	def test_sync_asked_for_in_the_form_says_it_is_paused(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		with (
			self.paused(),
			patch.object(frappe, "request", MagicMock(), create=True),
			self.assertRaises(frappe.ValidationError),
		):
			sync.run()

	def test_unpausing_syncs_the_orders_of_that_server(self):
		with (
			patch.object(
				sync_sales_orders, "get_names_of_wc_orders", return_value=[f"{SERVER}~12", f"{SERVER}~13"]
			) as get_names,
			patch.object(sync_sales_orders, "get_statuses_to_sync", return_value=["pending"]),
			patch.object(sync_sales_orders, "enqueue_sales_order_sync") as enqueue,
		):
			sync_sales_orders.sync_paused_orders(SERVER, since="2026-10-30 09:00:00")

		self.assertEqual(get_names.call_args.kwargs["date_time_from"], "2026-10-30 09:00:00")
		# Only that server is read, so the other shops' orders don't count against the limit of a sync
		self.assertEqual(get_names.call_args.kwargs["servers"], [SERVER])
		self.assertEqual(
			[c.kwargs["woocommerce_order_name"] for c in enqueue.call_args_list],
			[f"{SERVER}~12", f"{SERVER}~13"],
		)

	def test_unpausing_without_a_date_does_not_sync_the_whole_shop(self):
		with (
			patch.object(sync_sales_orders, "get_names_of_wc_orders") as get_names,
			patch.object(sync_sales_orders, "enqueue_sales_order_sync") as enqueue,
		):
			sync_sales_orders.sync_paused_orders(SERVER, since=None)
		get_names.assert_not_called()
		enqueue.assert_not_called()


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
			patch.object(frappe.db, "set_single_value") as set_single_value,
		):
			sync_woocommerce_orders_modified_since()
		self.set_single_value = set_single_value
		return settings, seen, enqueue

	def test_syncs_since_the_last_run_and_moves_the_cursor(self):
		_settings, seen, enqueue = self.run_hourly(names=["a~1", "b~2"])

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
		self.set_single_value.assert_called_once_with(
			"WooCommerce Settings", "wc_last_sync_date_orders", "2026-10-30 11:00:00"
		)

	def test_failed_fetch_keeps_the_cursor(self):
		settings = MagicMock(wc_last_sync_date_orders="2026-10-30 09:00:00")
		with (
			patch.object(frappe, "get_cached_doc", return_value=settings),
			patch.object(sync_sales_orders, "get_names_of_wc_orders", side_effect=ConnectionError),
			patch.object(sync_sales_orders, "enqueue_sales_order_sync") as enqueue,
			patch.object(frappe.db, "set_single_value") as set_single_value,
			self.assertRaises(ConnectionError),
		):
			sync_woocommerce_orders_modified_since()

		set_single_value.assert_not_called()
		enqueue.assert_not_called()

	def test_orders_the_webhook_synced_already_are_left_out(self):
		records = [
			{"name": "a~1", "woocommerce_date_modified": "2026-10-30T10:00:00"},
			{"name": "a~2", "woocommerce_date_modified": "2026-10-30T10:30:00"},
			{"name": "a~3", "woocommerce_date_modified": "2026-10-30T10:00:00"},
		]
		sales_orders = [
			# Synced by the webhook
			_dict(woocommerce_id="1", woocommerce_last_sync_hash=datetime(2026, 10, 30, 10, 0, 0)),
			# Changed again since
			_dict(woocommerce_id="2", woocommerce_last_sync_hash=datetime(2026, 10, 30, 10, 0, 0)),
		]
		with patch.object(frappe, "get_all", return_value=sales_orders):
			self.assertEqual(get_changed_order_names(records), ["a~2", "a~3"])


class TestStatusMap(IntegrationTestCase):
	def test_core_statuses_have_defaults(self):
		rows = {row["woocommerce_status"]: row for row in get_default_status_rows()}

		self.assertEqual(ticked(rows["checkout-draft"]), {ACTION_SKIP})
		self.assertEqual(ticked(rows["pending"]), SUBMIT)
		self.assertEqual(ticked(rows["failed"]), {ACTION_CANCEL})
		# An order first seen completed is for a WooCommerce Order Import
		self.assertEqual(ticked(rows["completed"]), set())
		self.assertEqual(ticked(rows["on-hold"]), SUBMIT_HOLD)
		self.assertEqual(ticked(rows["processing"]), SUBMIT)
		self.assertEqual(ticked(rows["cancelled"]), {ACTION_CANCEL})
		self.assertEqual(ticked(rows["refunded"]), {ACTION_CLOSE})
		self.assertEqual(rows["completed"]["push_from_erpnext_status"], "Completed")
		self.assertEqual(rows["cancelled"]["push_from_erpnext_status"], "Cancelled")

	def test_statuses_of_the_shop_are_added(self):
		live = {"wc-processing": "Being processed", "ready-pickup": "Ready for pickup"}
		rows = {row["woocommerce_status"]: row for row in get_default_status_rows(live)}

		# A plugin status does nothing until the user decides what it means
		self.assertEqual(ticked(rows["ready-pickup"]), set())
		self.assertEqual(rows["ready-pickup"]["woocommerce_status_label"], "Ready for pickup")
		# The core status keeps its actions, with the name of the shop
		self.assertEqual(rows["processing"]["woocommerce_status_label"], "Being processed")
		self.assertEqual(ticked(rows["processing"]), SUBMIT)

	def test_existing_rows_are_not_added_again(self):
		rows = get_default_status_rows({"ready-pickup": "Ready"}, existing=["wc-pending", "ready-pickup"])
		slugs = [row["woocommerce_status"] for row in rows]
		self.assertNotIn("pending", slugs)
		self.assertNotIn("ready-pickup", slugs)
		self.assertIn("processing", slugs)

	def test_status_slug_is_normalised(self):
		self.assertEqual(normalise_status("wc-processing"), "processing")
		self.assertEqual(normalise_status(" processing "), "processing")
		self.assertEqual(normalise_status(None), "")

	def test_new_server_starts_with_the_defaults(self):
		server = make_server(rows=[])
		server.before_insert()
		self.assertEqual(
			[row.woocommerce_status for row in server.sales_order_status_map],
			[row["woocommerce_status"] for row in get_default_status_rows()],
		)

	def test_existing_map_is_kept_on_insert(self):
		server = make_server(rows=[{"woocommerce_status": "pending", ACTION_SKIP: 1}])
		server.before_insert()
		self.assertEqual(len(server.sales_order_status_map), 1)

	def test_lookup_by_status(self):
		server = make_server()
		self.assertEqual(server.get_order_status_row("wc-on-hold").get_actions(), SUBMIT_HOLD)
		self.assertIsNone(server.get_order_status_row("quote-sent"))
		self.assertEqual(server.get_push_row("Completed").woocommerce_status, "completed")
		self.assertIsNone(server.get_push_row("To Deliver"))
		self.assertIsNone(server.get_push_row(None))

	def test_default_map_is_valid(self):
		make_server().validate_so_status_map()

	def test_actions_that_go_together_are_valid(self):
		for actions in (SUBMIT, {ACTION_HOLD}, SUBMIT_HOLD, set()):
			rows = [{"woocommerce_status": "processing", **dict.fromkeys(actions, 1)}]
			make_server(rows).validate_so_status_map()

	def test_cancel_close_and_skip_are_ticked_alone(self):
		for actions in (
			{ACTION_CANCEL, ACTION_SUBMIT},
			{ACTION_CLOSE, ACTION_HOLD},
			{ACTION_SKIP, ACTION_HOLD},
			{ACTION_CANCEL, ACTION_CLOSE},
		):
			rows = [{"woocommerce_status": "processing", **dict.fromkeys(actions, 1)}]
			with self.assertRaises(frappe.ValidationError, msg=actions):
				make_server(rows).validate_so_status_map()

	def test_statuses_of_orders_that_go_on_are_active(self):
		server = make_server()
		for status in ("pending", "processing", "on-hold", "completed"):
			self.assertTrue(is_active_status(server.get_order_status_row(status)), status)
		for status in ("checkout-draft", "failed", "cancelled", "refunded", "trash"):
			self.assertFalse(is_active_status(server.get_order_status_row(status)), status)
		# A status the map doesn't know leaves ERPNext as it is
		self.assertFalse(is_active_status(server.get_order_status_row("quote-sent")))

	def test_a_status_can_only_be_in_the_map_once(self):
		rows = [
			{"woocommerce_status": "completed", ACTION_SKIP: 1},
			{"woocommerce_status": "wc-completed", ACTION_SKIP: 1},
		]
		with self.assertRaises(frappe.ValidationError):
			make_server(rows).validate_so_status_map()

	def test_a_sales_order_status_is_pushed_as_one_status(self):
		rows = [
			{"woocommerce_status": "completed", ACTION_SKIP: 1, "push_from_erpnext_status": "Completed"},
			{"woocommerce_status": "delivered", ACTION_SKIP: 1, "push_from_erpnext_status": "Completed"},
		]
		with self.assertRaises(frappe.ValidationError):
			make_server(rows).validate_so_status_map()

	def test_statuses_of_the_gateway_are_not_pushed(self):
		for status in ("pending", "failed", "on-hold", "processing", "checkout-draft"):
			rows = [{"woocommerce_status": status, ACTION_SKIP: 1, "push_from_erpnext_status": "To Bill"}]
			with self.assertRaises(frappe.ValidationError, msg=status):
				make_server(rows).validate_so_status_map()


class TestCreatingOrders(IntegrationTestCase):
	def sync_new_order(self, status, rows=None):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(
			status=status, woocommerce_server=SERVER, woocommerce_date_modified="2026-10-30T10:00:00"
		)
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server(rows)),
			patch.object(sync, "create_sales_order") as create_sales_order,
		):
			sync.sync_wc_order_with_erpnext_order()
		return create_sales_order

	def test_orders_that_mean_nothing_are_not_created(self):
		# Making a Sales Order also makes a Customer, Address and Contact, so none of these is
		for status in ("checkout-draft", "failed", "cancelled", "trash", "refunded", "quote-sent"):
			self.sync_new_order(status).assert_not_called()

	def test_only_submit_creates_an_order(self):
		# A status that ticks nothing only acts on a Sales Order that exists
		rows = [{"woocommerce_status": "processing"}]
		self.sync_new_order("processing", rows).assert_not_called()

	def test_orders_that_are_wanted_are_created(self):
		self.sync_new_order("pending").assert_called_once_with(SUBMIT)
		self.sync_new_order("processing").assert_called_once_with(SUBMIT)
		self.sync_new_order("on-hold").assert_called_once_with(SUBMIT_HOLD)

	def test_an_order_first_seen_completed_is_for_an_order_import(self):
		# The import makes it with its invoice and payment, which the sync never makes
		self.sync_new_order("completed").assert_not_called()

		# Unless a person ticks Submit, which makes the Sales Order alone
		rows = [{"woocommerce_status": "completed", ACTION_SUBMIT: 1}]
		self.sync_new_order("completed", rows).assert_called_once_with(SUBMIT)

	def test_order_seen_before_is_not_new(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.sales_order = MagicMock(woocommerce_last_sync_hash="2026-10-30 10:00:00")
		sync.woocommerce_order = _dict(
			status="processing", woocommerce_server=SERVER, woocommerce_date_modified="2026-10-30T10:00:00"
		)
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(sync, "create_sales_order") as create_sales_order,
			patch.object(sync, "update_sales_order") as update_sales_order,
		):
			sync.sync_wc_order_with_erpnext_order()
		create_sales_order.assert_not_called()
		# WooCommerce didn't change it since
		update_sales_order.assert_not_called()


class TestPullingStatus(IntegrationTestCase):
	def pull(self, previous_status, status, actions):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		calls = []
		sync.sales_order = MagicMock(
			docstatus=1,
			woocommerce_status=previous_status,
			woocommerce_customer_note=None,
			woocommerce_payment_method="Card",
			flags=_dict(),
		)

		def db_set(values, **kwargs):
			calls.append("save")
			for fieldname, value in values.items():
				setattr(sync.sales_order, fieldname, value)

		sync.sales_order.db_set.side_effect = db_set
		sync.woocommerce_order = _dict(
			status=status,
			customer_note=None,
			payment_method_title="Card",
			payment_method="stripe",
			woocommerce_date_modified="2026-10-30T10:00:00",
		)
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(
				sync, "apply_status_actions", side_effect=lambda actions, active=False: calls.append(actions)
			),
			patch.object(sync, "update_sales_order_items", side_effect=lambda: calls.append("lines")),
		):
			sync.update_sales_order(actions)
		return sync, calls

	def test_status_is_stored_as_the_slug(self):
		sync, _calls = self.pull("pending", "wc-processing", SUBMIT)
		self.assertEqual(sync.sales_order.woocommerce_status, "processing")

	def test_action_follows_the_lines_when_the_status_changed(self):
		_sync, calls = self.pull("pending", "processing", SUBMIT)
		self.assertEqual(calls, ["save", "lines", SUBMIT])

	def test_lines_of_a_cancelled_order_are_left(self):
		_sync, calls = self.pull("pending", "failed", {ACTION_CANCEL})
		self.assertEqual(calls, ["save", {ACTION_CANCEL}])

	def test_action_is_not_repeated_for_other_changes(self):
		# The note of an order was edited: a hold put on the Sales Order in ERPNext must stay
		_sync, calls = self.pull("processing", "processing", SUBMIT)
		self.assertEqual(calls, ["save", "lines"])


class TestStatusActions(IntegrationTestCase):
	def make(self, invoices=(), payment_entries=(), **values):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.get_invoices = MagicMock(return_value=list(invoices))
		sync.get_payment_entries = MagicMock(return_value=list(payment_entries))
		sync.sales_order = MagicMock(
			**{
				"docstatus": 1,
				"status": "To Deliver and Bill",
				"per_delivered": 0,
				"per_billed": 0,
				"advance_paid": 0,
				"woocommerce_sync_issue": None,
				"flags": _dict(),
				**values,
			}
		)
		sync.sales_order.name = "SAL-ORD-1"
		return sync

	def test_changes_are_not_pushed_back(self):
		sync = self.make()
		sync.apply_status_actions(frozenset())
		self.assertTrue(sync.sales_order.flags.created_by_sync)

	def test_hold_and_resume(self):
		sync = self.make()
		sync.apply_status_actions(SUBMIT_HOLD)
		sync.sales_order.update_status.assert_called_once_with("On Hold")

		sync = self.make(status="On Hold")
		sync.apply_status_actions(SUBMIT_HOLD, active=True)
		sync.sales_order.update_status.assert_not_called()
		sync.apply_status_actions(SUBMIT, active=True)
		sync.sales_order.update_status.assert_called_once_with("Draft")

	def test_resume_leaves_an_order_that_is_not_on_hold(self):
		sync = self.make()
		sync.apply_status_actions(SUBMIT, active=True)
		sync.sales_order.update_status.assert_not_called()

	def test_an_active_status_that_ticks_nothing_lifts_a_hold(self):
		# Completed, whose Invoice and Deliver lifted it before they left the map
		sync = self.make(status="On Hold")
		sync.apply_status_actions(frozenset(), active=True)
		sync.sales_order.update_status.assert_called_once_with("Draft")

	def test_hold_without_submit(self):
		sync = self.make()
		sync.apply_status_actions({ACTION_HOLD})
		sync.sales_order.update_status.assert_called_once_with("On Hold")

	def test_close(self):
		sync = self.make()
		sync.apply_status_actions({ACTION_CLOSE})
		sync.sales_order.update_status.assert_called_once_with("Closed")

	def test_skip_and_unknown_statuses_leave_the_order(self):
		# Skip isn't active, and an empty set without a row is a status the map doesn't know
		for actions in ({ACTION_SKIP}, frozenset()):
			sync = self.make(status="On Hold")
			sync.apply_status_actions(actions)
			sync.sales_order.update_status.assert_not_called()

	def test_unpaid_order_is_cancelled(self):
		sync = self.make()
		sync.apply_status_actions({ACTION_CANCEL})
		sync.sales_order.cancel.assert_called_once()
		sync.sales_order.update_status.assert_not_called()

	def test_an_order_on_hold_is_cancelled_too(self):
		sync = self.make(status="On Hold")
		sync.apply_status_actions({ACTION_CANCEL})
		sync.sales_order.cancel.assert_called_once()

	def test_an_order_with_an_invoice_a_payment_or_a_delivery_is_closed_and_flagged(self):
		# An invoice is a person's, made when its payment was processed, so the sync cancels or deletes none.
		# ERPNext wouldn't cancel the order while a draft bills it either.
		for values in (
			{"invoices": [invoice_row(docstatus=0)]},
			{"invoices": [invoice_row()], "per_billed": 100},
			{"invoices": [invoice_row(is_return=1)]},
			{"payment_entries": ["ACC-PAY-1"]},
			{"per_delivered": 50},
			{"advance_paid": 50},
		):
			with self.subTest(values):
				sync = self.make(**values)
				with patch.object(frappe, "get_doc") as get_doc:
					sync.apply_status_actions({ACTION_CANCEL})

				get_doc.assert_not_called()
				sync.sales_order.cancel.assert_not_called()
				sync.sales_order.update_status.assert_called_once_with("Closed")
				sync.sales_order.db_set.assert_called_once()
				self.assertEqual(sync.sales_order.db_set.call_args.args[0], "woocommerce_sync_issue")

	def test_a_closed_order_stays_closed(self):
		# ERPNext won't cancel a closed Sales Order
		sync = self.make(status="Closed")
		sync.apply_status_actions({ACTION_CANCEL})

		sync.sales_order.cancel.assert_not_called()
		sync.sales_order.update_status.assert_not_called()
		self.assertEqual(sync.sales_order.db_set.call_args.args[0], "woocommerce_sync_issue")


class TestPushingStatus(IntegrationTestCase):
	def make_sales_order(self, **values):
		return MagicMock(
			**{
				"docstatus": 1,
				"status": "Completed",
				"woocommerce_server": SERVER,
				"woocommerce_id": "12",
				"woocommerce_status": "processing",
				"woocommerce_last_sync_hash": datetime(2026, 10, 30, 10, 0, 0),
				"flags": _dict(),
				**values,
			}
		)

	def test_status_to_push(self):
		with patch.object(frappe, "get_cached_doc", return_value=make_server()):
			self.assertEqual(get_status_to_push(self.make_sales_order()), "completed")
			# Already there
			self.assertIsNone(get_status_to_push(self.make_sales_order(woocommerce_status="completed")))
			# Not a status that is pushed
			self.assertIsNone(get_status_to_push(self.make_sales_order(status="To Deliver")))
			# Not linked
			self.assertIsNone(get_status_to_push(self.make_sales_order(woocommerce_id=None)))
			# In the trash, or deleted: a status would take it out of the trash
			self.assertIsNone(get_status_to_push(self.make_sales_order(woocommerce_status="trash")))

	def test_pushing_can_be_switched_off(self):
		with patch.object(frappe, "get_cached_doc", return_value=make_server(push=False)):
			self.assertIsNone(get_status_to_push(self.make_sales_order()))

	def test_hook_enqueues_a_job_by_name(self):
		doc = self.make_sales_order()
		doc.name = "SAL-ORD-1"
		with (
			patch.object(frappe, "in_test", False),
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(frappe, "enqueue") as enqueue,
		):
			enqueue_status_push_from_hook(doc)

		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["sales_order_name"], "SAL-ORD-1")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])

	def test_hook_ignores_changes_made_by_the_sync_and_drafts(self):
		for values in ({"flags": _dict(created_by_sync=True)}, {"docstatus": 0}, {"status": "To Deliver"}):
			doc = self.make_sales_order(**values)
			doc.name = "SAL-ORD-1"
			with (
				patch.object(frappe, "in_test", False),
				patch.object(frappe, "get_cached_doc", return_value=make_server()),
				patch.object(frappe, "enqueue") as enqueue,
			):
				enqueue_status_push_from_hook(doc)
			enqueue.assert_not_called()

	def test_hook_pushes_nothing_while_an_order_import_runs(self):
		# ERPNext changes the status of an imported Sales Order on a copy without the sync's flag, and the
		# shop's old orders must stay as they are
		doc = self.make_sales_order()
		doc.name = "SAL-ORD-1"
		with (
			patch.object(frappe, "in_test", False),
			patch.dict(frappe.flags, {"woocommerce_order_import": True}),
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(frappe, "enqueue") as enqueue,
		):
			enqueue_status_push_from_hook(doc)
		enqueue.assert_not_called()

	def push(self, wc_status="processing", wc_modified="2026-10-30T10:00:00", **sales_order_values):
		sync = make_sync(sales_order_name="SAL-ORD-1")
		sync.sales_order = self.make_sales_order(**sales_order_values)
		sync.woocommerce_order = MagicMock(
			status=wc_status, woocommerce_date_modified=wc_modified, flags=_dict()
		)

		def save():
			# WooCommerce answers with the date it changed the order
			sync.woocommerce_order.woocommerce_date_modified = "2026-10-30T10:05:00"

		sync.woocommerce_order.save.side_effect = save
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(sync, "sync_wc_order_with_erpnext_order") as pull,
		):
			sync.push_status_to_woocommerce()
		return sync, pull

	def test_status_is_pushed_and_its_date_kept(self):
		sync, pull = self.push()

		self.assertEqual(sync.woocommerce_order.status, "completed")
		sync.woocommerce_order.save.assert_called_once()
		pull.assert_not_called()
		values = sync.sales_order.db_set.call_args.args[0]
		self.assertEqual(values["woocommerce_status"], "completed")
		# So the next pull sees nothing new
		self.assertEqual(values["woocommerce_last_sync_hash"], datetime(2026, 10, 30, 10, 5, 0))

	def test_woocommerce_wins_when_it_changed_in_the_meantime(self):
		# The gateway marked the order paid after the Sales Order was last synced
		sync, pull = self.push(wc_status="processing", wc_modified="2026-10-30T10:03:00")

		sync.woocommerce_order.save.assert_not_called()
		sync.sales_order.db_set.assert_not_called()
		pull.assert_called_once()

	def test_nothing_is_written_when_woocommerce_has_the_status(self):
		sync, _pull = self.push(wc_status="completed")

		sync.woocommerce_order.save.assert_not_called()
		self.assertEqual(sync.sales_order.db_set.call_args.args[0]["woocommerce_status"], "completed")


class TestDeletedOrders(IntegrationTestCase):
	"""Orders that the shop deleted for good, which it answers are an invalid id"""

	def test_an_order_the_shop_deleted_is_noted_rather_than_an_error(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		with (
			patch.object(sync_sales_orders, "load_wc_order", side_effect=frappe.DoesNotExistError),
			patch.object(sync_sales_orders, "find_sales_order", return_value=None) as find_sales_order,
		):
			sync.load_orders()
			sync.get_corresponding_sales_order_or_woocommerce_order()

		self.assertTrue(sync.deleted_in_woocommerce)
		self.assertIsNone(sync.woocommerce_order)
		# Looked up by the order's name, as there is no order to read it from
		find_sales_order.assert_called_once_with(SERVER, 12)

	def test_a_sales_order_whose_order_was_deleted_notes_it_too(self):
		sync = make_sync(sales_order_name="SAL-ORD-1")
		sync.sales_order = _dict(name="SAL-ORD-1", woocommerce_server=SERVER, woocommerce_id="12")
		with (
			patch.object(frappe, "get_cached_doc", return_value=_dict(enabled=1)),
			patch.object(sync_sales_orders, "load_wc_order", side_effect=frappe.DoesNotExistError),
		):
			sync.get_corresponding_sales_order_or_woocommerce_order()
		self.assertTrue(sync.deleted_in_woocommerce)

	def deleted(self, **values):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.deleted_in_woocommerce = True
		sync.sales_order = MagicMock(
			**{
				"docstatus": 1,
				"woocommerce_server": SERVER,
				"woocommerce_status": "processing",
				"flags": _dict(),
				**values,
			}
		)
		return sync

	def sync_deleted(self, sync, push=False):
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(sync, "apply_status_actions") as apply_status_actions,
		):
			if push:
				sync.push_status_to_woocommerce()
			else:
				sync.sync_wc_order_with_erpnext_order()
		return apply_status_actions

	def test_its_sales_order_does_what_the_trash_row_ticks_once(self):
		sync = self.deleted()
		apply_status_actions = self.sync_deleted(sync)

		sync.sales_order.db_set.assert_called_once_with("woocommerce_status", "trash", update_modified=False)
		self.assertTrue(sync.sales_order.flags.created_by_sync)
		apply_status_actions.assert_called_once_with({ACTION_CANCEL})

		# Trashed before, or synced as deleted already
		sync = self.deleted(woocommerce_status="trash")
		self.sync_deleted(sync).assert_not_called()
		sync.sales_order.db_set.assert_not_called()

	def test_without_a_sales_order_or_with_a_cancelled_one_nothing_is_done(self):
		sync = self.deleted()
		sync.sales_order = None
		self.sync_deleted(sync).assert_not_called()

		sync = self.deleted(docstatus=2)
		self.sync_deleted(sync).assert_not_called()
		sync.sales_order.db_set.assert_not_called()

	def test_a_push_takes_the_deletion_instead(self):
		# Deleting the order is the change WooCommerce made, and there is no order to push to
		sync = self.deleted()
		self.sync_deleted(sync, push=True).assert_called_once_with({ACTION_CANCEL})


class TestComebacks(IntegrationTestCase):
	"""Orders that are back in WooCommerce after their status cancelled or closed the Sales Order"""

	def pull(self, previous_status, wc_status, amendment=None, **values):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.sales_order = MagicMock(
			**{
				"docstatus": 1,
				"status": "To Deliver and Bill",
				"woocommerce_status": previous_status,
				"woocommerce_last_sync_hash": datetime(2026, 10, 30, 9, 0, 0),
				"woocommerce_sync_issue": None,
				"flags": _dict(),
				**values,
			}
		)
		sync.sales_order.name = "SAL-ORD-1"
		sync.woocommerce_order = _dict(
			status=wc_status,
			woocommerce_server=SERVER,
			customer_note=None,
			payment_method_title="Card",
			payment_method="stripe",
			woocommerce_date_modified="2026-10-30T10:00:00",
		)
		self.cancelled = sync.sales_order
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(frappe, "copy_doc", return_value=amendment) as self.copy_doc,
			patch.object(frappe, "log_error") as self.log_error,
			patch.object(sync, "update_sales_order_items"),
			patch.object(sync, "apply_status_actions") as self.apply_status_actions,
		):
			sync.sync_wc_order_with_erpnext_order()
		return sync

	def make_amendment(self, **values):
		amendment = MagicMock(
			**{
				"docstatus": 2,
				"status": "Cancelled",
				"woocommerce_status": "failed",
				"flags": _dict(),
				**values,
			}
		)
		amendment.submit.side_effect = lambda: setattr(amendment, "docstatus", 1)
		return amendment

	def test_a_cancelled_sales_order_is_amended_and_synced_on(self):
		# Failed and cancelled, then paid on a retry
		amendment = self.make_amendment()
		sync = self.pull("failed", "processing", amendment, docstatus=2, status="Cancelled")

		self.copy_doc.assert_called_once_with(self.cancelled)
		self.assertEqual(amendment.amended_from, "SAL-ORD-1")
		amendment.insert.assert_called_once()
		self.assertEqual(amendment.docstatus, 1)
		self.assertTrue(amendment.flags.created_by_sync)
		self.assertTrue(amendment.flags.ignore_mandatory)
		self.assertIs(sync.sales_order, amendment)
		# The pull carries on with the amendment: its status, lines and actions
		self.assertEqual(amendment.db_set.call_args.args[0]["woocommerce_status"], "processing")
		self.apply_status_actions.assert_called_once_with(SUBMIT, True)
		self.log_error.assert_not_called()

	def test_an_order_back_as_completed_is_amended_too(self):
		# Completed makes no Sales Order, and the Order Import leaves out an order that has one, so the order
		# would be lost to both
		amendment = self.make_amendment()
		sync = self.pull("failed", "completed", amendment, docstatus=2, status="Cancelled")

		self.copy_doc.assert_called_once_with(self.cancelled)
		self.assertIs(sync.sales_order, amendment)
		self.apply_status_actions.assert_called_once_with(frozenset(), True)

	def test_an_amendment_that_fails_is_flagged_on_the_cancelled_sales_order(self):
		amendment = self.make_amendment()
		amendment.insert.side_effect = frappe.ValidationError("Item MUG is disabled")
		sync = self.pull("trash", "processing", amendment, docstatus=2, status="Cancelled")

		self.assertIs(sync.sales_order, self.cancelled)
		self.log_error.assert_called_once()
		fieldname, issue = self.cancelled.db_set.call_args.args
		self.assertEqual(fieldname, "woocommerce_sync_issue")
		self.assertIn("Item MUG is disabled", issue)
		# Flagged without its status being pushed back
		self.assertTrue(self.cancelled.flags.created_by_sync)
		self.apply_status_actions.assert_not_called()

	def test_a_closed_sales_order_is_reopened_and_flagged(self):
		# Paid when it was trashed, so the trash closed it rather than cancel it
		sync = self.pull("trash", "processing", status="Closed")

		sync.sales_order.update_status.assert_called_once_with("Draft")
		self.assertEqual(
			sync.sales_order.db_set.call_args.args,
			("woocommerce_sync_issue", "Back in WooCommerce as processing, so it was reopened"),
		)
		self.apply_status_actions.assert_called_once_with(SUBMIT, True)

	def test_an_order_whose_status_did_not_end_its_sales_order_is_left(self):
		# Cancelled or closed in ERPNext by a person, while the shop went on with the order
		sync = self.pull("processing", "completed", docstatus=2, status="Cancelled")
		self.copy_doc.assert_not_called()
		sync.sales_order.db_set.assert_not_called()

		sync = self.pull("processing", "completed", status="Closed")
		sync.sales_order.update_status.assert_not_called()


class TestSalesOrderNames(IntegrationTestCase):
	def sales_order_name(self, order_number="1234", taken=False, **server):
		wc_server = _dict({"name_sales_orders_by_number": 1, "sales_order_prefix": "WEB-", **server})
		with patch.object(frappe.db, "exists", return_value=taken):
			return get_sales_order_name(wc_server, order_number)

	def test_a_server_can_name_them_by_the_order_number(self):
		self.assertEqual(self.sales_order_name(), "WEB-1234")
		self.assertEqual(self.sales_order_name(order_number=1234, sales_order_prefix=" KL/ "), "KL/1234")
		# The number alone
		self.assertEqual(self.sales_order_name(sales_order_prefix=None), "1234")

	def test_the_naming_series_names_them_otherwise(self):
		self.assertIsNone(self.sales_order_name(name_sales_orders_by_number=0))
		# By another shop's order, or a Sales Order made by hand
		self.assertIsNone(self.sales_order_name(taken=True))
		# Not a name Frappe takes
		self.assertIsNone(self.sales_order_name(order_number="<1234>"))
		self.assertIsNone(self.sales_order_name(order_number="1" * 140))
		self.assertIsNone(self.sales_order_name(order_number=""))


class TestStatusesToSync(IntegrationTestCase):
	def test_only_statuses_that_do_something(self):
		with (
			patch.object(frappe, "get_all", return_value=[SERVER]),
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
		):
			statuses = get_statuses_to_sync()

		self.assertNotIn("checkout-draft", statuses)
		# A failed order that has a Sales Order has to be found to be cancelled
		self.assertIn("failed", statuses)
		self.assertIn("pending", statuses)
		self.assertIn("cancelled", statuses)

	def test_a_status_that_ticks_nothing_is_synced(self):
		# The Sales Order of an order with that status still takes its status, lines and payment
		rows = [{"woocommerce_status": "ready-pickup"}, {"woocommerce_status": "quote-sent", ACTION_SKIP: 1}]
		with (
			patch.object(frappe, "get_all", return_value=[SERVER]),
			patch.object(frappe, "get_cached_doc", return_value=make_server(rows)),
		):
			self.assertEqual(get_statuses_to_sync(), ["ready-pickup"])


class TestWebhook(IntegrationTestCase):
	def post(self, event, body, **server):
		headers = {"x-wc-webhook-event": event, "x-wc-webhook-source": f"https://{SERVER}/"}
		wc_server = _dict({"name": SERVER, "enabled": 1, "creation_user": "Administrator", **server})
		with (
			patch.object(endpoint, "validate_request", return_value=(wc_server, None)) as validate_request,
			patch.object(frappe, "request", MagicMock(data=body)),
			patch.object(
				frappe, "get_request_header", side_effect=lambda key, default=None: headers.get(key, default)
			),
			patch.object(frappe, "set_user") as set_user,
			patch.object(endpoint, "enqueue_sales_order_sync") as enqueue,
		):
			response = endpoint.order_created()
		self.validate_request, self.set_user = validate_request, set_user
		return response, enqueue

	def test_order_events_are_synced(self):
		# A deleted order is sent as its id alone
		for event in ("created", "updated", "deleted", "restored"):
			response, enqueue = self.post(event, b'{"id": 12}')
			self.assertEqual(response.status_code, 200)
			enqueue.assert_called_once_with(woocommerce_order_name=f"{SERVER}~12")
			self.set_user.assert_called_once_with("Administrator")

	def test_other_events_are_refused(self):
		response, enqueue = self.post("action", b'{"id": 12}')
		self.assertEqual(response.status_code, 400)
		enqueue.assert_not_called()

	def test_ping_is_accepted(self):
		# WooCommerce sends it unsigned and without headers when the webhook is saved
		response, enqueue = self.post("created", b"webhook_id=3")
		self.assertEqual(response.status_code, 200)
		self.validate_request.assert_not_called()
		enqueue.assert_not_called()

	def test_a_shop_whose_sync_is_off_is_acknowledged_and_left_alone(self):
		response, enqueue = self.post("updated", b'{"id": 12}', enabled=0)
		self.assertEqual(response.status_code, 200)
		enqueue.assert_not_called()
		self.set_user.assert_not_called()


class TestWebhookSignature(IntegrationTestCase):
	def validate(self, signature: str | None, secret: str | None = "s3cret", source=f"https://{SERVER}/"):
		body = b'{"id": 12}'
		headers = {"x-wc-webhook-source": source, "x-wc-webhook-signature": signature}
		# A Password field: the server holds its mask, and the secret is read with get_password
		self.server = MagicMock(secret="*" * len(secret or ""))
		self.server.name = SERVER
		self.server.get_password.return_value = secret
		with (
			patch.object(frappe, "request", MagicMock(data=body)),
			patch.object(
				frappe,
				"get_request_header",
				side_effect=lambda key, default=None: headers.get(key) or default,
			),
			patch.object(frappe, "get_doc", return_value=self.server),
		):
			return endpoint.validate_request()

	@staticmethod
	def sign(body: bytes, secret: str) -> str:
		# As WooCommerce sends it in the X-WC-Webhook-Signature header
		return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()

	def test_a_signed_webhook_names_its_server(self):
		server, refusal = self.validate(self.sign(b'{"id": 12}', "s3cret"))
		self.assertIsNone(refusal)
		self.assertEqual(server.name, SERVER)
		self.server.get_password.assert_called_once_with("secret", raise_exception=False)

	def test_the_mask_the_server_holds_is_not_the_key(self):
		_server, refusal = self.validate(self.sign(b'{"id": 12}', "******"))
		self.assertEqual(refusal.status_code, 401)

	def test_a_server_without_a_secret_takes_no_webhook(self):
		# Anyone can sign with an empty key
		server, refusal = self.validate(self.sign(b'{"id": 12}', ""), secret=None)
		self.assertIsNone(server)
		self.assertEqual(refusal.status_code, 401)

	def test_a_wrong_or_missing_signature_is_refused(self):
		for signature in (self.sign(b'{"id": 12}', "guessed"), None):
			server, refusal = self.validate(signature)
			self.assertIsNone(server)
			self.assertEqual(refusal.status_code, 401)

	def test_a_webhook_without_its_source_is_refused(self):
		_server, refusal = self.validate(self.sign(b'{"id": 12}', "s3cret"), source="")
		self.assertEqual(refusal.status_code, 400)


class TestOrderDocuments(IntegrationTestCase):
	"""The invoices and payments a person made for a Sales Order, which the sync looks at but never makes"""

	def test_invoices_are_found_through_their_items(self):
		with patch.object(frappe, "get_all", return_value=[]) as get_all:
			get_order_invoices("SAL-ORD-1")

		self.assertEqual(get_all.call_args.args[0], "Sales Invoice")
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			[["Sales Invoice Item", "sales_order", "=", "SAL-ORD-1"], ["docstatus", "<", 2]],
		)

	def test_payments_of_the_order_and_of_its_invoices_are_found(self):
		with patch.object(
			frappe, "get_all", side_effect=[["ACC-PAY-2"], ["ACC-PAY-1", "ACC-PAY-2"]]
		) as get_all:
			payment_entries = get_order_payment_entries("SAL-ORD-1", ["ACC-SINV-1"])

		self.assertEqual(payment_entries, ["ACC-PAY-1", "ACC-PAY-2"])
		self.assertEqual(
			[
				(call.kwargs["filters"]["reference_doctype"], call.kwargs["filters"]["reference_name"])
				for call in get_all.call_args_list
			],
			[("Sales Order", ["in", ["SAL-ORD-1"]]), ("Sales Invoice", ["in", ["ACC-SINV-1"]])],
		)
		self.assertEqual(get_all.call_args.kwargs["filters"]["docstatus"], ["<", 2])

	def test_payment_method_title(self):
		self.assertEqual(
			get_payment_method(_dict(payment_method_title="Card", payment_method="stripe")), "Card"
		)
		# A title that doesn't fit the field
		self.assertEqual(
			get_payment_method(_dict(payment_method_title="C" * 140, payment_method="stripe")), "stripe"
		)
		self.assertIsNone(get_payment_method(_dict(payment_method_title="", payment_method="")))


def line(line_id, product_id, qty, total, total_tax=0, variation_id=0, name="Mug"):
	return {
		"id": line_id,
		"name": name,
		"product_id": product_id,
		"variation_id": variation_id,
		"quantity": qty,
		"subtotal": str(total),
		"total": str(total),
		"total_tax": str(total_tax),
	}


class TestLineValues(IntegrationTestCase):
	def test_product_id_is_the_variation_if_there_is_one(self):
		self.assertEqual(get_product_id(line(1, 10, 1, 5, variation_id=11)), "11")
		self.assertEqual(get_product_id(line(1, 10, 1, 5)), "10")
		# Deleted in WooCommerce
		self.assertEqual(get_product_id(line(1, 0, 1, 5)), "0")

	def test_rate_is_after_coupons(self):
		mug = {**line(1, 10, 2, 18, total_tax=3.78), "subtotal": "20.00"}
		self.assertEqual(get_line_rate(mug, tax_inclusive=False), 9)
		self.assertAlmostEqual(get_line_rate(mug, tax_inclusive=True), 10.89)
		self.assertEqual(get_line_rate(line(1, 10, 0, 18), tax_inclusive=False), 0)
		# A shop that charges no tax itself: the price the customer paid, VAT in it
		self.assertEqual(get_line_rate(line(1, 10, 2, 520), tax_inclusive=True), 260)

	def test_rates_match_to_the_cent(self):
		self.assertTrue(rates_match(3.33, 10 / 3))
		self.assertFalse(rates_match(3.32, 10 / 3))

	def test_totals_allow_for_rounded_rates(self):
		lines = [line(1, 10, 3, 10)]
		# 3 x 3.33 is 9.99
		self.assertFalse(totals_differ(9.99, 10, lines))
		self.assertTrue(totals_differ(9.90, 10, lines))


class TestCharges(IntegrationTestCase):
	def order(self, **values):
		return _dict(
			{
				"shipping_total": 5,
				"shipping_tax": 1.05,
				"fee_lines": json.dumps([{"name": "Gift wrap", "total": "1.00", "total_tax": "0.21"}]),
				"tax_lines": json.dumps(
					[
						{
							"label": "VAT",
							"rate_code": "PL-VAT-1",
							"tax_total": "3.99",
							"shipping_tax_total": "1.05",
						},
						{
							"label": "Zero",
							"rate_code": "PL-ZERO-2",
							"tax_total": "0",
							"shipping_tax_total": "0",
						},
					]
				),
				**values,
			}
		)

	def server(self, **values):
		return _dict({"freight_and_forwarding_account": "Freight", **values})

	VAT = [("VAT", 21.0)]  # noqa: RUF012

	def test_net_charges_get_the_vat_woocommerce_charged(self):
		charges = build_charges(self.order(), self.server(fee_account="Fees"), True, self.VAT, False)

		self.assertEqual(
			[(c["account_head"], c["tax_amount"], c["description"]) for c in charges],
			[("Freight", 5, "Shipping"), ("Fees", 1, "Gift wrap"), ("VAT", 1.26, "VAT on shipping and fees")],
		)
		self.assertTrue(all(c["charge_type"] == "Actual" for c in charges))
		# Kept out of the VAT rate of the lines, which ERPNext reads from the last row on each account
		self.assertTrue(all(c["set_by_item_tax_template"] for c in charges))

	def test_order_adds_up(self):
		# 2 x 9 after a coupon, 5 shipping, a 1 fee and 5.04 tax, as WooCommerce totals it
		lines = [line(1, 10, 2, 18, total_tax=3.78)]
		item_total = sum(get_line_rate(row, False) * row["quantity"] for row in lines)
		# The template's 21% on the lines
		item_tax = item_total * 0.21
		charges = build_charges(self.order(), self.server(), True, self.VAT, False)
		self.assertAlmostEqual(item_total + item_tax + sum(c["tax_amount"] for c in charges), 29.04)

	def test_fees_fall_back_to_the_freight_account(self):
		charges = build_charges(self.order(), self.server(), True, self.VAT, False)
		self.assertEqual(charges[1]["account_head"], "Freight")

	def test_shipping_rule_charges_the_shipping(self):
		charges = build_charges(self.order(), self.server(fee_account="Fees"), False, self.VAT, False)

		# ERPNext's rule charges the shipping without VAT, so the VAT WooCommerce charged on it is kept
		self.assertEqual(
			[(c["account_head"], c["tax_amount"], c["description"]) for c in charges],
			[("Fees", 1, "Gift wrap"), ("VAT", 1.26, "VAT on shipping and fees")],
		)

	def test_shipping_rule_keeps_the_vat_of_inclusive_shipping(self):
		# A shop that leaves taxes to ERPNext, with prices that include the VAT
		order = self.order(shipping_total=20, shipping_tax=0, fee_lines="[]", tax_lines="[]")
		charges = build_charges(order, self.server(), False, [("VAT", 23.0)], True)
		self.assertEqual([(c["account_head"], c["tax_amount"]) for c in charges], [("VAT", 3.74)])

	def test_inclusive_charges_have_the_vat_taken_out(self):
		# A shop that leaves taxes to ERPNext, with prices that include the VAT
		order = self.order(
			shipping_total=20,
			shipping_tax=0,
			fee_lines=json.dumps([{"name": "Gift wrap", "total": "1.23", "total_tax": "0"}]),
			tax_lines="[]",
		)
		charges = build_charges(order, self.server(), True, [("VAT", 23.0)], True)

		self.assertEqual(
			[(c["account_head"], c["tax_amount"]) for c in charges],
			[("Freight", 16.26), ("Freight", 1.0), ("VAT", 3.97)],
		)
		self.assertAlmostEqual(sum(c["tax_amount"] for c in charges), 21.23)

	def test_inclusive_charges_woocommerce_taxed_have_its_vat_taken_out(self):
		order = self.order(shipping_total=16.26, shipping_tax=3.74, fee_lines="[]")
		charges = build_charges(order, self.server(), True, [("VAT", 23.0)], True)
		self.assertEqual(
			[(c["account_head"], c["tax_amount"]) for c in charges], [("Freight", 16.26), ("VAT", 3.74)]
		)

	def test_inclusive_charges_the_shop_left_untaxed_are_booked_whole(self):
		# WooCommerce taxed the order (it has tax lines), but not its shipping or the fee
		order = self.order(
			shipping_total=20,
			shipping_tax=0,
			fee_lines=json.dumps([{"name": "Gift wrap", "total": "1.23", "total_tax": "0"}]),
		)
		charges = build_charges(order, self.server(), True, [("VAT", 23.0)], True)
		self.assertEqual(
			[(c["account_head"], c["tax_amount"]) for c in charges], [("Freight", 20), ("Freight", 1.23)]
		)

	def test_shipping_rule_books_no_vat_on_shipping_the_shop_left_untaxed(self):
		order = self.order(shipping_total=20, shipping_tax=0, fee_lines="[]")
		self.assertEqual(build_charges(order, self.server(), False, [("VAT", 23.0)], True), [])

	def test_inclusive_charges_without_vat_rows_stay_whole(self):
		order = self.order(shipping_total=20, shipping_tax=0, fee_lines="[]")
		charges = build_charges(order, self.server(), True, [], True)
		self.assertEqual([(c["account_head"], c["tax_amount"]) for c in charges], [("Freight", 20)])


class TestLines(IntegrationTestCase):
	def make(self, lines, items=None, invoices=(), **sales_order_values):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.get_invoices = MagicMock(return_value=list(invoices))
		sync.woocommerce_order = _dict(
			name=f"{SERVER}~12",
			woocommerce_server=SERVER,
			line_items=json.dumps(lines),
			total=sum(float(row["total"]) for row in lines),
		)
		sync.sales_order = MagicMock(
			**{
				"docstatus": 1,
				"per_delivered": 0,
				"per_billed": 0,
				"delivery_date": "2026-11-06",
				"items": items or [],
				"woocommerce_sync_issue": None,
				**sales_order_values,
			}
		)
		sync.sales_order.name = "SAL-ORD-1"
		sync.sales_order.grand_total = sync.woocommerce_order.total
		return sync

	def build(self, sync, item_codes, **server):
		with (
			patch.object(order_lines, "find_item_code", side_effect=lambda server, pid: item_codes.get(pid)),
			patch.object(order_lines, "get_placeholder_item", return_value=order_lines.PLACEHOLDER_ITEM),
			patch.object(frappe, "get_cached_value", return_value="Mug (Item)"),
		):
			return sync.build_item_rows(_dict(name=SERVER, warehouse="Stores", **server), "2026-11-06")

	def test_every_line_is_a_row(self):
		sync = self.make([line(1, 10, 2, 18), line(2, 0, 1, 5, name="Gone")])
		rows, issues = self.build(sync, {"10": "MUG"})

		self.assertEqual([row["item_code"] for row in rows], ["MUG", order_lines.PLACEHOLDER_ITEM])
		self.assertEqual(rows[0]["rate"], 9)
		self.assertEqual(rows[0]["woocommerce_line_item_id"], "1")
		self.assertEqual(rows[0]["woocommerce_id"], "10")
		self.assertEqual(rows[0]["warehouse"], "Stores")
		self.assertEqual(rows[0]["delivery_date"], "2026-11-06")
		# The placeholder keeps the name the line has in WooCommerce
		self.assertEqual(rows[1]["item_name"], "Gone")
		self.assertEqual(len(issues), 1)

	def test_a_line_without_an_item_reads_plain_and_keeps_its_html_description(self):
		sync = self.make([line(1, 0, 1, 5, name="Sleeves &amp; Toploader")])

		rows, issues = self.build(sync, {})

		self.assertEqual(rows[0]["item_name"], "Sleeves & Toploader")
		# An HTML field, where the escaped name reads right
		self.assertEqual(rows[0]["description"], "Sleeves &amp; Toploader")
		self.assertIn("Sleeves & Toploader", issues[0])

	def map_line(self, *rows, **line_values):
		sync = self.make([{**line(1, 10, 1, 5), **line_values}])
		frappe.flags.pop(LOGGED_PROBLEMS_FLAG, None)
		with patch.object(frappe, "log_error") as log_error:
			built_rows, _issues = self.build(
				sync,
				{"10": "MUG"},
				order_item_field_map=[
					_dict(erpnext_field_name=field, woocommerce_field_name=path) for field, path in rows
				],
			)
		return built_rows[0], log_error

	def test_the_order_line_fields_map_fills_the_row(self):
		row, _log_error = self.map_line(
			("description | Description", "$.meta_data[?key=='Engraving'].value"),
			meta_data=[{"key": "Engraving", "value": "For Ala"}],
		)

		self.assertEqual(row["description"], "For Ala")
		self.assertEqual(row["item_code"], "MUG")

	def test_a_line_value_the_field_can_not_take_is_left_out(self):
		row, log_error = self.map_line(
			("item_name | Item Name", "$.meta_data[?key=='Note'].value"),
			meta_data=[{"key": "Note", "value": "M" * 200}],
		)

		# The row is made all the same, with the Item's name
		self.assertEqual(row["item_name"], "Mug (Item)")
		log_error.assert_called_once()

	def test_inclusive_rows_are_what_the_customer_paid(self):
		sync = self.make([line(1, 10, 2, 400, total_tax=92)])
		with patch.object(order_lines, "prices_include_tax", return_value=True):
			rows, _issues = self.build(sync, {"10": "MOWER"})
		self.assertEqual(rows[0]["rate"], 246)

	def test_a_free_line_is_a_full_discount(self):
		sync = self.make([line(1, 10, 1, 0, name="Gift"), line(2, 11, 2, 18)])
		rows, _issues = self.build(sync, {"10": "GIFT", "11": "MUG"})

		self.assertEqual((rows[0]["rate"], rows[0]["discount_percentage"]), (0, 100))
		self.assertNotIn("discount_percentage", rows[1])

	def test_erpnext_keeps_a_free_line_at_nothing(self):
		# Without the discount ERPNext puts the price list rate in place of a rate of 0
		totals = calculate_taxes_and_totals.__new__(calculate_taxes_and_totals)
		totals.doc = _dict(ignore_pricing_rule=1)
		for discount_percentage, rate in ((0, 20), (100, 0)):
			row = frappe.new_doc("Sales Order Item")
			row.update({"price_list_rate": 20, "rate": 0, "discount_percentage": discount_percentage})
			# As calculate_item_values does before it
			row.round_floats_in(row)
			totals.calculate_item_rate(row)
			self.assertEqual(row.rate, rate)

	def test_product_without_an_item_is_flagged_too(self):
		sync = self.make([line(1, 10, 1, 5)])
		rows, issues = self.build(sync, {})
		self.assertEqual(rows[0]["item_code"], order_lines.PLACEHOLDER_ITEM)
		self.assertEqual(len(issues), 1)

	def row(self, line_id, item_code, qty, rate, name):
		return _dict(
			name=name,
			woocommerce_line_item_id=str(line_id),
			item_code=item_code,
			qty=qty,
			rate=rate,
			uom="Nos",
			conversion_factor=1,
			description=f"{item_code} as described on the Sales Order",
		)

	def wanted(self, line_id, item_code, qty, rate):
		return {
			"woocommerce_line_item_id": str(line_id),
			"woocommerce_id": "10",
			"item_code": item_code,
			"item_name": item_code,
			"description": item_code,
			"qty": qty,
			"rate": rate,
			"warehouse": "Stores",
			"delivery_date": "2026-11-06",
		}

	def update(self, sync, wanted, server=None):
		with (
			patch.object(sync, "get_wc_server", return_value=server or _dict(name=SERVER)),
			patch.object(sync, "create_missing_items"),
			patch.object(sync, "build_item_rows", return_value=(wanted, [])),
			patch.object(order_lines, "update_child_qty_rate") as update_child_qty_rate,
			patch.object(frappe.db, "set_value") as set_value,
		):
			sync.update_sales_order_items()
		trans_items = (
			json.loads(update_child_qty_rate.call_args.args[1]) if update_child_qty_rate.called else None
		)
		return trans_items, set_value

	def test_unchanged_lines_are_left(self):
		sync = self.make([line(1, 10, 2, 18)], items=[self.row(1, "MUG", 2, 9, "SOI-1")])
		trans_items, _set_value = self.update(sync, [self.wanted(1, "MUG", 2, 9)])
		self.assertIsNone(trans_items)

	def test_changed_lines_update_the_sales_order(self):
		sync = self.make(
			[line(1, 10, 3, 27), line(3, 12, 1, 4)],
			items=[self.row(1, "MUG", 2, 9, "SOI-1"), self.row(2, "CUP", 1, 5, "SOI-2")],
		)
		trans_items, _set_value = self.update(
			sync, [self.wanted(1, "MUG", 3, 9), self.wanted(3, "PLATE", 1, 4)]
		)

		# Line 1 keeps its row with the new qty, line 2 is gone and line 3 is new
		self.assertEqual(trans_items[0]["docname"], "SOI-1")
		self.assertEqual(trans_items[0]["qty"], 3)
		self.assertNotIn("docname", trans_items[1])
		self.assertEqual(trans_items[1]["item_code"], "PLATE")
		self.assertEqual(len(trans_items), 2)

	def test_updated_lines_keep_their_descriptions(self):
		# Update Items takes a missing description for a change, and empties the row's
		sync = self.make([line(1, 10, 3, 27), line(3, 12, 1, 4)], items=[self.row(1, "MUG", 2, 9, "SOI-1")])
		trans_items, _set_value = self.update(
			sync, [self.wanted(1, "MUG", 3, 9), self.wanted(3, "PLATE", 1, 4)]
		)

		self.assertEqual(
			[row["description"] for row in trans_items], ["MUG as described on the Sales Order", "PLATE"]
		)

	def test_line_with_another_product_is_a_new_row(self):
		sync = self.make([line(1, 11, 2, 18)], items=[self.row(1, "MUG", 2, 9, "SOI-1")])
		trans_items, _set_value = self.update(sync, [self.wanted(1, "CUP", 2, 9)])
		self.assertNotIn("docname", trans_items[0])

	def test_new_rows_are_linked_to_their_line(self):
		sync = self.make([line(1, 10, 2, 18), line(3, 12, 1, 4)], items=[self.row(1, "MUG", 2, 9, "SOI-1")])

		def reload():
			# What Update Items leaves: a new row without the custom fields
			sync.sales_order.items = [
				self.row(1, "MUG", 2, 9, "SOI-1"),
				_dict(name="SOI-3", item_code="PLATE", woocommerce_line_item_id=None),
			]

		sync.sales_order.reload.side_effect = reload
		_trans_items, set_value = self.update(
			sync, [self.wanted(1, "MUG", 2, 9), self.wanted(3, "PLATE", 1, 4)]
		)

		set_value.assert_called_once()
		self.assertEqual(set_value.call_args.args[:2], ("Sales Order Item", "SOI-3"))
		self.assertEqual(set_value.call_args.args[2]["woocommerce_line_item_id"], "3")

	def test_rows_update_items_adds_get_the_mapped_values_sanitised(self):
		sync = self.make([line(1, 10, 2, 18), line(3, 12, 1, 4)], items=[self.row(1, "MUG", 2, 9, "SOI-1")])

		def reload():
			sync.sales_order.items = [
				self.row(1, "MUG", 2, 9, "SOI-1"),
				_dict(name="SOI-3", item_code="PLATE", woocommerce_line_item_id=None),
			]

		sync.sales_order.reload.side_effect = reload
		new_line = {**self.wanted(3, "PLATE", 1, 4), "additional_notes": "For Ala<script>alert(1)</script>"}
		row = _dict(
			erpnext_field_name="additional_notes | Notes", woocommerce_field_name="$.meta_data[0].value"
		)
		_trans_items, set_value = self.update(
			sync,
			[self.wanted(1, "MUG", 2, 9), new_line],
			server=_dict(name=SERVER, order_item_field_map=[row]),
		)

		# One write, with the line's ids and its mapped value, as a save would store it
		set_value.assert_called_once()
		values = set_value.call_args.args[2]
		self.assertEqual(values["woocommerce_line_item_id"], "3")
		self.assertIn("For Ala", values["additional_notes"])
		self.assertNotIn("<script>", values["additional_notes"])

	def test_lines_of_a_delivered_or_invoiced_order_are_flagged(self):
		# A draft invoice a person started counts too, though per_billed doesn't, as it has the old lines
		for values in (
			{"per_delivered": 50},
			{"per_billed": 100},
			{"docstatus": 0},
			{"invoices": [invoice_row(docstatus=0)]},
		):
			sync = self.make([line(1, 10, 3, 27)], items=[self.row(1, "MUG", 2, 9, "SOI-1")], **values)
			trans_items, _set_value = self.update(sync, [self.wanted(1, "MUG", 3, 9)])

			self.assertIsNone(trans_items)
			self.assertEqual(sync.sales_order.db_set.call_args.args[0], "woocommerce_sync_issue")

	def test_rows_without_a_line_are_flagged(self):
		# Made before the rows were linked to their line
		sync = self.make([line(1, 10, 3, 27)], items=[self.row(None, "MUG", 2, 9, "SOI-1")])
		sync.sales_order.items[0].woocommerce_line_item_id = None
		trans_items, _set_value = self.update(sync, [self.wanted(1, "MUG", 3, 9)])
		self.assertIsNone(trans_items)

	def test_totals_that_differ_are_flagged(self):
		sync = self.make([line(1, 10, 2, 18)])
		sync.sales_order.grand_total = 20
		self.assertIn("18", sync.check_totals(sync.sales_order))
		sync.sales_order.grand_total = 18
		self.assertIsNone(sync.check_totals(sync.sales_order))


class TestNewSalesOrderLines(IntegrationTestCase):
	def test_lines_and_charges_of_a_new_sales_order(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(
			name=f"{SERVER}~12",
			woocommerce_server=SERVER,
			line_items=json.dumps([line(1, 10, 2, 18)]),
			shipping_total=5,
			fee_lines="[]",
			tax_lines="[]",
		)
		sales_order = frappe.new_doc("Sales Order")
		server = make_server(warehouse="Stores", freight_and_forwarding_account="Freight")
		rows = [{"item_code": "MUG", "qty": 2, "rate": 9, "woocommerce_line_item_id": "1"}]
		with (
			patch.object(sync, "get_wc_server", return_value=server),
			patch.object(sync, "build_item_rows", return_value=(rows, ["one issue"])),
		):
			sync.set_items_in_sales_order(sales_order)

		self.assertEqual(sales_order.ignore_pricing_rule, 1)
		self.assertEqual([row.item_code for row in sales_order.items], ["MUG"])
		self.assertEqual([tax.description for tax in sales_order.taxes], ["Shipping"])
		self.assertEqual(sync.line_issues, ["one issue"])

	def test_inclusive_shipping_has_its_vat_taken_out(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(
			name=f"{SERVER}~12",
			woocommerce_server=SERVER,
			line_items=json.dumps([line(1, 10, 1, 123)]),
			shipping_total=20,
			shipping_tax=0,
			fee_lines="[]",
		)
		sales_order = frappe.new_doc("Sales Order")
		server = make_server(
			warehouse="Stores",
			freight_and_forwarding_account="Freight",
			default_item_tax_template="VAT 23%",
		)
		rows = [{"item_code": "MUG", "qty": 1, "rate": 100, "woocommerce_line_item_id": "1"}]
		with (
			patch.object(sync, "get_wc_server", return_value=server),
			patch.object(sync, "build_item_rows", return_value=(rows, [])),
			patch.object(order_lines, "get_vat_rates", return_value=[("VAT", 23.0)]) as get_vat_rates,
			patch.object(order_lines, "prices_include_tax", return_value=True),
		):
			sync.set_items_in_sales_order(sales_order)

		get_vat_rates.assert_called_once_with(server, "VAT 23%")
		self.assertEqual(
			[(tax.account_head, tax.tax_amount) for tax in sales_order.taxes],
			[("Freight", 16.26), ("VAT", 3.74)],
		)

	def test_an_order_without_lines_or_warehouse_is_an_error(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(name=f"{SERVER}~12", woocommerce_server=SERVER, line_items="[]")
		for server in (make_server(warehouse="Stores"), make_server(warehouse=None)):
			with (
				patch.object(sync, "get_wc_server", return_value=server),
				self.assertRaises(frappe.ValidationError),
			):
				sync.set_items_in_sales_order(frappe.new_doc("Sales Order"))


# The tax ID a plugin keeps in the order's meta data, mapped into the Customer
TAX_ID_ROW = {
	"erpnext_field_name": "tax_id | Tax ID",
	"woocommerce_field_name": "$.meta_data[?key=='_billing_nip'].value",
}


def nip(tax_id: str) -> str:
	"""
	The meta data of an order with a tax ID, as a plugin's checkout field keeps it
	"""
	return json.dumps([{"id": 1, "key": "_billing_nip", "value": tax_id}])


class TestCustomer(IntegrationTestCase):
	"""
	The Customer, Address and Contact of a new order, made for real in the test's transaction
	"""

	def setUp(self):
		super().setUp()
		# Frappe rolls back once a class is done, and each test finds Customers by what the others left
		frappe.db.savepoint("test_customer")
		self.addCleanup(frappe.db.rollback, save_point="test_customer")
		# What the Customers link to
		make_server().db_insert()

	def make_customer(self, name, customer_name=None, identifier=None, **values) -> str:
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"name": f"_Test WC {name}",
				"customer_name": customer_name or name,
				"customer_type": "Individual",
				"woocommerce_server": SERVER,
				"woocommerce_identifier": identifier,
				"creation": "2026-01-01 10:00:00",
				**values,
			}
		)
		customer.db_insert()
		return customer.name

	def make_contact(self, customer, first_name, last_name, email) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": first_name,
					"last_name": last_name,
					"email_ids": [{"email_id": email, "is_primary": 1}],
					"links": [{"link_doctype": "Customer", "link_name": customer}],
				}
			)
			.insert()
			.name
		)

	def make_address(self, customer, address_line1, **values) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Address",
					"address_title": "Ann Smith",
					"address_type": "Billing",
					"address_line1": address_line1,
					"city": "Kraków",
					"pincode": "30-001",
					"country": "Poland",
					"phone": "555-0100",
					"links": [{"link_doctype": "Customer", "link_name": customer}],
					**values,
				}
			)
			.insert()
			.name
		)

	def run_step(self, customer_id=0, account=None, field_map=(), **values):
		"""
		The customer step of an order Ann Smith placed as a guest, unless `customer_id` names an account, which
		`account` is (the shop no longer has it when None). `values` change the order's fields.
		"""
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		order = _dict(
			{
				"name": f"{SERVER}~12",
				"woocommerce_server": SERVER,
				"woocommerce_id": "12",
				"customer_id": customer_id,
				"billing_first_name": "Ann",
				"billing_last_name": "Smith",
				"billing_company": "",
				"billing_email": "ann@example.com",
				"billing_phone": "555-0100",
				"billing_address_1": "Order Street 1",
				"billing_city": "Kraków",
				"billing_postcode": "30-001",
				"billing_country": "PL",
				"shipping_first_name": "Ann",
				"shipping_last_name": "Smith",
				"shipping_address_1": "Order Street 1",
				"shipping_city": "Kraków",
				"shipping_postcode": "30-001",
				"shipping_country": "PL",
				"meta_data": "[]",
				**values,
			}
		)
		order.current_wc_api = MagicMock()
		order.current_wc_api.get.return_value = MagicMock(
			status_code=200 if account else 404, json=lambda: account
		)
		sync.woocommerce_order = order
		server = make_server(customer_field_map=list(field_map))
		get_cached_doc = frappe.get_cached_doc
		with patch.object(
			frappe,
			"get_cached_doc",
			side_effect=lambda *args, **kwargs: (
				server if args and args[0] == "WooCommerce Server" else get_cached_doc(*args, **kwargs)
			),
		):
			name = sync.create_or_link_customer_and_address()
		return sync, name

	def test_a_guest_gets_a_customer_of_the_order(self):
		sync, name = self.run_step()

		sync.woocommerce_order.current_wc_api.get.assert_not_called()
		customer = frappe.get_doc("Customer", name)
		self.assertEqual(
			(customer.woocommerce_identifier, customer.customer_name, customer.customer_type),
			("Guest-12", "Ann Smith", "Individual"),
		)
		# One Address for billing and shipping, and a Contact: its first, so its primary ones
		self.assertEqual(sync.shipping_address, sync.billing_address)
		address = frappe.get_doc("Address", sync.billing_address)
		self.assertEqual(
			(address.address_line1, address.country, address.phone, address.address_title),
			("Order Street 1", "Poland", "555-0100", "Ann Smith"),
		)
		self.assertEqual((address.is_primary_address, address.is_shipping_address), (1, 1))
		contact = frappe.get_doc("Contact", sync.contact)
		self.assertEqual(
			(contact.email_id, contact.phone, contact.is_primary_contact), ("ann@example.com", "555-0100", 1)
		)
		self.assertEqual(
			(customer.customer_primary_address, customer.customer_primary_contact),
			(sync.billing_address, sync.contact),
		)
		self.assertEqual(sync.customer_issues, [])

	def test_a_guest_goes_to_the_customer_of_its_email_which_keeps_what_it_had(self):
		ann = self.make_customer("Ann", "Ann Smith", "Guest-5")
		contact = self.make_contact(ann, "Ann", "Smith", "ann@example.com")
		address = self.make_address(ann, "Old Street 9")
		modified = frappe.db.get_value("Address", address, "modified")

		sync, name = self.run_step()

		self.assertEqual(name, ann)
		self.assertEqual(sync.customer_issues, [])
		self.assertEqual(sync.contact, contact)
		self.assertEqual(frappe.db.get_value("Customer", ann, "woocommerce_identifier"), "Guest-5")
		# The order's address is added, and the Address of the earlier order is left as it was
		self.assertNotEqual(sync.billing_address, address)
		self.assertEqual(
			frappe.db.get_value("Address", address, ["address_line1", "modified"]), ("Old Street 9", modified)
		)

	def test_an_order_of_another_name_on_the_customer_of_its_email_is_flagged(self):
		bob = self.make_customer("Bob", "Bob Smith", "Guest-5")
		bobs_contact = self.make_contact(bob, "Bob", "Smith", "ann@example.com")

		sync, name = self.run_step()

		self.assertEqual(name, bob)
		self.assertEqual(frappe.db.get_value("Customer", bob, "customer_name"), "Bob Smith")
		self.assertEqual(len(sync.customer_issues), 1)
		self.assertIn("Ann Smith", sync.customer_issues[0])
		# The email is Bob's too, but the order's Contact is Ann
		self.assertNotEqual(sync.contact, bobs_contact)
		self.assertEqual(frappe.db.get_value("Contact", sync.contact, "first_name"), "Ann")

	def test_an_order_with_a_tax_id_is_not_put_on_another_buyers_customer_of_its_email(self):
		bob = self.make_customer("Bob", "Bob Smith", "Guest-5")
		self.make_contact(bob, "Bob", "Smith", "ann@example.com")

		sync, name = self.run_step(field_map=[TAX_ID_ROW], meta_data=nip("PL6791003801"))

		self.assertNotEqual(name, bob)
		self.assertEqual(frappe.db.get_value("Customer", name, "tax_id"), "PL6791003801")
		self.assertIsNone(frappe.db.get_value("Customer", bob, "tax_id"))
		self.assertEqual(sync.customer_issues, [])

	def test_customers_of_other_shops_and_disabled_ones_are_not_found_by_email(self):
		others = [
			self.make_customer("Ann", "Ann Smith", "Guest-5", woocommerce_server="two.example.com"),
			self.make_customer("Ann Disabled", "Ann Smith", "Guest-6", disabled=1),
		]
		for customer in others:
			self.make_contact(customer, "Ann", "Smith", "ann@example.com")

		_sync, name = self.run_step()

		self.assertNotIn(name, others)
		self.assertEqual(frappe.db.get_value("Customer", name, "woocommerce_identifier"), "Guest-12")

	def test_a_registered_customer_is_their_account_which_the_order_does_not_rename(self):
		ann = self.make_customer("Ann", "Ann Old", "ann")
		account = {"email": "ann@account.example.com", "username": "ann", "role": "customer"}

		sync, name = self.run_step(customer_id=7, account=account)

		request = sync.woocommerce_order.current_wc_api.get.call_args
		self.assertEqual(request.args[0], "customers/7")
		self.assertIn("role", request.kwargs["params"]["_fields"].split(","))
		self.assertEqual(name, ann)
		self.assertEqual(frappe.db.get_value("Customer", ann, "customer_name"), "Ann Old")
		self.assertIn("Ann Smith", sync.customer_issues[0])
		# The order's Contact has the email the order gives
		self.assertEqual(frappe.db.get_value("Contact", sync.contact, "email_id"), "ann@example.com")

	def test_a_registered_customer_of_the_orders_name_is_not_flagged(self):
		ann = self.make_customer("Ann", "Ann Smith", "ann")

		sync, name = self.run_step(customer_id=7, account={"username": "ann", "role": "customer"})

		self.assertEqual(name, ann)
		self.assertEqual(sync.customer_issues, [])

	def test_a_new_registered_customer_is_known_by_their_username(self):
		account = {"username": "ann", "avatar_url": "https://a.example.com/ann", "role": "customer"}

		_sync, name = self.run_step(customer_id=7, account=account)

		self.assertEqual(
			frappe.db.get_value("Customer", name, ["woocommerce_identifier", "image"]),
			("ann", "https://a.example.com/ann"),
		)

	def test_a_customer_whose_account_is_gone_is_known_by_email(self):
		_sync, name = self.run_step(customer_id=7)

		self.assertEqual(frappe.db.get_value("Customer", name, "woocommerce_identifier"), "ann@example.com")

	def test_an_account_whose_customer_was_merged_goes_to_the_customer_it_was_merged_into(self):
		kept = self.make_customer("Kept", "Ann Smith", "Guest-5")
		frappe.get_doc(
			{
				"doctype": "WooCommerce Customer Decision",
				"decision": "Merged",
				"customer": kept,
				"merged_customer": "_Test WC Ann",
				"woocommerce_server": SERVER,
				"woocommerce_identifier": "ann",
			}
		).insert(ignore_permissions=True)

		sync, name = self.run_step(customer_id=7, account={"username": "ann", "role": "customer"})

		self.assertEqual(name, kept)
		self.assertEqual(sync.customer_issues, [])

	def test_an_order_the_shops_staff_placed_is_the_buyers(self):
		admin = self.make_customer("Admin", "Shop Admin", "admin")
		account = {"username": "admin", "avatar_url": "https://a.example.com/admin", "role": "administrator"}

		sync, name = self.run_step(customer_id=1, account=account)

		self.assertNotEqual(name, admin)
		self.assertEqual(
			frappe.db.get_value("Customer", name, ["woocommerce_identifier", "customer_name", "image"]),
			("Guest-12", "Ann Smith", None),
		)
		self.assertEqual(frappe.db.get_value("Customer", admin, "customer_name"), "Shop Admin")
		self.assertEqual(sync.customer_issues, [])

	def test_an_order_the_staff_placed_for_a_customer_finds_them_by_email(self):
		ann = self.make_customer("Ann", "Ann Smith", "ann")
		self.make_contact(ann, "Ann", "Smith", "ann@example.com")

		_sync, name = self.run_step(customer_id=1, account={"username": "manager", "role": "shop_manager"})

		self.assertEqual(name, ann)

	def test_an_order_with_a_tax_id_goes_to_the_customer_with_it(self):
		firm = self.make_customer(
			"Firm", "Firma X sp. z o.o.", "firma", customer_type="Company", tax_id="6791003801"
		)

		sync, name = self.run_step(
			field_map=[TAX_ID_ROW], billing_company="FIRMA X", meta_data=nip("PL 679-100-38-01")
		)

		self.assertEqual(name, firm)
		# The tax ID says which company it is, however the order spells its name
		self.assertEqual(sync.customer_issues, [])
		self.assertEqual(frappe.db.get_value("Customer", firm, "tax_id"), "6791003801")

	def test_the_customer_of_an_account_that_is_the_company_takes_its_tax_id(self):
		firm = self.make_customer("Firm", "Firma X", "ann", customer_type="Company")

		_sync, name = self.run_step(
			customer_id=7,
			account={"username": "ann", "role": "customer"},
			field_map=[TAX_ID_ROW],
			billing_company="Firma X",
			meta_data=nip("PL6791003801"),
		)

		self.assertEqual(name, firm)
		self.assertEqual(frappe.db.get_value("Customer", firm, "tax_id"), "PL6791003801")

	def test_a_person_buying_for_their_company_gets_a_customer_of_the_company(self):
		ann = self.make_customer("Ann", "Ann Smith", "ann")

		sync, name = self.run_step(
			customer_id=7,
			account={"username": "ann", "role": "customer"},
			field_map=[TAX_ID_ROW],
			billing_company="Firma X",
			meta_data=nip("PL6791003801"),
		)

		self.assertNotEqual(name, ann)
		self.assertEqual(
			frappe.db.get_value(
				"Customer", name, ["woocommerce_identifier", "customer_name", "customer_type", "tax_id"]
			),
			("TaxID-6791003801", "Firma X", "Company", "PL6791003801"),
		)
		self.assertIsNone(frappe.db.get_value("Customer", ann, "tax_id"))
		self.assertEqual(sync.customer_issues, [])

	def test_a_guests_company_order_gets_a_customer_with_its_tax_id(self):
		_sync, name = self.run_step(
			field_map=[TAX_ID_ROW], billing_company="Firma X", meta_data=nip("PL6791003801")
		)

		self.assertEqual(
			frappe.db.get_value("Customer", name, ["woocommerce_identifier", "customer_type", "tax_id"]),
			("Guest-12", "Company", "PL6791003801"),
		)

	def test_an_address_the_customer_has_is_used_again_as_it_is(self):
		ann = self.make_customer("Ann", "Ann Smith", "Guest-5")
		self.make_contact(ann, "Ann", "Smith", "ann@example.com")
		# The order's address, written with other capitals and spaces
		address = self.make_address(ann, " order  street 1", city="KRAKÓW")
		fields = ["address_line1", "city", "phone", "address_title"]
		written = frappe.db.get_value("Address", address, fields)

		sync, _name = self.run_step()

		self.assertEqual((sync.billing_address, sync.shipping_address), (address, address))
		# Only made the Customer's primary one, which ERPNext marks it as when it saves the Customer
		self.assertEqual(frappe.db.get_value("Address", address, fields), written)
		self.assertEqual(len(get_addresses_linking_to("Customer", ann)), 1)

	def test_a_shipping_address_of_its_own_gets_an_address_of_its_own(self):
		sync, _name = self.run_step(
			shipping_first_name="Bob", shipping_last_name="Smith", shipping_address_1="Gift Street 2"
		)

		self.assertNotEqual(sync.shipping_address, sync.billing_address)
		shipping = frappe.get_doc("Address", sync.shipping_address)
		# Titled for whom it is, and reached at the billing phone, as it has none of its own
		self.assertEqual(
			(shipping.address_title, shipping.address_type, shipping.phone),
			("Bob Smith", "Shipping", "555-0100"),
		)
		self.assertEqual((shipping.is_primary_address, shipping.is_shipping_address), (0, 1))
		self.assertEqual(
			frappe.db.get_value(
				"Address", sync.billing_address, ["is_primary_address", "is_shipping_address"]
			),
			(1, 0),
		)

	def test_an_empty_shipping_address_is_the_billing_one(self):
		sync, _name = self.run_step(
			**{
				f"shipping_{key}": ""
				for key in ("first_name", "last_name", "address_1", "city", "postcode", "country")
			}
		)

		self.assertEqual(sync.shipping_address, sync.billing_address)

	def test_the_sales_order_links_the_address_and_contact_of_its_order(self):
		sync, name = self.run_step(
			shipping_first_name="Bob", shipping_last_name="Smith", shipping_address_1="Gift Street 2"
		)
		sales_order = frappe.new_doc("Sales Order")

		sync.set_customer_in_sales_order(sales_order)

		self.assertEqual(
			(
				sales_order.customer,
				sales_order.customer_address,
				sales_order.shipping_address_name,
				sales_order.contact_person,
			),
			(name, sync.billing_address, sync.shipping_address, sync.contact),
		)
		self.assertEqual(
			(sales_order.contact_display, sales_order.contact_email, sales_order.contact_mobile),
			("Ann Smith", "ann@example.com", "555-0100"),
		)

	def create_sales_order(self, sync, customer_issues=(), phone_issues=(), line_issues=()):
		"""
		The Sales Order that `sync` makes of an order whose customer step and lines found these issues, and what
		it was flagged with when it was submitted
		"""
		sync.woocommerce_order = _dict(
			woocommerce_server=SERVER,
			woocommerce_id="12",
			number="12",
			woocommerce_date_created="2026-11-03T10:00:00",
			woocommerce_date_modified="2026-11-03T10:00:00",
			currency="PLN",
		)
		sync.line_issues = list(line_issues)
		sales_order = MagicMock(flags=_dict(), woocommerce_sync_issue=None)
		flagged_at_submit = []
		sales_order.submit.side_effect = lambda: flagged_at_submit.append(sales_order.woocommerce_sync_issue)

		def customer_step():
			sync.customer_issues = list(customer_issues)
			sync.phone_issues = list(phone_issues)
			return "_Test WC Bob"

		with (
			patch.object(sync, "create_or_link_customer_and_address", side_effect=customer_step),
			patch.object(sync, "create_missing_items"),
			patch.object(sync, "set_customer_in_sales_order"),
			patch.object(sync, "set_items_in_sales_order"),
			patch.object(sync, "check_totals", return_value=None),
			patch.object(frappe, "new_doc", return_value=sales_order),
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
		):
			sync.create_sales_order(SUBMIT)
		return sales_order, flagged_at_submit

	def test_what_to_check_about_the_customer_is_flagged_before_the_submit(self):
		_sales_order, flagged_at_submit = self.create_sales_order(
			make_sync(woocommerce_order_name=f"{SERVER}~12"),
			customer_issues=["Placed as Ann Smith, but its Customer is Bob Smith"],
			line_issues=["Line 1 has no Item"],
		)

		# Flagged, the Sales Order isn't invoiced until a person has looked at it
		self.assertEqual(
			flagged_at_submit, ["Placed as Ann Smith, but its Customer is Bob Smith; Line 1 has no Item"]
		)

	def test_a_phone_erpnext_can_not_take_is_flagged_by_the_sync(self):
		customer_issue = "Placed as Ann Smith, but its Customer is Bob Smith"
		phone_issue = "The billing phone 1 / 2 isn't a number ERPNext takes"
		line_issue = "Line 1 has no Item"

		sales_order, flagged_at_submit = self.create_sales_order(
			make_sync(woocommerce_order_name=f"{SERVER}~12"),
			customer_issues=[customer_issue],
			phone_issues=[phone_issue],
			line_issues=[line_issue],
		)

		self.assertEqual(flagged_at_submit, ["; ".join([customer_issue, phone_issue, line_issue])])
		sales_order.add_comment.assert_not_called()

	def test_an_import_tells_of_a_phone_erpnext_can_not_take_in_a_comment(self):
		note = "The billing phone <1> / 2 isn't a number ERPNext takes"
		importer = order_import.OrderImport(MagicMock(), f"{SERVER}~12")

		sales_order, flagged_at_submit = self.create_sales_order(importer, phone_issues=[note])

		# A flag would hold back the invoice of an order that shipped long ago
		self.assertEqual(flagged_at_submit, [None])
		sales_order.add_comment.assert_called_once_with("Comment", frappe.utils.escape_html(note))

	def test_tax_ids_are_compared_however_they_are_written(self):
		for tax_id in ("PL 679-100-38-01", "pl6791003801", "679 100 38 01"):
			with self.subTest(tax_id=tax_id):
				self.assertEqual(normalise_tax_id(tax_id), "6791003801")
		self.assertEqual(normalise_tax_id(None), "")

	def test_phone_numbers_are_cleaned_as_erpnext_takes_them(self):
		for typed, cleaned in (
			# Taken as they are, so the Addresses made from them still match
			("555-0100", "555-0100"),
			("+48 (12) 345 67 89", "+48 (12) 345 67 89"),
			("-", "-"),
			# En dash, em dash and minus sign
			("+48 \u2013 500-100-200", "+48 - 500-100-200"),
			("500\u2014100\u2014200", "500-100-200"),
			("500\u2212100\u2212200", "500-100-200"),
			# No-break and thin spaces
			("500\u00a0100\u00a0200", "500 100 200"),
			("500\u2009100\u2009200", "500 100 200"),
			# Full-width digits and plus sign
			("\uff0b48 \uff15\uff10\uff10 100 200", "+48 500 100 200"),
			# Other characters are dropped
			("tel. 500 100 200", "500 100 200"),
			("500/100/200", "500 100 200"),
			# Still too long for ERPNext, so left out
			("+48 500 100 200 / +48 600 100 200", ""),
			("brak", ""),
			(None, ""),
		):
			with self.subTest(typed=typed):
				self.assertEqual(clean_phone(typed), cleaned)

	def test_a_phone_typed_with_a_dash_is_cleaned_on_the_address_and_contact(self):
		sync, _name = self.run_step(billing_phone="+48 \u2013 500-100-200")

		self.assertEqual(frappe.db.get_value("Address", sync.billing_address, "phone"), "+48 - 500-100-200")
		self.assertEqual(frappe.db.get_value("Contact", sync.contact, "phone"), "+48 - 500-100-200")
		self.assertEqual(sync.phone_issues, [])

	def test_a_phone_erpnext_can_not_take_is_left_out_and_noted(self):
		typed = "+48 500 100 200 / +48 600 100 200"

		sync, _name = self.run_step(billing_phone=typed)

		self.assertFalse(frappe.db.get_value("Address", sync.billing_address, "phone"))
		contact = frappe.get_doc("Contact", sync.contact)
		self.assertEqual((contact.email_id, contact.phone_nos), ("ann@example.com", []))
		self.assertEqual(len(sync.phone_issues), 1)
		self.assertIn(typed, sync.phone_issues[0])

	def test_a_shipping_phone_erpnext_can_not_take_gives_way_to_the_billing_one(self):
		typed = "+48 500 100 200 / +48 600 100 200"

		sync, _name = self.run_step(
			shipping_first_name="Bob",
			shipping_last_name="Smith",
			shipping_address_1="Gift Street 2",
			shipping_phone=typed,
		)

		self.assertEqual(frappe.db.get_value("Address", sync.shipping_address, "phone"), "555-0100")
		self.assertEqual(len(sync.phone_issues), 1)
		self.assertIn(typed, sync.phone_issues[0])

	def test_a_phone_field_without_a_number_is_left_out_unnoted(self):
		sync, _name = self.run_step(billing_phone="brak")

		self.assertFalse(frappe.db.get_value("Address", sync.billing_address, "phone"))
		self.assertEqual(sync.phone_issues, [])


class TestCustomerShopPatch(IntegrationTestCase):
	def test_customers_get_the_shop_of_their_first_order(self):
		other = "two.example.com"
		for name, identifier in (("Ann", "ann"), ("Guest", "Guest-12"), ("Bob", "bob"), ("Hand", None)):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"name": f"_Test WC {name}",
					"customer_name": name,
					"woocommerce_identifier": identifier,
				}
			).db_insert()
		for idx, (customer, server, creation) in enumerate(
			(
				("Ann", other, "2026-01-02"),
				("Ann", SERVER, "2026-01-01"),
				("Guest", SERVER, "2026-01-03"),
				("Hand", SERVER, "2026-01-03"),
			)
		):
			frappe.get_doc(
				{
					"doctype": "Sales Order",
					"name": f"_Test WC SO {idx}",
					"customer": f"_Test WC {customer}",
					"woocommerce_server": server,
					"creation": creation,
				}
			).db_insert()

		def run(servers):
			with patch.object(frappe, "get_all", return_value=servers), patch("builtins.print") as print_:
				set_customer_woocommerce_server.execute()
			shops = {
				name: frappe.db.get_value("Customer", f"_Test WC {name}", "woocommerce_server")
				for name in ("Ann", "Guest", "Bob", "Hand")
			}
			return shops, print_

		# Ann ordered in both shops, in this one first. Bob has no Sales Order, Hand was made by hand.
		shops, print_ = run([SERVER, other])
		self.assertEqual(shops, {"Ann": SERVER, "Guest": SERVER, "Bob": None, "Hand": None})
		self.assertIn("_Test WC Ann", print_.call_args.args[0])

		# With one shop, a Customer the sync made is of that shop, with or without a Sales Order
		shops, print_ = run([SERVER])
		self.assertEqual((shops["Bob"], shops["Hand"]), (SERVER, None))
		print_.assert_not_called()
