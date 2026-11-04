# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks import order_import, order_import_serials
from woocommerce_conduit.tasks.order_import import ImportResult, OrderImport, import_orders
from woocommerce_conduit.tasks.order_import_serials import (
	SerialRow,
	assign_serial_numbers,
	check_serial_numbers,
	parse_serial_file,
)
from woocommerce_conduit.tasks.test_order_import import (
	SERVER,
	ImportCase,
	make_import,
	make_importer,
	make_sales_order,
	order_name,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order_import import woocommerce_order_import
from woocommerce_conduit.woocommerce.doctype.woocommerce_order_import_log.woocommerce_order_import_log import (
	RESULT_PAID,
)

WAREHOUSE = "Stores - TIC"
COMPANY = "_Test Import Company"


def serial(name, **values):
	"""
	A Serial No as the site has it, Active in the shop's warehouse
	"""
	return _dict(
		{
			"name": name,
			"item_code": "MOWER",
			"status": "Active",
			"warehouse": WAREHOUSE,
			"company": COMPANY,
			"posting_date": date(2026, 10, 1),
			**values,
		}
	)


class TestParsingTheFile(IntegrationTestCase):
	def test_a_file_of_the_schema_is_read_row_by_row(self):
		rows, errors = parse_serial_file(b'order_number,serial_no\n1234,SN-1\n1234, SN-2 \r\n1240,"SN,3"\n')

		self.assertEqual(errors, [])
		self.assertEqual(
			rows,
			[SerialRow(2, "1234", "SN-1"), SerialRow(3, "1234", "SN-2"), SerialRow(4, "1240", "SN,3")],
		)

	def test_the_byte_order_mark_of_excel_is_accepted(self):
		rows, errors = parse_serial_file("﻿order_number,serial_no\n1234,SN-1\n".encode())
		self.assertEqual((rows, errors), ([SerialRow(2, "1234", "SN-1")], []))

	def test_the_header_must_be_exact(self):
		for header in ("order,serial", "serial_no,order_number", "order_number,serial_no,item", ""):
			with self.subTest(header):
				rows, errors = parse_serial_file(f"{header}\n1234,SN-1\n")
				self.assertEqual(rows, [])
				self.assertEqual(len(errors), 1)

	def test_every_row_that_breaks_the_schema_is_listed(self):
		content = "\n".join(
			[
				"order_number,serial_no",
				"1234,SN-1",
				"1234,SN-2,extra",
				"1235",
				"1236,  ",
				"#1237,SN-3",
				"",
				"1238,SN-1",
				"1239,SN-4",
			]
		)
		rows, errors = parse_serial_file(content)

		self.assertEqual(rows, [SerialRow(2, "1234", "SN-1"), SerialRow(9, "1239", "SN-4")])
		self.assertEqual(len(errors), 6)
		for line, error in zip((3, 4, 5, 6, 7, 8), errors, strict=True):
			self.assertIn(f"Row {line}", error)
		# The row a repeated serial was first on
		self.assertIn("row 2", errors[-1])

	def test_a_file_that_is_not_utf8_is_refused(self):
		rows, errors = parse_serial_file("order_number,serial_no\n1234,Kosiarka-ż\n".encode("cp1250"))
		self.assertEqual(rows, [])
		self.assertIn("UTF-8", errors[0])

	def test_broken_quoting_is_refused(self):
		_rows, errors = parse_serial_file('order_number,serial_no\n1234,SN-1\n1235,"SN-2\n')
		self.assertEqual(len(errors), 1)
		self.assertIn("isn't valid CSV", errors[0])

	def test_a_file_without_serials_is_refused(self):
		self.assertEqual(len(parse_serial_file("order_number,serial_no\n")[1]), 1)


class TestCheckingTheFile(IntegrationTestCase):
	def check(self, content, serials=(), imported=(), deliver=1):
		doc = _dict(
			serial_numbers="/private/files/serials.csv",
			deliver=deliver,
			woocommerce_server=SERVER,
			serial_number_summary=None,
		)

		def get_all(doctype, **kwargs):
			if doctype == "Sales Order":
				return [number for number in imported if number in kwargs["filters"]["po_no"][1]]
			return [
				row
				for row in serials
				if row.name.casefold() in {n.casefold() for n in kwargs["filters"]["name"][1]}
			]

		with (
			patch.object(order_import_serials, "read_file", return_value=content),
			patch.object(frappe, "get_cached_doc", return_value=_dict(warehouse=WAREHOUSE, company=COMPANY)),
			patch.object(frappe, "get_all", side_effect=get_all) as get_all_mock,
		):
			return check_serial_numbers(doc), doc, get_all_mock

	def test_a_good_file_gives_the_serials_of_each_order(self):
		serials, doc, _get_all = self.check(
			"order_number,serial_no\n1234,SN-1\n1234,SN-2\n1240,SN-3\n",
			serials=[serial("SN-1"), serial("SN-2"), serial("SN-3")],
		)

		self.assertEqual(serials, {"1234": ["SN-1", "SN-2"], "1240": ["SN-3"]})
		self.assertEqual(doc.serial_number_summary, "3 serial numbers for 2 orders")

	def test_rows_of_orders_imported_already_are_left_out(self):
		# SN-1 was delivered by the first run of the import, so it is no longer Active
		serials, doc, get_all = self.check(
			"order_number,serial_no\n1234,SN-1\n1240,SN-3\n",
			serials=[serial("SN-1", status="Delivered"), serial("SN-3")],
			imported=["1234"],
		)

		self.assertEqual(serials, {"1240": ["SN-3"]})
		self.assertIn("1 rows left out", doc.serial_number_summary)
		looked_up = get_all.call_args_list[-1].kwargs["filters"]["name"][1]
		self.assertEqual(looked_up, ["SN-3"])

	def test_every_serial_that_can_not_be_delivered_is_listed_at_once(self):
		content = "order_number,serial_no\n" + "\n".join(
			f"1234,{name}" for name in ("SN-1", "sn-2", "SN-3", "SN-4", "SN-5")
		)
		with self.assertRaises(frappe.ValidationError) as raised:
			self.check(
				content,
				serials=[
					serial("SN-2"),
					serial("SN-3", status="Delivered"),
					serial("SN-4", warehouse="Returns - TIC"),
					serial("SN-5", company="Other"),
				],
			)

		message = str(raised.exception)
		# Unknown, spelled otherwise, delivered, elsewhere, of another company
		for line in (2, 3, 4, 5, 6):
			self.assertIn(f"Row {line}", message)

	def test_a_file_needs_delivery_notes_to_go_on(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self.check("order_number,serial_no\n1234,SN-1\n", serials=[serial("SN-1")], deliver=0)
		self.assertIn("Make Delivery Notes", str(raised.exception))

	def test_no_file_gives_no_serials(self):
		doc = _dict(serial_numbers=None, serial_number_summary="old")
		self.assertEqual(check_serial_numbers(doc), {})
		self.assertIsNone(doc.serial_number_summary)


class TestAssigningSerials(IntegrationTestCase):
	def assign(self, lines, serial_nos, details, serialized=("MOWER",)):
		# Not a _dict, whose items is the dict's own method
		delivery_note = SimpleNamespace(
			posting_date=date(2026, 11, 2),
			items=[
				_dict(item_code=item_code, qty=qty, stock_qty=qty, conversion_factor=1, serial_no=None)
				for item_code, qty in lines
			],
		)
		with (
			patch.object(frappe, "get_all", return_value=details),
			patch.object(
				frappe, "get_cached_value", side_effect=lambda doctype, name, field: name in serialized
			),
		):
			assign_serial_numbers(delivery_note, serial_nos)
		return delivery_note

	def test_an_items_serials_are_handed_out_over_its_lines(self):
		delivery_note = self.assign(
			[("MOWER", 2), ("BLADE", 3), ("MOWER", 1)],
			["SN-1", "SN-2", "SN-3"],
			[serial("SN-1"), serial("SN-2"), serial("SN-3")],
		)

		self.assertEqual([row.serial_no for row in delivery_note.items], ["SN-1\nSN-2", None, "SN-3"])

	def test_serials_that_do_not_add_up_fail_the_order(self):
		cases = {
			"one too few": ([("MOWER", 2)], ["SN-1"], [serial("SN-1")]),
			"of an Item not on the order": (
				[("MOWER", 1)],
				["SN-1", "SN-2"],
				[serial("SN-1"), serial("SN-2", item_code="TRIMMER")],
			),
			"none for a serialized line": ([("MOWER", 1)], [], []),
			"no longer on the site": ([("MOWER", 1)], ["SN-1"], []),
			"in stock only after the order was completed": (
				[("MOWER", 1)],
				["SN-1"],
				[serial("SN-1", posting_date=date(2026, 11, 3))],
			),
		}
		for case, (lines, serial_nos, details) in cases.items():
			with self.subTest(case), self.assertRaises(frappe.ValidationError):
				self.assign(lines, serial_nos, details)

	def test_an_order_without_serialized_items_needs_no_serials(self):
		delivery_note = self.assign([("BLADE", 3)], [], [])
		self.assertIsNone(delivery_note.items[0].serial_no)


class TestSerialsInTheImport(ImportCase):
	def test_the_delivery_note_gets_the_orders_serials(self):
		importer = make_importer(sales_order=make_sales_order())
		importer.serials = {"1007": ["SN-1", "SN-2"], "2000": ["SN-3"]}
		delivery_note = MagicMock()
		with (
			patch.object(order_import, "make_delivery_note", return_value=delivery_note),
			patch.object(order_import, "assign_serial_numbers") as assign,
		):
			importer.deliver()

		assign.assert_called_once_with(delivery_note, ["SN-1", "SN-2"])
		# Before the insert, which makes the bundles
		delivery_note.insert.assert_called_once()

	def test_the_file_is_checked_when_it_changes(self):
		doc = make_import(serial_numbers="/private/files/serials.csv", deliver=1)
		cases = {"new": (True, False), "changed": (False, True), "unchanged": (False, False)}
		for case, (is_new, changed) in cases.items():
			with (
				self.subTest(case),
				patch.object(type(doc), "is_new", return_value=is_new),
				patch.object(type(doc), "has_value_changed", return_value=changed),
				patch.object(woocommerce_order_import, "check_serial_numbers") as check,
			):
				doc.validate_serial_numbers()
				self.assertEqual(check.called, case != "unchanged")

	def test_starting_an_import_checks_the_file_again(self):
		doc = MagicMock(woocommerce_server=SERVER, deliver=1)
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe, "get_cached_doc", return_value=_dict(enabled=1)),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(order_import, "check_serial_numbers", side_effect=frappe.ValidationError),
			patch.object(frappe, "enqueue") as enqueue,
			self.assertRaises(frappe.ValidationError),
		):
			order_import.start_order_import("WC-ORD-IMP-00001")
		enqueue.assert_not_called()

	def test_the_end_of_an_import_counts_the_serials_it_did_not_use(self):
		with (
			patch.object(
				order_import,
				"check_serial_numbers",
				return_value={"1007": ["SN-1"], "2000": ["SN-2", "SN-3"]},
			),
			patch.object(order_import, "get_orders_to_import", return_value=[order_name(7)]),
			patch.object(
				order_import, "import_order", return_value=(ImportResult(RESULT_PAID), "1007")
			) as import_order,
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
			patch.object(frappe, "publish_realtime"),
			patch.object(frappe, "msgprint") as msgprint,
		):
			import_orders(self.doc.name)

		self.assertEqual(import_order.call_args.args[2], {"1007": ["SN-1"], "2000": ["SN-2", "SN-3"]})
		self.assertIn("2 serial numbers", msgprint.call_args.args[0])

	def test_an_importer_knows_its_orders_number(self):
		self.assertEqual(make_importer().order_number, "1007")
		self.assertIsNone(OrderImport(make_import(), order_name(7)).order_number)
