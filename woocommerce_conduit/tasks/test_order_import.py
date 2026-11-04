# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase
from rq.timeouts import JobTimeoutException

from woocommerce_conduit.tasks import order_import
from woocommerce_conduit.tasks.order_import import (
	ImportResult,
	OrderImport,
	count_orders_to_import,
	count_results,
	get_date_filters,
	get_notified_users,
	get_orders_to_import,
	import_orders,
	list_orders_to_import,
	notify_orders_to_import,
	send_orders_to_import_notification,
	start_order_import,
	write_log,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
from woocommerce_conduit.woocommerce.doctype.woocommerce_order_import import woocommerce_order_import
from woocommerce_conduit.woocommerce.doctype.woocommerce_order_import_log.woocommerce_order_import_log import (
	RESULT_FAILED,
	RESULT_NEEDS_REVIEW,
	RESULT_PAID,
	RESULT_SKIPPED,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order_import_payment_method.woocommerce_order_import_payment_method import (
	PAYMENT_DATE_COMPLETED,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_SUBMIT,
)

SERVER = "import.example.com"
COMPANY = "_Test Import Company"
ACCOUNT = "Cash - TIC"


def order_name(order_id) -> str:
	return f"{SERVER}~{order_id}"


def make_import(**fields):
	"""
	An unsaved import with a card method that has a Mode of Payment, and cash on delivery without one
	"""
	doc = frappe.get_doc(
		{
			"doctype": "WooCommerce Order Import",
			"woocommerce_server": SERVER,
			"payment_method_map": [
				{
					"woocommerce_payment_method": "stripe",
					"woocommerce_payment_method_title": "Card",
					"mode_of_payment": "Credit Card",
					"bank_account": "Stripe Bank",
				},
				{"woocommerce_payment_method": "cod", "woocommerce_payment_method_title": "Cash on delivery"},
			],
			**fields,
		}
	)
	doc.company = COMPANY
	return doc


def make_order(**values):
	"""
	A completed order paid by card, as the import loads it
	"""
	return _dict(
		{
			"name": order_name(7),
			"woocommerce_id": "7",
			"number": "1007",
			"status": "completed",
			"total": "100.00",
			"payment_method": "stripe",
			"payment_method_title": "Card",
			"transaction_id": "ch_1",
			"date_paid": "2026-10-30 14:00:00",
			"date_completed": "2026-11-02 09:15:30",
			"refunds": "[]",
			**values,
		}
	)


def make_sales_order(**values):
	sales_order = MagicMock()
	sales_order.name = "SAL-ORD-7"
	sales_order.docstatus = 1
	sales_order.status = "To Deliver and Bill"
	sales_order.woocommerce_sync_issue = values.pop("woocommerce_sync_issue", None)
	sales_order.grand_total = values.pop("grand_total", 100)
	sales_order.per_delivered = values.pop("per_delivered", 0)
	sales_order.transaction_date = date(2026, 10, 29)
	for field, value in values.items():
		setattr(sales_order, field, value)
	return sales_order


def make_importer(doc=None, wc_order=None, sales_order=None) -> OrderImport:
	importer = OrderImport(doc or make_import(), order_name(7))
	importer.woocommerce_order = make_order() if wc_order is None else wc_order
	importer.sales_order = sales_order
	return importer


class TestOrderListing(IntegrationTestCase):
	def test_the_completed_orders_of_the_shop_are_listed_by_id_with_their_names_only(self):
		doc = make_import(created_from="2026-01-01", created_to="2026-06-30")
		with (
			patch.object(
				WooCommerceOrder, "get_list_of_records", return_value=[{"name": order_name(1)}]
			) as get_list,
			patch.object(frappe, "get_all", return_value=[]),
		):
			self.assertEqual(get_orders_to_import(doc), [order_name(1)])

		args = get_list.call_args.args[0]
		self.assertEqual(
			args["filters"],
			[
				["WooCommerce Order", "status", "=", "completed"],
				["WooCommerce Order", "date_created", "between", [date(2026, 1, 1), date(2026, 6, 30)]],
			],
		)
		# Only names: a full order of every one of them would fill the memory of the worker
		self.assertEqual(args["fields"], ["name"])
		self.assertEqual((args["order_by"], args["servers"], args["strict"]), ("name asc", [SERVER], True))

	def test_an_open_end_of_the_range_is_left_open(self):
		self.assertEqual(get_date_filters(None, None), [])
		self.assertEqual(
			get_date_filters("2026-01-01", None),
			[["WooCommerce Order", "date_created", ">", date(2026, 1, 1)]],
		)
		# The last day included, as between takes it
		self.assertEqual(
			get_date_filters(None, "2026-06-30"),
			[["WooCommerce Order", "date_created", "<", date(2026, 7, 1)]],
		)

	def test_orders_that_have_a_sales_order_are_left_out(self):
		records = [{"name": order_name(i)} for i in (1, 2, 3)]
		with (
			patch.object(WooCommerceOrder, "get_list_of_records", return_value=records),
			patch.object(frappe, "get_all", return_value=["2"]) as get_all,
		):
			self.assertEqual(get_orders_to_import(make_import()), [order_name(1), order_name(3)])

		# A cancelled one counts too, so no docstatus filter
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"woocommerce_server": SERVER, "woocommerce_id": ["in", ["1", "2", "3"]]},
		)

	def test_orders_are_listed_with_the_fields_asked_for(self):
		records = [{"name": order_name(1), "total": 5.0}, {"name": order_name(2), "total": 7.0}]
		with (
			patch.object(WooCommerceOrder, "get_list_of_records", return_value=records) as get_list,
			patch.object(frappe, "get_all", return_value=["2"]),
		):
			self.assertEqual(list_orders_to_import(make_import(), ["name", "total"]), [records[0]])
		self.assertEqual(get_list.call_args.args[0]["fields"], ["name", "total"])

	def test_a_list_that_reaches_the_limit_says_so(self):
		with (
			patch.object(order_import, "MAX_ORDERS_TO_IMPORT", 2),
			patch.object(
				WooCommerceOrder,
				"get_list_of_records",
				return_value=[{"name": order_name(1)}, {"name": order_name(2)}],
			),
			patch.object(frappe, "get_all", return_value=[]),
			patch.object(frappe, "msgprint") as msgprint,
		):
			get_orders_to_import(make_import())
		msgprint.assert_called_once()


class ImportCase(IntegrationTestCase):
	"""
	A saved import of a shop, rolled back after each test
	"""

	def setUp(self):
		frappe.db.savepoint("test_order_import")
		self.addCleanup(frappe.db.rollback, save_point="test_order_import")
		if not frappe.db.exists("WooCommerce Server", SERVER):
			frappe.get_doc(
				{
					"doctype": "WooCommerce Server",
					"name": SERVER,
					"woocommerce_server_url": f"https://{SERVER}",
					"enabled": 1,
				}
			).db_insert()
		self.doc = frappe.get_doc(
			{"doctype": "WooCommerce Order Import", "woocommerce_server": SERVER}
		).insert(ignore_permissions=True)


class TestImportJob(ImportCase):
	def run_import(self, results, names=None, listed=True):
		"""
		Run the import, each order giving its result from `results` (an exception is raised), and return
		what it did
		"""
		names = names if names is not None else list(results)

		def run(importer):
			result = results[importer.woocommerce_order_name]
			if isinstance(result, BaseException):
				raise result
			return result

		listing = (
			patch.object(order_import, "get_orders_to_import", return_value=names)
			if listed
			else patch.object(order_import, "get_orders_to_import", side_effect=ConnectionError("down"))
		)
		with (
			listing,
			patch.object(OrderImport, "run", autospec=True, side_effect=run),
			# A real commit or rollback would end the test's own transaction
			patch.object(frappe.db, "commit") as commit,
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe, "publish_realtime") as publish_realtime,
			patch.object(frappe, "msgprint") as msgprint,
			patch.object(frappe, "log_error") as log_error,
		):
			try:
				import_orders(self.doc.name)
			finally:
				self.doc.reload()
		return _dict(
			commit=commit,
			rollback=rollback,
			publish_realtime=publish_realtime,
			msgprint=msgprint,
			log_error=log_error,
		)

	def log(self) -> dict[str, tuple]:
		return {
			row.woocommerce_id: (row.result, row.message)
			for row in frappe.get_all(
				"WooCommerce Order Import Log",
				filters={"order_import": self.doc.name},
				fields=["woocommerce_id", "result", "message"],
			)
		}

	def test_each_order_is_logged_and_committed_in_turn(self):
		calls = self.run_import(
			{
				order_name(1): ImportResult(RESULT_PAID),
				order_name(2): ImportResult(RESULT_NEEDS_REVIEW, "No payment date"),
			}
		)

		self.assertEqual(
			self.log(), {"1": (RESULT_PAID, None), "2": (RESULT_NEEDS_REVIEW, "No payment date")}
		)
		# Its start, each order, its end
		self.assertEqual(calls.commit.call_count, 4)
		self.assertEqual(
			(self.doc.status, self.doc.paid, self.doc.needs_review, self.doc.skipped, self.doc.failed),
			("Success", 1, 1, 0, 0),
		)
		# Frappe sends events of its own for the log rows
		events = [
			call
			for call in calls.publish_realtime.call_args_list
			if call.args[0] == "woocommerce_order_import"
		]
		progress = [call.args[1] for call in events]
		self.assertEqual(progress[0], {"order_import": self.doc.name, "done": 1, "total": 2})
		self.assertEqual(progress[-1], {"order_import": self.doc.name, "finished": True, "status": "Success"})
		self.assertEqual(events[-1].kwargs, {"doctype": "WooCommerce Order Import", "docname": self.doc.name})

	def test_an_order_that_fails_is_rolled_back_and_the_next_one_still_runs(self):
		calls = self.run_import(
			{order_name(1): ValueError("<b>No</b> Fiscal Year"), order_name(2): ImportResult(RESULT_PAID)}
		)

		self.assertEqual(self.log(), {"1": (RESULT_FAILED, "No Fiscal Year"), "2": (RESULT_PAID, None)})
		# The failed order, and the end of the import, which drops what an order left behind
		self.assertEqual(calls.rollback.call_count, 2)
		self.assertEqual((self.doc.status, self.doc.failed), ("Partial Success", 1))

	def test_a_rerun_replaces_what_an_order_got_before(self):
		write_log(self.doc, make_importer(), ImportResult(RESULT_FAILED, "No Fiscal Year"))

		self.run_import({order_name(7): ImportResult(RESULT_PAID)})

		self.assertEqual(self.log(), {"7": (RESULT_PAID, None)})
		self.assertEqual((self.doc.paid, self.doc.failed), (1, 0))

	def test_a_shop_that_can_not_be_listed_ends_the_import(self):
		calls = self.run_import({}, listed=False)

		calls.log_error.assert_called_once()
		self.assertEqual(self.doc.status, "Error")
		self.assertEqual(calls.msgprint.call_args.kwargs["indicator"], "red")

	def test_a_timeout_ends_the_job_and_keeps_the_counts(self):
		with self.assertRaises(JobTimeoutException):
			self.run_import(
				{
					order_name(1): ImportResult(RESULT_PAID),
					order_name(2): JobTimeoutException("out of time"),
					order_name(3): ImportResult(RESULT_PAID),
				}
			)

		# Order 3 never ran, and order 2 isn't taken for a failure
		self.assertEqual(self.log(), {"1": (RESULT_PAID, None)})
		self.assertEqual((self.doc.status, self.doc.paid), ("Timed Out", 1))

	def test_the_status_push_is_held_off_while_the_import_runs(self):
		seen = []

		def run(importer):
			seen.append(frappe.flags.get("woocommerce_order_import"))
			return ImportResult(RESULT_PAID)

		with patch.object(order_import, "get_orders_to_import", return_value=[order_name(1)]):
			with (
				patch.object(OrderImport, "run", autospec=True, side_effect=run),
				patch.object(frappe.db, "commit"),
				patch.object(frappe.db, "rollback"),
				patch.object(frappe, "publish_realtime"),
				patch.object(frappe, "msgprint"),
			):
				import_orders(self.doc.name)

		self.assertEqual(seen, [True])
		self.assertFalse(frappe.flags.get("woocommerce_order_import"))

	def test_results_are_counted_from_the_log(self):
		for order_id, result in ((1, RESULT_PAID), (2, RESULT_PAID), (3, RESULT_SKIPPED)):
			importer = make_importer(wc_order=make_order(woocommerce_id=str(order_id)))
			importer.woocommerce_order_name = order_name(order_id)
			write_log(self.doc, importer, ImportResult(result))

		self.assertEqual(
			count_results(self.doc.name),
			{RESULT_PAID: 2, RESULT_NEEDS_REVIEW: 0, RESULT_SKIPPED: 1, RESULT_FAILED: 0},
		)

	def test_deleting_an_import_deletes_its_log(self):
		write_log(self.doc, make_importer(), ImportResult(RESULT_SKIPPED, "Not completed"))

		self.doc.delete(ignore_permissions=True)

		self.assertFalse(frappe.db.exists("WooCommerce Order Import Log", {"order_import": self.doc.name}))


class TestStartingAnImport(IntegrationTestCase):
	def start(self, deliver=0, enabled=1, price_list=None):
		doc = MagicMock(woocommerce_server=SERVER, deliver=deliver, serial_numbers=None)
		doc.name = "WC-ORD-IMP-00001"
		server = _dict(name=SERVER, enabled=enabled, price_list=price_list, sales_price_list=None)
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe, "get_cached_doc", return_value=server),
			patch.object(frappe, "has_permission", return_value=True) as has_permission,
			patch.object(frappe, "enqueue", return_value=object()) as enqueue,
		):
			started = start_order_import(doc.name)
		doc.check_permission.assert_called_once_with("write")
		return started, {call.args[:2] for call in has_permission.call_args_list}, enqueue

	def test_the_import_runs_once_at_a_time_per_shop(self):
		started, _checked, enqueue = self.start()

		self.assertTrue(started)
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(
			(kwargs["queue"], kwargs["job_id"], kwargs["deduplicate"], kwargs["order_import"]),
			("long", f"woocommerce_order_import::{SERVER}", True, "WC-ORD-IMP-00001"),
		)

	def test_the_user_must_be_able_to_make_what_the_job_makes(self):
		_started, checked, _enqueue = self.start()

		for doctype in ("Sales Order", "Sales Invoice", "Payment Entry"):
			self.assertIn((doctype, "create"), checked)
			self.assertIn((doctype, "submit"), checked)
		for doctype in ("Customer", "Address", "Contact", "Item"):
			self.assertIn((doctype, "create"), checked)
		self.assertNotIn(("Delivery Note", "create"), checked)
		self.assertNotIn(("Item Price", "create"), checked)

		_started, checked, _enqueue = self.start(deliver=1, price_list="Standard Selling")
		self.assertIn(("Delivery Note", "submit"), checked)
		self.assertIn(("Item Price", "create"), checked)

	def test_a_disabled_shop_is_not_imported(self):
		with self.assertRaises(frappe.ValidationError):
			self.start(enabled=0)


class TestImportingAnOrder(IntegrationTestCase):
	def run_steps(self, importer, sales_order=None):
		"""
		Import the order with its documents' steps stubbed out, and return the stubs
		"""
		made = sales_order or make_sales_order()

		def create_sales_order(self, actions):
			self.sales_order = made

		stubs = _dict()
		with (
			patch.object(OrderImport, "create_sales_order", autospec=True, side_effect=create_sales_order),
			patch.object(order_import, "get_mode_of_payment_account", return_value=ACCOUNT),
			patch.object(OrderImport, "make_invoice", return_value=_dict(name="ACC-SINV-7")) as make_invoice,
			patch.object(
				OrderImport, "make_payment_entry", return_value=_dict(name="ACC-PAY-7")
			) as make_payment_entry,
			patch.object(OrderImport, "deliver") as deliver,
			patch.object(OrderImport, "close_if_undelivered", return_value=True) as close,
		):
			importer.import_wc_order()
		stubs.update(
			make_invoice=make_invoice, make_payment_entry=make_payment_entry, deliver=deliver, close=close
		)
		return stubs

	def test_a_paid_order_gets_its_invoice_and_payment(self):
		importer = make_importer()
		stubs = self.run_steps(importer)

		self.assertEqual(
			importer.result, ImportResult(RESULT_PAID, None, "SAL-ORD-7", "ACC-SINV-7", "ACC-PAY-7")
		)
		method, account, paid_at = stubs.make_payment_entry.call_args.args[1:]
		self.assertEqual((method.mode_of_payment, account), ("Credit Card", ACCOUNT))
		# Dated when WooCommerce took it as paid, the invoice as its payment
		self.assertEqual(paid_at, datetime(2026, 10, 30, 14, 0))
		stubs.make_invoice.assert_called_once_with(paid_at)
		stubs.deliver.assert_not_called()
		stubs.close.assert_called_once()

	def test_an_import_that_delivers_delivers_after_the_invoice(self):
		order = []
		importer = make_importer(doc=make_import(deliver=1))
		with (
			patch.object(
				OrderImport,
				"create_sales_order",
				autospec=True,
				side_effect=lambda self, actions: setattr(self, "sales_order", make_sales_order()),
			),
			patch.object(order_import, "get_mode_of_payment_account", return_value=ACCOUNT),
			patch.object(
				OrderImport,
				"make_invoice",
				side_effect=lambda paid_at: order.append("invoice") or _dict(name="ACC-SINV-7"),
			),
			patch.object(
				OrderImport,
				"make_payment_entry",
				side_effect=lambda *args: order.append("payment") or _dict(name="ACC-PAY-7"),
			),
			patch.object(OrderImport, "deliver", side_effect=lambda: order.append("deliver")),
			patch.object(OrderImport, "close_if_undelivered", side_effect=lambda: order.append("close")),
		):
			importer.import_wc_order()

		self.assertEqual(order, ["invoice", "payment", "deliver", "close"])

	def test_orders_that_are_not_for_the_import_are_skipped_with_nothing_made(self):
		cases = {
			"gone": make_importer(wc_order={}),
			"has a Sales Order": make_importer(sales_order=make_sales_order()),
			"not completed": make_importer(wc_order=make_order(status="processing")),
			"method without a Mode of Payment": make_importer(wc_order=make_order(payment_method="cod")),
			"method not in the map": make_importer(wc_order=make_order(payment_method="paypal")),
		}
		for case, importer in cases.items():
			with (
				self.subTest(case),
				patch.object(OrderImport, "create_sales_order") as create_sales_order,
			):
				importer.import_wc_order()
				self.assertEqual(importer.result.result, RESULT_SKIPPED)
				create_sales_order.assert_not_called()

	def test_a_mode_of_payment_without_an_account_is_skipped(self):
		importer = make_importer()
		with (
			patch.object(order_import, "get_mode_of_payment_account", return_value=None),
			patch.object(OrderImport, "create_sales_order") as create_sales_order,
		):
			importer.import_wc_order()

		self.assertEqual(importer.result.result, RESULT_SKIPPED)
		self.assertIn("Credit Card", importer.result.message)
		create_sales_order.assert_not_called()

	def test_orders_a_person_must_invoice_are_imported_without_an_invoice(self):
		cases = {
			"flagged": (make_order(), make_sales_order(woocommerce_sync_issue="Totals differ")),
			"no payment date": (make_order(date_paid=None), make_sales_order()),
			"refunded in part": (make_order(refunds='[{"id": 3, "total": "-20.00"}]'), make_sales_order()),
			# Nothing paid, so no payment method is asked for
			"nothing to pay": (make_order(total="0.00", payment_method=""), make_sales_order(grand_total=0)),
		}
		for case, (wc_order, sales_order) in cases.items():
			with self.subTest(case):
				importer = make_importer(wc_order=wc_order)
				stubs = self.run_steps(importer, sales_order=sales_order)

				self.assertEqual(importer.result.result, RESULT_NEEDS_REVIEW)
				self.assertEqual(importer.result.sales_order, "SAL-ORD-7")
				stubs.make_invoice.assert_not_called()
				stubs.make_payment_entry.assert_not_called()
				# Flagged on the Sales Order, which a person looks at, and closed, so re-opened to invoice it
				flag = sales_order.db_set.call_args.args[1]
				self.assertIn("without an invoice", flag)
				self.assertIn("Re-open", flag)

	def test_a_method_paid_on_completion_needs_the_completion_date(self):
		doc = make_import()
		doc.payment_method_map[0].payment_date = PAYMENT_DATE_COMPLETED

		# Without a payment date it is imported, dated when it was completed
		importer = make_importer(doc=doc, wc_order=make_order(date_paid=None))
		stubs = self.run_steps(importer)
		self.assertEqual(importer.result.result, RESULT_PAID)
		stubs.make_invoice.assert_called_once_with(datetime(2026, 11, 2, 9, 15, 30))
		self.assertEqual(stubs.make_payment_entry.call_args.args[3], datetime(2026, 11, 2, 9, 15, 30))

		# Without a completion date a person dates it
		importer = make_importer(doc=doc, wc_order=make_order(date_completed=None))
		stubs = self.run_steps(importer)
		self.assertEqual(
			(importer.result.result, importer.result.message),
			(RESULT_NEEDS_REVIEW, "WooCommerce has no completion date for it"),
		)
		stubs.make_invoice.assert_not_called()

	def test_an_order_without_a_sales_order_fails(self):
		importer = make_importer()
		with (
			patch.object(order_import, "get_mode_of_payment_account", return_value=ACCOUNT),
			patch.object(OrderImport, "create_sales_order"),
			self.assertRaises(frappe.ValidationError),
		):
			importer.import_wc_order()

	def test_an_import_runs_while_the_order_sync_is_paused(self):
		importer = make_importer()
		with patch.object(OrderImport, "run_locked") as run_locked:
			importer.run()
		self.assertEqual(run_locked.call_args.args, (importer.import_wc_order,))
		self.assertEqual(run_locked.call_args.kwargs, {"while_paused": True})


class TestImportedDocuments(IntegrationTestCase):
	def test_the_invoice_is_dated_when_the_order_was_paid_and_submitted(self):
		importer = make_importer(sales_order=make_sales_order())
		invoice = MagicMock(customer="Jan", company=COMPANY, payment_terms_template="14 days")
		with (
			patch.object(order_import, "make_sales_invoice", return_value=invoice) as make_sales_invoice,
			patch.object(order_import, "get_due_date", return_value=date(2026, 11, 13)) as get_due_date,
		):
			self.assertIs(importer.make_invoice(datetime(2026, 10, 30, 14, 0)), invoice)

		make_sales_invoice.assert_called_once_with("SAL-ORD-7")
		self.assertEqual(
			(invoice.update_stock, invoice.set_posting_time, invoice.posting_date, invoice.posting_time),
			(0, 1, date(2026, 10, 30), "14:00:00"),
		)
		# Due from the day it was paid rather than today, which the template would refuse
		get_due_date.assert_called_once_with(
			date(2026, 10, 30), "Customer", "Jan", COMPANY, template_name="14 days"
		)
		self.assertEqual((invoice.due_date, invoice.payment_schedule), (date(2026, 11, 13), []))
		self.assertTrue(invoice.flags.created_by_sync)
		invoice.insert.assert_called_once()
		invoice.submit.assert_called_once()

	def make_payment(self, method, currencies=("PLN", "PLN"), **order_values):
		importer = make_importer(wc_order=make_order(**order_values), sales_order=make_sales_order())
		payment_entry = MagicMock(company=COMPANY)
		payment_entry.paid_from_account_currency, payment_entry.paid_to_account_currency = currencies
		invoice = _dict(name="ACC-SINV-7")
		with (
			patch.object(order_import, "get_payment_entry", return_value=payment_entry) as get_payment_entry,
			patch.object(frappe, "get_cached_value", return_value="PLN"),
		):
			self.assertIs(
				importer.make_payment_entry(invoice, method, ACCOUNT, importer.get_paid_at(method)),
				payment_entry,
			)
		return payment_entry, get_payment_entry

	def test_the_payment_is_booked_on_the_invoice_when_it_was_paid(self):
		method = make_import().payment_method_map[0]
		payment_entry, get_payment_entry = self.make_payment(method)

		get_payment_entry.assert_called_once_with(
			"Sales Invoice", "ACC-SINV-7", bank_account=ACCOUNT, reference_date=date(2026, 10, 30)
		)
		self.assertEqual(
			(
				payment_entry.posting_date,
				payment_entry.mode_of_payment,
				payment_entry.reference_no,
				payment_entry.bank_account,
			),
			(date(2026, 10, 30), "Credit Card", "ch_1", "Stripe Bank"),
		)
		payment_entry.set_bank_account_data.assert_called_once()
		# The company's own currency needs no rates of the day
		payment_entry.set_exchange_rate.assert_not_called()
		payment_entry.insert.assert_called_once()
		payment_entry.submit.assert_called_once()

	def test_a_method_paid_on_completion_is_booked_when_the_order_was_completed(self):
		# A transfer for a reserved product, or cash the courier collects, comes in after WooCommerce's paid date
		method = _dict(
			mode_of_payment="Wire Transfer", bank_account=None, payment_date=PAYMENT_DATE_COMPLETED
		)
		importer = make_importer(sales_order=make_sales_order())
		paid_at = importer.get_paid_at(method)
		self.assertEqual(paid_at, datetime(2026, 11, 2, 9, 15, 30))

		invoice = MagicMock(customer="Jan", company=COMPANY, payment_terms_template=None)
		with (
			patch.object(order_import, "make_sales_invoice", return_value=invoice),
			patch.object(order_import, "get_due_date", return_value=date(2026, 11, 2)) as get_due_date,
		):
			importer.make_invoice(paid_at)
		self.assertEqual((invoice.posting_date, invoice.posting_time), (date(2026, 11, 2), "09:15:30"))
		self.assertEqual(get_due_date.call_args.args[0], date(2026, 11, 2))

		payment_entry, get_payment_entry = self.make_payment(method)
		self.assertEqual(payment_entry.posting_date, date(2026, 11, 2))
		self.assertEqual(get_payment_entry.call_args.kwargs["reference_date"], date(2026, 11, 2))

	def test_a_payment_without_a_bank_account_carries_none(self):
		method = _dict(mode_of_payment="Cash", bank_account=None)
		payment_entry, _get = self.make_payment(method, transaction_id="", payment_method_title="x" * 200)

		# Not the company's default Bank Account, which get_payment_entry puts there
		self.assertEqual(
			(payment_entry.bank_account, payment_entry.bank, payment_entry.bank_account_no),
			(None, None, None),
		)
		payment_entry.set_bank_account_data.assert_not_called()
		self.assertEqual(payment_entry.reference_no, "x" * 140)

	def test_a_payment_in_another_currency_takes_the_rates_of_its_day(self):
		method = make_import().payment_method_map[0]
		payment_entry, _get = self.make_payment(method, currencies=("EUR", "PLN"))

		self.assertEqual((payment_entry.source_exchange_rate, payment_entry.target_exchange_rate), (0, 0))
		payment_entry.set_exchange_rate.assert_called_once_with(ref_doc=_dict(name="ACC-SINV-7"))
		payment_entry.set_amounts.assert_called_once()

	def test_the_delivery_is_dated_when_the_order_was_completed(self):
		importer = make_importer(sales_order=make_sales_order())
		delivery_note = MagicMock()
		with patch.object(order_import, "make_delivery_note", return_value=delivery_note):
			importer.deliver()

		self.assertEqual(
			(delivery_note.set_posting_time, delivery_note.posting_date, delivery_note.posting_time),
			(1, date(2026, 11, 2), "09:15:30"),
		)
		delivery_note.insert.assert_called_once()
		delivery_note.submit.assert_called_once()

	def test_an_undelivered_order_is_closed_and_its_reservations_cancelled(self):
		sales_order = make_sales_order()
		importer = make_importer(sales_order=sales_order)
		with patch.object(frappe.db, "exists", return_value=True):
			self.assertTrue(importer.close_if_undelivered())

		# Fresh, as ERPNext refuses to change the status of a stale copy
		sales_order.reload.assert_called_once()
		sales_order.update_status.assert_called_once_with("Closed")
		sales_order.cancel_stock_reservation_entries.assert_called_once_with(notify=False)

	def test_a_delivered_order_stays_open(self):
		sales_order = make_sales_order(per_delivered=100)
		importer = make_importer(sales_order=sales_order)

		self.assertFalse(importer.close_if_undelivered())
		sales_order.update_status.assert_not_called()


class TestOrderImportForm(IntegrationTestCase):
	def validate(self, doc, account=ACCOUNT, bank_account=None):
		with (
			patch.object(frappe, "get_cached_value", return_value=COMPANY),
			patch.object(woocommerce_order_import, "get_mode_of_payment_account", return_value=account),
			patch.object(frappe.db, "get_value", return_value=bank_account),
		):
			doc.validate()

	def bank_account(self, **values):
		return _dict(
			{"company": COMPANY, "is_company_account": 1, "disabled": 0, "account": ACCOUNT, **values}
		)

	def test_a_map_that_books_on_the_companys_accounts_is_valid(self):
		self.validate(make_import(), bank_account=self.bank_account())

	def test_a_payment_method_is_mapped_once(self):
		doc = make_import()
		doc.append("payment_method_map", {"woocommerce_payment_method": "stripe "})
		with self.assertRaises(frappe.ValidationError):
			self.validate(doc, bank_account=self.bank_account())

	def test_a_mode_of_payment_needs_an_account_of_the_company(self):
		with self.assertRaises(frappe.ValidationError):
			self.validate(make_import(), account=None)

	def test_a_bank_account_must_be_the_companys_on_the_mode_of_payments_account(self):
		for case, bank_account in {
			"missing": None,
			"not a company account": self.bank_account(is_company_account=0),
			"disabled": self.bank_account(disabled=1),
			"of another company": self.bank_account(company="Other"),
			"on another account": self.bank_account(account="Bank PKO - TIC"),
		}.items():
			with self.subTest(case), self.assertRaises(frappe.ValidationError):
				self.validate(make_import(), bank_account=bank_account)

	def test_a_bank_account_needs_a_mode_of_payment(self):
		doc = make_import()
		doc.payment_method_map[1].bank_account = "Stripe Bank"
		with self.assertRaises(frappe.ValidationError):
			self.validate(doc, bank_account=self.bank_account())

	def test_the_range_runs_forward(self):
		with self.assertRaises(frappe.ValidationError):
			self.validate(make_import(created_from="2026-06-30", created_to="2026-01-01"))

	def test_a_new_import_takes_the_map_of_the_shops_last_one(self):
		doc = frappe.get_doc({"doctype": "WooCommerce Order Import", "woocommerce_server": SERVER})
		last_import = make_import()
		last_import.payment_method_map[1].payment_date = PAYMENT_DATE_COMPLETED
		with (
			patch.object(frappe, "get_all", return_value=["WC-ORD-IMP-00001"]),
			patch.object(frappe, "get_doc", return_value=last_import),
		):
			doc.add_payment_methods()

		self.assertEqual(
			[
				(row.woocommerce_payment_method, row.mode_of_payment, row.bank_account, row.payment_date)
				for row in doc.payment_method_map
			],
			[
				("stripe", "Credit Card", "Stripe Bank", last_import.payment_method_map[0].payment_date),
				("cod", None, None, PAYMENT_DATE_COMPLETED),
			],
		)

	def test_the_first_import_of_a_shop_starts_with_its_gateways(self):
		doc = frappe.get_doc({"doctype": "WooCommerce Order Import", "woocommerce_server": SERVER})
		with (
			patch.object(frappe, "get_all", return_value=[]),
			patch.object(type(doc), "fetch_payment_methods", return_value=({"bacs": "Bank transfer"}, None)),
		):
			doc.add_payment_methods()

		self.assertEqual(
			[
				(row.woocommerce_payment_method, row.woocommerce_payment_method_title, row.mode_of_payment)
				for row in doc.payment_method_map
			],
			[("bacs", "Bank transfer", None)],
		)

	def test_missing_payment_methods_leave_the_mapped_ones(self):
		doc = make_import()
		with patch.object(
			type(doc),
			"fetch_payment_methods",
			return_value=({"stripe": "Card", "cod": "Cash on delivery", "bacs": ""}, None),
		):
			result = doc.get_missing_payment_method_rows()

		self.assertEqual(
			result,
			{
				"rows": [{"woocommerce_payment_method": "bacs", "woocommerce_payment_method_title": "bacs"}],
				"error": None,
			},
		)
		self.assertEqual(doc.get_payment_method_row("stripe").mode_of_payment, "Credit Card")
		self.assertIsNone(doc.get_payment_method_row("paypal"))


class TestCountingOrdersToImport(IntegrationTestCase):
	def count(self, records=None, error=None):
		with (
			patch.object(frappe, "get_doc", return_value=make_import()),
			patch.object(
				order_import, "list_orders_to_import", return_value=records, side_effect=error
			) as list_orders,
		):
			return count_orders_to_import("WC-ORD-IMP-00009"), list_orders

	def test_the_orders_it_would_skip_are_counted_by_payment_method(self):
		def order(order_id, method, title, total):
			return {
				"name": order_name(order_id),
				"payment_method": method,
				"payment_method_title": title,
				"total": total,
			}

		result, list_orders = self.count(
			[
				order(1, "stripe", "Card", 100.0),
				# A row without a Mode of Payment
				order(2, "cod", "Cash on delivery", 50.0),
				# Not in the map, under the title it had then and the one it has now
				order(3, "reserve", "Reserve a mower", 10.0),
				order(4, "reserve", "Reserve a robot", 10.0),
				# Made in wp-admin without one
				order(5, "", "", 20.0),
				# Nothing to pay, so no method is needed
				order(6, "", "", 0.0),
			]
		)

		self.assertEqual(
			result,
			{
				"total": 6,
				"skipped": [
					{"payment_method": "reserve", "title": "Reserve a robot", "orders": 2},
					{"payment_method": "cod", "title": "Cash on delivery", "orders": 1},
					{"payment_method": "", "title": None, "orders": 1},
				],
				"error": None,
			},
		)
		# Only what the count needs, as the import lists them
		self.assertEqual(
			list_orders.call_args.args[1], ["name", "payment_method", "payment_method_title", "total"]
		)

	def test_a_shop_that_can_not_be_read_is_said_rather_than_raised(self):
		with patch.object(frappe, "log_error") as log_error:
			result, _list_orders = self.count(error=ConnectionError("The shop is down"))

		self.assertEqual(result, {"total": None, "skipped": [], "error": "The shop is down"})
		log_error.assert_called_once()


class TestOrdersWaitingForAnImport(IntegrationTestCase):
	def test_completed_orders_without_a_sales_order_are_told_of_once_per_shop(self):
		records = [
			{"name": "a.example.com~1", "status": "completed", "number": "1001"},
			# It has a Sales Order
			{"name": "a.example.com~2", "status": "completed", "number": "1002"},
			# The sync makes it one
			{"name": "a.example.com~3", "status": "processing", "number": "1003"},
			# Named by its id without a number
			{"name": "a.example.com~4", "status": "wc-completed", "number": None},
			# Its shop is paused, and listed again when it is unpaused
			{"name": "b.example.com~5", "status": "completed", "number": "5"},
			# Its shop submits completed orders itself
			{"name": "c.example.com~6", "status": "completed", "number": "6"},
			# All its completed orders have a Sales Order
			{"name": "d.example.com~7", "status": "completed", "number": "7"},
		]
		rows = {"c.example.com": _dict(get_actions=lambda: frozenset({ACTION_SUBMIT}))}
		with (
			patch.object(
				order_import, "is_order_sync_paused", side_effect=lambda server: server == "b.example.com"
			),
			patch.object(
				frappe,
				"get_cached_doc",
				side_effect=lambda doctype, name: _dict(get_order_status_row=lambda status: rows.get(name)),
			),
			patch.object(
				order_import,
				"leave_out_imported",
				side_effect=lambda server, names: [
					name for name in names if name not in ("a.example.com~2", "d.example.com~7")
				],
			),
			patch.object(order_import, "send_orders_to_import_notification") as send,
		):
			notify_orders_to_import(records)

		self.assertEqual([call.args for call in send.call_args_list], [("a.example.com", ["1001", "4"])])

	def test_nothing_is_said_when_nothing_waits(self):
		with patch.object(order_import, "send_orders_to_import_notification") as send:
			notify_orders_to_import([{"name": "a.example.com~1", "status": "processing"}])
		send.assert_not_called()

	def test_the_notification_leads_to_the_shops_imports_and_sends_no_email(self):
		with (
			patch.object(order_import, "get_notified_users", return_value=["admin@example.com"]),
			patch.object(order_import, "enqueue_create_notification") as enqueue,
		):
			send_orders_to_import_notification("a.example.com", [str(number) for number in range(1, 23)])

		users, notification = enqueue.call_args.args
		self.assertEqual(users, ["admin@example.com"])
		# Frappe sends an Alert by no email, also to the user it comes from, Administrator in a job
		self.assertEqual(notification["type"], "Alert")
		self.assertEqual(
			notification["link"], "/app/woocommerce-order-import?woocommerce_server=a.example.com"
		)
		self.assertEqual(
			(notification["document_type"], notification["document_name"]),
			("WooCommerce Server", "a.example.com"),
		)
		# The first 20 are named, the rest counted
		self.assertIn("#1, #2", notification["subject"])
		self.assertIn("#20 and 2 more", notification["subject"])
		self.assertNotIn("#21", notification["subject"])

	def test_the_system_managers_are_told_administrator_among_them(self):
		# Administrator is left out of Frappe's lookup by role, but is the only one on a site one person set up
		self.assertIn(frappe.db.get_value("User", "Administrator", "email"), get_notified_users())
