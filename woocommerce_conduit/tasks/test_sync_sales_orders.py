# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

import json
from contextlib import nullcontext
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit import endpoint
from woocommerce_conduit.exceptions import SyncDisabledError
from woocommerce_conduit.tasks import order_lines, order_payment, sync_sales_orders
from woocommerce_conduit.tasks.order_lines import (
	build_charges,
	get_line_rate,
	get_product_id,
	rates_match,
	totals_differ,
)
from woocommerce_conduit.tasks.order_payment import get_payment_method, unlink_from_sales_order
from woocommerce_conduit.tasks.order_status import (
	enqueue_status_push_from_hook,
	get_status_to_push,
	get_statuses_to_sync,
)
from woocommerce_conduit.tasks.sync import SynchroniseWooCommerce
from woocommerce_conduit.tasks.sync_sales_orders import (
	SynchroniseSalesOrder,
	enqueue_sales_order_sync,
	get_names_of_wc_orders,
	sync_woocommerce_orders_modified_since,
	wc_order_changed,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_CANCEL,
	ACTION_COMPLETE,
	ACTION_SKIP,
	ACTION_STATUS_ONLY,
	ACTION_SUBMIT_HOLD,
	ACTION_SUBMIT_INVOICE,
	ACTION_SUBMIT_INVOICE_HOLD,
	get_default_status_rows,
	normalise_status,
)
from woocommerce_conduit.woocommerce.woocommerce_api import map_frappe_filters_to_wc_params

SERVER = "one.example.com"


def make_server(rows=None, push=True, **fields):
	"""
	A WooCommerce Server with a status map and payment settings, which is all the sync asks of it. The
	default map unless given.
	"""
	return frappe.get_doc(
		{
			"doctype": "WooCommerce Server",
			"name": SERVER,
			"woocommerce_server_url": f"https://{SERVER}",
			"enabled": 1,
			"enabled_order_status": 1 if push else 0,
			"sales_order_status_map": get_default_status_rows() if rows is None else rows,
			"create_sales_invoice": 1,
			"submit_sales_invoice": 1,
			"enabled_payments_sync": 1,
			"payment_method_map": [
				{
					"woocommerce_payment_method": "stripe",
					"mode_of_payment": "Credit Card",
					"bank_account": "Stripe Bank",
				},
				{"woocommerce_payment_method": "cod"},
			],
			**fields,
		}
	)


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

	def test_invoice_and_payment_are_synced_whether_or_not_the_order_changed(self):
		for last_sync in (datetime(2026, 10, 30, 10, 0, 0), datetime(2026, 10, 30, 9, 0, 0)):
			sync = self.sync_with(last_sync)
			with (
				patch.object(frappe, "get_cached_doc", return_value=make_server()),
				patch.object(sync, "update_sales_order"),
				patch.object(sync, "sync_invoice_and_payment") as sync_invoice_and_payment,
			):
				sync.sync_wc_order_with_erpnext_order()
			sync_invoice_and_payment.assert_called_once_with(ACTION_STATUS_ONLY)

	def test_changed_order_is_pulled(self):
		sync = self.sync_with(datetime(2026, 10, 30, 9, 0, 0))
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(sync, "update_sales_order") as update_sales_order,
		):
			sync.sync_wc_order_with_erpnext_order()
		update_sales_order.assert_called_once_with(ACTION_STATUS_ONLY)

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
				sync_sales_orders,
				"get_names_of_wc_orders",
				return_value=[f"{SERVER}~12", "two.example.com~5", f"{SERVER}~13"],
			) as get_names,
			patch.object(sync_sales_orders, "get_statuses_to_sync", return_value=["pending"]),
			patch.object(sync_sales_orders, "enqueue_sales_order_sync") as enqueue,
		):
			sync_sales_orders.sync_paused_orders(SERVER, since="2026-10-30 09:00:00")

		self.assertEqual(get_names.call_args.kwargs["date_time_from"], "2026-10-30 09:00:00")
		self.assertEqual(
			[c.kwargs["woocommerce_order_name"] for c in enqueue.call_args_list],
			[f"{SERVER}~12", f"{SERVER}~13"],
		)


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


class TestStatusMap(IntegrationTestCase):
	def test_core_statuses_have_defaults(self):
		rows = {row["woocommerce_status"]: row for row in get_default_status_rows()}

		self.assertEqual(rows["checkout-draft"]["erpnext_action"], ACTION_SKIP)
		self.assertEqual(rows["pending"]["erpnext_action"], ACTION_SUBMIT_INVOICE)
		self.assertEqual(rows["failed"]["erpnext_action"], ACTION_CANCEL)
		self.assertEqual(rows["completed"]["erpnext_action"], ACTION_COMPLETE)
		self.assertEqual(rows["on-hold"]["erpnext_action"], ACTION_SUBMIT_HOLD)
		self.assertEqual(rows["processing"]["erpnext_action"], ACTION_SUBMIT_INVOICE)
		self.assertEqual(rows["cancelled"]["erpnext_action"], ACTION_CANCEL)
		self.assertEqual(rows["completed"]["push_from_erpnext_status"], "Completed")
		self.assertEqual(rows["cancelled"]["push_from_erpnext_status"], "Cancelled")

	def test_statuses_of_the_shop_are_added(self):
		live = {"wc-processing": "Being processed", "ready-pickup": "Ready for pickup"}
		rows = {row["woocommerce_status"]: row for row in get_default_status_rows(live)}

		# A plugin status does nothing until the user decides what it means
		self.assertEqual(rows["ready-pickup"]["erpnext_action"], ACTION_STATUS_ONLY)
		self.assertEqual(rows["ready-pickup"]["woocommerce_status_label"], "Ready for pickup")
		# The core status keeps its action, with the name of the shop
		self.assertEqual(rows["processing"]["woocommerce_status_label"], "Being processed")
		self.assertEqual(rows["processing"]["erpnext_action"], ACTION_SUBMIT_INVOICE)

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
		server = make_server(rows=[{"woocommerce_status": "pending", "erpnext_action": ACTION_SKIP}])
		server.before_insert()
		self.assertEqual(len(server.sales_order_status_map), 1)

	def test_lookup_by_status(self):
		server = make_server()
		self.assertEqual(server.get_order_status_row("wc-on-hold").erpnext_action, ACTION_SUBMIT_HOLD)
		self.assertIsNone(server.get_order_status_row("quote-sent"))
		self.assertEqual(server.get_push_row("Completed").woocommerce_status, "completed")
		self.assertIsNone(server.get_push_row("To Deliver"))
		self.assertIsNone(server.get_push_row(None))

	def test_default_map_is_valid(self):
		make_server().validate_so_status_map()

	def test_a_status_can_only_be_in_the_map_once(self):
		rows = [
			{"woocommerce_status": "completed", "erpnext_action": ACTION_SKIP},
			{"woocommerce_status": "wc-completed", "erpnext_action": ACTION_SKIP},
		]
		with self.assertRaises(frappe.ValidationError):
			make_server(rows).validate_so_status_map()

	def test_a_sales_order_status_is_pushed_as_one_status(self):
		rows = [
			{
				"woocommerce_status": "completed",
				"erpnext_action": ACTION_SKIP,
				"push_from_erpnext_status": "Completed",
			},
			{
				"woocommerce_status": "delivered",
				"erpnext_action": ACTION_SKIP,
				"push_from_erpnext_status": "Completed",
			},
		]
		with self.assertRaises(frappe.ValidationError):
			make_server(rows).validate_so_status_map()

	def test_statuses_of_the_gateway_are_not_pushed(self):
		for status in ("pending", "failed", "on-hold", "processing", "checkout-draft"):
			rows = [
				{
					"woocommerce_status": status,
					"erpnext_action": ACTION_SKIP,
					"push_from_erpnext_status": "To Bill",
				}
			]
			with self.assertRaises(frappe.ValidationError, msg=status):
				make_server(rows).validate_so_status_map()


class TestCreatingOrders(IntegrationTestCase):
	def sync_new_order(self, status):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(
			status=status, woocommerce_server=SERVER, woocommerce_date_modified="2026-10-30T10:00:00"
		)
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(sync, "create_sales_order") as create_sales_order,
		):
			sync.sync_wc_order_with_erpnext_order()
		return create_sales_order

	def test_orders_that_mean_nothing_are_not_created(self):
		# Making a Sales Order also makes a Customer, Address and Contact, so none of these is
		for status in ("checkout-draft", "failed", "cancelled", "trash", "refunded", "quote-sent"):
			self.sync_new_order(status).assert_not_called()

	def test_invoice_and_payment_follow_the_creation(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(
			status="pending", woocommerce_server=SERVER, woocommerce_date_modified="2026-10-30T10:00:00"
		)
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(sync, "create_sales_order"),
			patch.object(sync, "sync_invoice_and_payment") as sync_invoice_and_payment,
		):
			sync.sync_wc_order_with_erpnext_order()
		sync_invoice_and_payment.assert_called_once_with(ACTION_SUBMIT_INVOICE)

	def test_orders_that_are_wanted_are_created(self):
		self.sync_new_order("pending").assert_called_once_with(ACTION_SUBMIT_INVOICE)
		self.sync_new_order("processing").assert_called_once_with(ACTION_SUBMIT_INVOICE)
		self.sync_new_order("on-hold").assert_called_once_with(ACTION_SUBMIT_HOLD)
		self.sync_new_order("completed").assert_called_once_with(ACTION_COMPLETE)

	def test_order_seen_before_is_not_new(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.sales_order = MagicMock(woocommerce_last_sync_hash="2026-10-30 10:00:00")
		sync.woocommerce_order = _dict(
			status="completed", woocommerce_server=SERVER, woocommerce_date_modified="2026-10-30T10:00:00"
		)
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server()),
			patch.object(sync, "sync_invoice_and_payment") as sync_invoice_and_payment,
		):
			sync.sync_wc_order_with_erpnext_order()
		sync_invoice_and_payment.assert_called_once_with(ACTION_COMPLETE)


class TestPullingStatus(IntegrationTestCase):
	def pull(self, previous_status, status, action):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		calls = []
		sync.sales_order = MagicMock(
			docstatus=1,
			woocommerce_status=previous_status,
			woocommerce_customer_note=None,
			woocommerce_payment_method="Card",
			woocommerce_payment_entry="ACC-PAY-1",
			flags=_dict(),
		)
		sync.sales_order.save.side_effect = lambda: calls.append("save")
		sync.woocommerce_order = _dict(
			status=status,
			customer_note=None,
			payment_method_title="Card",
			payment_method="stripe",
			woocommerce_date_modified="2026-10-30T10:00:00",
		)
		with (
			patch.object(sync, "apply_status_action", side_effect=lambda a: calls.append(a)),
			patch.object(sync, "update_sales_order_items", side_effect=lambda: calls.append("lines")),
		):
			sync.update_sales_order(action)
		return sync, calls

	def test_status_is_stored_as_the_slug(self):
		sync, _calls = self.pull("pending", "wc-processing", ACTION_SUBMIT_INVOICE)
		self.assertEqual(sync.sales_order.woocommerce_status, "processing")

	def test_action_follows_the_lines_when_the_status_changed(self):
		_sync, calls = self.pull("pending", "processing", ACTION_SUBMIT_INVOICE)
		self.assertEqual(calls, ["save", "lines", ACTION_SUBMIT_INVOICE])

	def test_lines_of_a_cancelled_order_are_left(self):
		_sync, calls = self.pull("pending", "failed", ACTION_CANCEL)
		self.assertEqual(calls, ["save", ACTION_CANCEL])

	def test_action_is_not_repeated_for_other_changes(self):
		# The note of an order was edited: a hold put on the Sales Order in ERPNext must stay
		_sync, calls = self.pull("processing", "processing", ACTION_SUBMIT_INVOICE)
		self.assertEqual(calls, ["save", "lines"])


class TestStatusActions(IntegrationTestCase):
	def make(self, **values):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.sales_order = MagicMock(
			**{
				"docstatus": 1,
				"status": "To Deliver and Bill",
				"per_delivered": 0,
				"per_billed": 0,
				"woocommerce_payment_entry": None,
				"flags": _dict(),
				**values,
			}
		)
		sync.sales_order.get.return_value = values.get("invoice")
		return sync

	def test_changes_are_not_pushed_back(self):
		sync = self.make()
		sync.apply_status_action(ACTION_STATUS_ONLY)
		self.assertTrue(sync.sales_order.flags.created_by_sync)

	def test_hold_and_resume(self):
		sync = self.make()
		sync.apply_status_action(ACTION_SUBMIT_HOLD)
		sync.sales_order.update_status.assert_called_once_with("On Hold")

		sync = self.make(status="On Hold")
		sync.apply_status_action(ACTION_SUBMIT_HOLD)
		sync.sales_order.update_status.assert_not_called()
		sync.apply_status_action(ACTION_SUBMIT_INVOICE)
		sync.sales_order.update_status.assert_called_once_with("Draft")

	def test_resume_leaves_an_order_that_is_not_on_hold(self):
		sync = self.make()
		sync.apply_status_action(ACTION_SUBMIT_INVOICE)
		sync.sales_order.update_status.assert_not_called()

	def test_unpaid_order_is_cancelled(self):
		sync = self.make()
		sync.apply_status_action(ACTION_CANCEL)
		sync.sales_order.cancel.assert_called_once()
		sync.sales_order.update_status.assert_not_called()

	def test_unpaid_invoice_is_cancelled_first(self):
		invoice = MagicMock(outstanding_amount=100, rounded_total=100, grand_total=100, flags=_dict())
		sync = self.make(invoice="ACC-SINV-1", per_billed=100)
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe, "get_doc", return_value=invoice),
		):
			sync.apply_status_action(ACTION_CANCEL)

		invoice.cancel.assert_called_once()
		sync.sales_order.cancel.assert_called_once()

	def test_paid_order_is_closed_and_flagged(self):
		for values in (
			{"woocommerce_payment_entry": "ACC-PAY-1"},
			{"per_delivered": 50},
			{"per_billed": 100},
		):
			sync = self.make(**values)
			sync.apply_status_action(ACTION_CANCEL)

			sync.sales_order.cancel.assert_not_called()
			sync.sales_order.update_status.assert_called_once_with("Closed")
			sync.sales_order.db_set.assert_called_once()
			self.assertEqual(sync.sales_order.db_set.call_args.args[0], "woocommerce_sync_issue")

	def test_invoice_with_a_payment_closes_the_order(self):
		invoice = MagicMock(outstanding_amount=40, rounded_total=100, grand_total=100, flags=_dict())
		sync = self.make(invoice="ACC-SINV-1", per_billed=100)
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe, "get_doc", return_value=invoice),
		):
			sync.apply_status_action(ACTION_CANCEL)

		invoice.cancel.assert_not_called()
		sync.sales_order.cancel.assert_not_called()
		sync.sales_order.update_status.assert_called_once_with("Closed")


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

	def test_pushing_can_be_switched_off(self):
		with patch.object(frappe, "get_cached_doc", return_value=make_server(push=False)):
			self.assertIsNone(get_status_to_push(self.make_sales_order()))

	def test_hook_enqueues_a_job_by_name(self):
		doc = self.make_sales_order()
		doc.name = "SAL-ORD-1"
		with (
			patch.dict(frappe.flags, {"in_test": False}),
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
				patch.dict(frappe.flags, {"in_test": False}),
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


class TestWebhook(IntegrationTestCase):
	def post(self, event, body):
		headers = {"x-wc-webhook-event": event, "x-wc-webhook-source": f"https://{SERVER}/"}
		with (
			patch.object(endpoint, "validate_request", return_value=(True, None)),
			patch.object(frappe, "request", MagicMock(data=body)),
			patch.object(
				frappe, "get_request_header", side_effect=lambda key, default=None: headers.get(key, default)
			),
			patch.object(endpoint, "enqueue_sales_order_sync") as enqueue,
		):
			response = endpoint.order_created()
		return response, enqueue

	def test_created_and_updated_orders_are_synced(self):
		for event in ("created", "updated"):
			response, enqueue = self.post(event, b'{"id": 12}')
			self.assertEqual(response.status_code, 200)
			enqueue.assert_called_once_with(woocommerce_order_name=f"{SERVER}~12")

	def test_other_events_are_refused(self):
		response, enqueue = self.post("deleted", b'{"id": 12}')
		self.assertEqual(response.status_code, 400)
		enqueue.assert_not_called()

	def test_ping_is_accepted(self):
		response, enqueue = self.post("created", b"webhook_id=3")
		self.assertEqual(response.status_code, 200)
		enqueue.assert_not_called()


class InvoiceAndPaymentCase(IntegrationTestCase):
	"""Makes a synced Sales Order and runs its invoice and payment step with ERPNext mocked"""

	def setUp(self):
		self.delivery_note = MagicMock(flags=_dict())

	def make(self, server=None, **sales_order_values):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.sales_order = MagicMock(
			**{
				"docstatus": 1,
				"status": "To Deliver and Bill",
				"woocommerce_sales_invoice": None,
				"woocommerce_payment_entry": None,
				"woocommerce_sync_issue": None,
				"per_billed": 0,
				"grand_total": 100,
				"transaction_date": "2026-10-29",
				"flags": _dict(),
				**sales_order_values,
			}
		)
		sync.sales_order.name = "SAL-ORD-1"
		sync.woocommerce_order = _dict(
			name=f"{SERVER}~12",
			woocommerce_server=SERVER,
			payment_method="stripe",
			payment_method_title="Card",
			transaction_id="ch_1",
			date_paid="2026-10-30T10:00:00",
		)
		sync.server = server or make_server()
		return sync

	def run_sync(
		self,
		sync,
		action=ACTION_SUBMIT_INVOICE,
		payment_reference_status=1,
		account="1100 - Bank - T",
	):
		invoice = MagicMock()
		invoice.name = "ACC-SINV-1"
		payment_entry = MagicMock()
		payment_entry.name = "ACC-PAY-1"
		with (
			patch.object(frappe, "get_cached_doc", return_value=sync.server),
			patch.object(frappe, "log_error"),
			patch.object(frappe.db, "get_value", return_value=payment_reference_status)
			if payment_reference_status is not None
			else nullcontext(),
			patch.object(order_payment, "make_sales_invoice", return_value=invoice) as make_sales_invoice,
			patch.object(order_payment, "get_payment_entry", return_value=payment_entry) as get_payment_entry,
			patch.object(order_payment, "get_mode_of_payment_account", return_value=account),
			patch.object(order_payment, "make_delivery_note", return_value=self.delivery_note),
		):
			sync.sync_invoice_and_payment(action)
		return invoice, payment_entry, make_sales_invoice, get_payment_entry

	def linked(self, sync, fieldname):
		calls = [c.args for c in sync.sales_order.db_set.call_args_list if c.args[0] == fieldname]
		return calls[-1][1] if calls else None


class TestInvoiceAndPayment(InvoiceAndPaymentCase):
	def test_invoice_is_made_without_waiting_for_delivery(self):
		sync = self.make(make_server(enabled_payments_sync=0))
		invoice, _pe, make_sales_invoice, _get_pe = self.run_sync(sync)

		make_sales_invoice.assert_called_once_with("SAL-ORD-1")
		self.assertEqual(invoice.update_stock, 0)
		# The date of the order, not of the day it was synced
		self.assertEqual(invoice.posting_date, "2026-10-29")
		invoice.insert.assert_called_once()
		invoice.submit.assert_called_once()
		self.assertEqual(self.linked(sync, "woocommerce_sales_invoice"), "ACC-SINV-1")

	def test_invoice_is_left_a_draft_when_submitting_is_off(self):
		sync = self.make(make_server(enabled_payments_sync=0, submit_sales_invoice=0))
		invoice, *_ = self.run_sync(sync)

		invoice.insert.assert_called_once()
		invoice.submit.assert_not_called()

	def test_invoice_only_for_statuses_that_say_so(self):
		sync = self.make(make_server(enabled_payments_sync=0))
		_inv, _pe, make_sales_invoice, _ = self.run_sync(sync, action=ACTION_SUBMIT_HOLD)
		make_sales_invoice.assert_not_called()

		sync = self.make(make_server(enabled_payments_sync=0, create_sales_invoice=0))
		_inv, _pe, make_sales_invoice, _ = self.run_sync(sync)
		make_sales_invoice.assert_not_called()

	def test_invoice_is_made_once_and_not_for_billed_or_flagged_orders(self):
		for values in (
			{"woocommerce_sales_invoice": "ACC-SINV-9"},
			{"per_billed": 100},
			{"woocommerce_sync_issue": "Totals differ"},
		):
			sync = self.make(make_server(enabled_payments_sync=0), **values)
			_inv, _pe, make_sales_invoice, _ = self.run_sync(sync)
			make_sales_invoice.assert_not_called()

	def test_nothing_is_made_for_orders_on_hold_or_closed(self):
		for status in ("On Hold", "Closed"):
			sync = self.make(status=status)
			_inv, _pe, make_sales_invoice, get_payment_entry = self.run_sync(sync)
			make_sales_invoice.assert_not_called()
			get_payment_entry.assert_not_called()

	def test_nothing_is_made_for_a_sales_order_that_is_not_submitted(self):
		sync = self.make(docstatus=2)
		_inv, _pe, make_sales_invoice, get_payment_entry = self.run_sync(sync)
		make_sales_invoice.assert_not_called()
		get_payment_entry.assert_not_called()

	def test_payment_is_booked_against_the_invoice(self):
		sync = self.make(woocommerce_sales_invoice="ACC-SINV-1")
		_inv, payment_entry, _msi, get_payment_entry = self.run_sync(sync)

		get_payment_entry.assert_called_once_with(
			"Sales Invoice", "ACC-SINV-1", bank_account="1100 - Bank - T", reference_date=date(2026, 10, 30)
		)
		self.assertEqual(payment_entry.posting_date, date(2026, 10, 30))
		self.assertEqual(payment_entry.reference_no, "ch_1")
		self.assertEqual(payment_entry.bank_account, "Stripe Bank")
		self.assertEqual(payment_entry.mode_of_payment, "Credit Card")
		payment_entry.insert.assert_called_once()
		payment_entry.submit.assert_called_once()
		self.assertEqual(self.linked(sync, "woocommerce_payment_entry"), "ACC-PAY-1")

	def test_payment_waits_for_the_invoice_to_be_submitted(self):
		sync = self.make(woocommerce_sales_invoice="ACC-SINV-1")
		_inv, _pe, _msi, get_payment_entry = self.run_sync(sync, payment_reference_status=0)
		get_payment_entry.assert_not_called()

	def test_payment_without_an_invoice_is_an_advance_on_the_sales_order(self):
		sync = self.make(make_server(create_sales_invoice=0))
		_inv, _pe, _msi, get_payment_entry = self.run_sync(sync, action=ACTION_STATUS_ONLY)
		self.assertEqual(get_payment_entry.call_args.args[:2], ("Sales Order", "SAL-ORD-1"))

	def test_both_are_made_for_a_paid_order(self):
		sync = self.make()
		invoice, payment_entry, make_sales_invoice, get_payment_entry = self.run_sync(sync)

		make_sales_invoice.assert_called_once()
		get_payment_entry.assert_called_once()
		self.assertEqual(self.linked(sync, "woocommerce_sales_invoice"), invoice.name)
		self.assertEqual(self.linked(sync, "woocommerce_payment_entry"), payment_entry.name)

	def test_payment_is_not_made_twice_or_for_nothing(self):
		for values, order_values in (
			({"woocommerce_payment_entry": "ACC-PAY-9"}, {}),
			({"grand_total": 0}, {}),
			({}, {"date_paid": None}),
			({}, {"payment_method": None}),
		):
			sync = self.make(make_server(create_sales_invoice=0), **values)
			sync.woocommerce_order.update(order_values)
			_inv, _pe, _msi, get_payment_entry = self.run_sync(sync, action=ACTION_STATUS_ONLY)
			get_payment_entry.assert_not_called()

	def test_payment_method_without_a_mode_of_payment_is_not_booked(self):
		# cod has a row without a Mode of Payment, paypal has no row at all
		for method in ("cod", "paypal"):
			sync = self.make(make_server(create_sales_invoice=0))
			sync.woocommerce_order.payment_method = method
			_inv, _pe, _msi, get_payment_entry = self.run_sync(sync, action=ACTION_STATUS_ONLY)

			get_payment_entry.assert_not_called()
			self.assertIsNone(self.linked(sync, "woocommerce_sync_issue"))

	def test_mode_of_payment_without_an_account_is_flagged(self):
		sync = self.make(make_server(create_sales_invoice=0), company="Test Company")
		_inv, _pe, _msi, get_payment_entry = self.run_sync(sync, action=ACTION_STATUS_ONLY, account=None)

		get_payment_entry.assert_not_called()
		self.assertIn("Credit Card", self.linked(sync, "woocommerce_sync_issue"))

	def test_a_step_that_fails_is_flagged_and_the_next_still_runs(self):
		sync = self.make()
		payment_entry = MagicMock()
		payment_entry.name = "ACC-PAY-1"
		with (
			patch.object(frappe, "get_cached_doc", return_value=sync.server),
			patch.object(frappe, "log_error") as log_error,
			patch.object(frappe.db, "get_value", return_value=1),
			patch.object(
				order_payment, "make_sales_invoice", side_effect=frappe.ValidationError("No account")
			),
			patch.object(order_payment, "get_payment_entry", return_value=payment_entry),
			patch.object(order_payment, "get_mode_of_payment_account", return_value="1100 - Bank - T"),
		):
			sync.sync_invoice_and_payment(ACTION_SUBMIT_INVOICE)

		log_error.assert_called_once()
		self.assertIn("No account", self.linked(sync, "woocommerce_sync_issue"))
		self.assertEqual(self.linked(sync, "woocommerce_payment_entry"), "ACC-PAY-1")

	def test_paid_invoice_gets_no_second_payment(self):
		# The Payment Entry of the sync was cancelled, and the invoice paid by hand
		sync = self.make(woocommerce_sales_invoice="ACC-SINV-1")
		values = {"docstatus": 1, "outstanding_amount": 0}
		with patch.object(frappe.db, "get_value", side_effect=lambda dt, name, field: values[field]):
			_inv, _pe, _msi, get_payment_entry = self.run_sync(sync, payment_reference_status=None)
		get_payment_entry.assert_not_called()

	def test_paid_sales_order_gets_no_second_advance(self):
		sync = self.make(make_server(create_sales_invoice=0), advance_paid=100)
		_inv, _pe, _msi, get_payment_entry = self.run_sync(sync, action=ACTION_STATUS_ONLY)
		get_payment_entry.assert_not_called()

	def test_cancelling_unlinks_the_sales_order(self):
		for doctype, fieldname in (
			("Sales Invoice", "woocommerce_sales_invoice"),
			("Payment Entry", "woocommerce_payment_entry"),
		):
			with (
				patch.object(frappe, "get_all", return_value=["SAL-ORD-1"]) as get_all,
				patch.object(frappe.db, "set_value") as set_value,
			):
				unlink_from_sales_order(_dict(doctype=doctype, name="DOC-1"))

			self.assertEqual(get_all.call_args.kwargs["filters"], {fieldname: "DOC-1"})
			set_value.assert_called_once_with(
				"Sales Order", "SAL-ORD-1", fieldname, None, update_modified=False
			)

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
		return _dict(
			{
				"freight_and_forwarding_account": "Freight",
				"tax_account": "VAT",
				"use_actual_tax_type": 1,
				**values,
			}
		)

	def test_actual_tax_is_one_row_per_rate(self):
		charges = build_charges(self.order(), self.server(fee_account="Fees"), with_shipping=True)

		self.assertEqual(
			[(c["account_head"], c["tax_amount"], c["description"]) for c in charges],
			[("Freight", 5, "Shipping"), ("Fees", 1, "Gift wrap"), ("VAT", 5.04, "VAT")],
		)
		self.assertTrue(all(c["charge_type"] == "Actual" for c in charges))

	def test_order_adds_up(self):
		# 2 x 9 after a coupon, 5 shipping, a 1 fee and 5.04 tax, as WooCommerce totals it
		lines = [line(1, 10, 2, 18, total_tax=3.78)]
		item_total = sum(get_line_rate(row, False) * row["quantity"] for row in lines)
		charges = build_charges(self.order(), self.server(), with_shipping=True)
		self.assertAlmostEqual(item_total + sum(c["tax_amount"] for c in charges), 29.04)

	def test_fees_fall_back_to_the_freight_account(self):
		charges = build_charges(self.order(), self.server(), with_shipping=True)
		self.assertEqual(charges[1]["account_head"], "Freight")

	def test_shipping_rule_charges_the_shipping(self):
		charges = build_charges(self.order(), self.server(), with_shipping=False)
		self.assertNotIn("Shipping", [c["description"] for c in charges])

	def test_template_taxes_the_lines_and_the_rest_is_added(self):
		charges = build_charges(self.order(), self.server(use_actual_tax_type=0), with_shipping=True)
		self.assertEqual(
			[(c["account_head"], c["tax_amount"]) for c in charges],
			[("Freight", 5), ("Freight", 1), ("VAT", 1.26)],
		)


class TestLines(IntegrationTestCase):
	def make(self, lines, items=None, **sales_order_values):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
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
				**sales_order_values,
			}
		)
		sync.sales_order.name = "SAL-ORD-1"
		sync.sales_order.grand_total = sync.woocommerce_order.total
		return sync

	def build(self, sync, item_codes):
		with (
			patch.object(order_lines, "find_item_code", side_effect=lambda server, pid: item_codes.get(pid)),
			patch.object(order_lines, "get_placeholder_item", return_value=order_lines.PLACEHOLDER_ITEM),
			patch.object(frappe, "get_cached_value", return_value="Mug (Item)"),
		):
			return sync.build_item_rows(
				_dict(name=SERVER, warehouse="Stores", use_actual_tax_type=1), "2026-11-06"
			)

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
		)

	def wanted(self, line_id, item_code, qty, rate):
		return {
			"woocommerce_line_item_id": str(line_id),
			"woocommerce_id": "10",
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"warehouse": "Stores",
			"delivery_date": "2026-11-06",
		}

	def update(self, sync, wanted):
		with (
			patch.object(sync, "get_wc_server", return_value=_dict(name=SERVER)),
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

	def test_lines_of_a_delivered_or_invoiced_order_are_flagged(self):
		for values in ({"per_delivered": 50}, {"per_billed": 100}, {"docstatus": 0}):
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
		server = make_server(
			warehouse="Stores", use_actual_tax_type=1, freight_and_forwarding_account="Freight"
		)
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

	def test_an_order_without_lines_or_warehouse_is_an_error(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(name=f"{SERVER}~12", woocommerce_server=SERVER, line_items="[]")
		for server in (make_server(warehouse="Stores"), make_server(warehouse=None)):
			with (
				patch.object(sync, "get_wc_server", return_value=server),
				self.assertRaises(frappe.ValidationError),
			):
				sync.set_items_in_sales_order(frappe.new_doc("Sales Order"))


class TestCompletedOrders(InvoiceAndPaymentCase):
	def test_order_first_seen_completed_gets_everything(self):
		sync = self.make(per_delivered=0)
		sync.woocommerce_order.date_completed = "2026-10-31T12:30:00"
		_inv, _pe, make_sales_invoice, get_payment_entry = self.run_sync(sync, action=ACTION_COMPLETE)

		make_sales_invoice.assert_called_once()
		get_payment_entry.assert_called_once()
		self.delivery_note.submit.assert_called_once()
		self.assertEqual(self.delivery_note.posting_date, date(2026, 10, 31))
		self.assertEqual(self.delivery_note.posting_time, "12:30:00")
		self.assertTrue(self.delivery_note.flags.created_by_sync)

	def test_order_completed_in_woocommerce_later_gets_what_it_lacks(self):
		# Invoiced and paid while pending and processing, then completed in WooCommerce by mistake
		sync = self.make(
			per_delivered=0,
			woocommerce_sales_invoice="ACC-SINV-1",
			woocommerce_payment_entry="ACC-PAY-1",
		)
		_inv, _pe, make_sales_invoice, get_payment_entry = self.run_sync(sync, action=ACTION_COMPLETE)

		make_sales_invoice.assert_not_called()
		get_payment_entry.assert_not_called()
		self.delivery_note.submit.assert_called_once()

	def test_delivered_order_is_not_delivered_again(self):
		sync = self.make(per_delivered=100)
		self.run_sync(sync, action=ACTION_COMPLETE)
		self.delivery_note.insert.assert_not_called()

	def test_processing_after_pending_only_adds_the_payment(self):
		sync = self.make(woocommerce_sales_invoice="ACC-SINV-1")
		_inv, _pe, make_sales_invoice, get_payment_entry = self.run_sync(sync)

		make_sales_invoice.assert_not_called()
		get_payment_entry.assert_called_once()
		self.delivery_note.insert.assert_not_called()

	def test_delivery_that_fails_is_flagged(self):
		sync = self.make(per_delivered=0)
		self.delivery_note.submit.side_effect = frappe.ValidationError("Not enough stock")
		self.run_sync(sync, action=ACTION_COMPLETE)

		self.assertIn("Not enough stock", self.linked(sync, "woocommerce_sync_issue"))
		self.assertEqual(self.linked(sync, "woocommerce_payment_entry"), "ACC-PAY-1")


class TestInvoiceAndHold(InvoiceAndPaymentCase):
	def invoice_and_hold(self, sync):
		calls = []
		sync.sales_order.update_status.side_effect = lambda status: calls.append(status)
		invoice = MagicMock()
		invoice.name = "ACC-SINV-1"
		invoice.insert.side_effect = lambda: calls.append("invoice")
		with (
			patch.object(frappe, "get_cached_doc", return_value=sync.server),
			patch.object(frappe.db, "get_value", return_value=1),
			patch.object(order_payment, "make_sales_invoice", return_value=invoice),
			patch.object(order_payment, "get_payment_entry", return_value=MagicMock()),
			patch.object(order_payment, "get_mode_of_payment_account", return_value="1100 - Bank - T"),
		):
			sync.apply_status_action(ACTION_SUBMIT_INVOICE_HOLD)
		return calls

	def test_order_is_invoiced_before_it_is_held(self):
		sync = self.make(woocommerce_payment_entry="ACC-PAY-9")
		self.assertEqual(self.invoice_and_hold(sync), ["invoice", "On Hold"])

	def test_held_order_is_resumed_to_be_invoiced(self):
		sync = self.make(status="On Hold", woocommerce_payment_entry="ACC-PAY-9")
		calls = self.invoice_and_hold(sync)
		self.assertEqual(calls[:2], ["Draft", "invoice"])

	def test_new_order_is_invoiced_and_held(self):
		sync = make_sync(woocommerce_order_name=f"{SERVER}~12")
		sync.woocommerce_order = _dict(
			status="on-hold", woocommerce_server=SERVER, woocommerce_date_modified="2026-10-30T10:00:00"
		)
		rows = [{"woocommerce_status": "on-hold", "erpnext_action": ACTION_SUBMIT_INVOICE_HOLD}]
		with (
			patch.object(frappe, "get_cached_doc", return_value=make_server(rows)),
			patch.object(sync, "create_sales_order") as create_sales_order,
			patch.object(sync, "sync_invoice_and_payment"),
		):
			sync.sync_wc_order_with_erpnext_order()
		create_sales_order.assert_called_once_with(ACTION_SUBMIT_INVOICE_HOLD)
