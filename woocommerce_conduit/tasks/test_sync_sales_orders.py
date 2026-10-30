# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

from datetime import datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit import endpoint
from woocommerce_conduit.exceptions import SyncDisabledError
from woocommerce_conduit.tasks import sync_sales_orders
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
	ACTION_SKIP,
	ACTION_SKIP_CANCEL,
	ACTION_STATUS_ONLY,
	ACTION_SUBMIT_HOLD,
	ACTION_SUBMIT_INVOICE,
	get_default_status_rows,
	normalise_status,
)
from woocommerce_conduit.woocommerce.woocommerce_api import map_frappe_filters_to_wc_params

SERVER = "one.example.com"


def make_server(rows=None, push=True):
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
		self.assertEqual(rows["failed"]["erpnext_action"], ACTION_SKIP_CANCEL)
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

	def test_orders_that_are_wanted_are_created(self):
		self.sync_new_order("pending").assert_called_once_with(ACTION_SUBMIT_INVOICE)
		self.sync_new_order("processing").assert_called_once_with(ACTION_SUBMIT_INVOICE)
		self.sync_new_order("on-hold").assert_called_once_with(ACTION_SUBMIT_HOLD)


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
		with patch.object(sync, "apply_status_action", side_effect=lambda a: calls.append(a)):
			sync.update_sales_order(action)
		return sync, calls

	def test_status_is_stored_as_the_slug(self):
		sync, _calls = self.pull("pending", "wc-processing", ACTION_SUBMIT_INVOICE)
		self.assertEqual(sync.sales_order.woocommerce_status, "processing")

	def test_action_follows_the_save_when_the_status_changed(self):
		_sync, calls = self.pull("pending", "failed", ACTION_SKIP_CANCEL)
		self.assertEqual(calls, ["save", ACTION_SKIP_CANCEL])

	def test_action_is_not_repeated_for_other_changes(self):
		# The note of an order was edited: a hold put on the Sales Order in ERPNext must stay
		_sync, calls = self.pull("processing", "processing", ACTION_SUBMIT_INVOICE)
		self.assertEqual(calls, ["save"])


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
		sync.apply_status_action(ACTION_SKIP_CANCEL)
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
