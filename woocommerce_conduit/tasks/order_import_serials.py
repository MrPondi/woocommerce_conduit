"""
The serial numbers a WooCommerce Order Import puts on its Delivery Notes, from a CSV attached to the import.
ERPNext then records each serial as delivered to the order's Customer, and starts its warranty on the day of
the delivery.

The file has one strict schema, and the guide at `public/order_import/serial_numbers_guide.md` tells an LLM how
to write the script that turns a company's earlier records into it, so this only checks and never guesses.
"""

import csv
import io
from collections import defaultdict
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, formatdate, getdate

HEADER = ["order_number", "serial_no"]
# Serial Nos and Sales Orders are looked up this many at a time
LOOKUP_BATCH_SIZE = 1000
# The most problems one message lists. The rest are counted.
MAX_LISTED_PROBLEMS = 50


@dataclass(frozen=True)
class SerialRow:
	line: int
	order_number: str
	serial_no: str


def parse_serial_file(content: bytes | str) -> tuple[list[SerialRow], list[str]]:
	"""
	The rows of a serial number file, and what is wrong with it: the header exactly `order_number,serial_no`,
	then one serial per row, both cells filled, no serial twice
	"""
	if isinstance(content, bytes):
		try:
			# Excel's "CSV UTF-8" starts the file with a byte-order mark
			content = content.decode("utf-8-sig")
		except UnicodeDecodeError:
			return [], [_("The file isn't UTF-8 text. Save it as CSV UTF-8.")]
	content = content.removeprefix("﻿")

	rows: list[SerialRow] = []
	errors: list[str] = []
	reader = csv.reader(io.StringIO(content), strict=True)
	try:
		header = next(reader, None)
		if [cstr(cell).strip() for cell in header or []] != HEADER:
			return [], [_("The first row must be exactly: {0}").format(",".join(HEADER))]

		lines = {}
		for cells in reader:
			line = reader.line_num
			if not cells:
				errors.append(_("Row {0} is empty").format(line))
				continue
			if len(cells) != len(HEADER):
				errors.append(_("Row {0} has {1} cells instead of 2").format(line, len(cells)))
				continue
			order_number, serial_no = (cell.strip() for cell in cells)
			if not (order_number and serial_no):
				errors.append(_("Row {0} has an empty cell").format(line))
				continue
			if order_number.startswith("#"):
				errors.append(_("Row {0}: write the order number without #").format(line))
				continue
			if serial_no in lines:
				errors.append(
					_("Row {0}: serial number {1} is on row {2} already").format(
						line, serial_no, lines[serial_no]
					)
				)
				continue
			lines[serial_no] = line
			rows.append(SerialRow(line, order_number, serial_no))
	except csv.Error as error:
		errors.append(_("Row {0} isn't valid CSV: {1}").format(reader.line_num, str(error)))

	if not rows and not errors:
		errors.append(_("The file has no serial numbers"))
	return rows, errors


def read_file(file_url: str) -> bytes | str:
	"""
	The content of an attached file
	"""
	file_name = frappe.get_all("File", filters={"file_url": file_url}, pluck="name", limit=1)
	if not file_name:
		frappe.throw(_("The serial number file {0} is gone. Attach it again.").format(file_url))
	return frappe.get_doc("File", file_name[0]).get_content()


def check_serial_numbers(order_import) -> dict[str, list[str]]:
	"""
	The serials of the import's file by order number, for the orders it hasn't imported yet. Every serial must
	be Active in the warehouse the shop's orders ship from, of the shop's company. Throws when anything is
	wrong, listing every problem, so a file is never half used. Writes the file's summary on the import.
	"""
	if not order_import.serial_numbers:
		order_import.serial_number_summary = None
		return {}

	rows, errors = parse_serial_file(read_file(order_import.serial_numbers))
	if not order_import.deliver:
		errors.insert(
			0, _("Serial numbers go on Delivery Notes: tick Make Delivery Notes, or remove the file")
		)

	imported = get_imported_order_numbers(order_import.woocommerce_server, {row.order_number for row in rows})
	rows_to_import = [row for row in rows if row.order_number not in imported]
	server = frappe.get_cached_doc("WooCommerce Server", order_import.woocommerce_server)
	errors.extend(check_serials_on_site(rows_to_import, server.warehouse, server.company))
	if errors:
		throw_problems(errors)

	serials: dict[str, list[str]] = defaultdict(list)
	for row in rows_to_import:
		serials[row.order_number].append(row.serial_no)
	order_import.serial_number_summary = _("{0} serial numbers for {1} orders").format(
		len(rows_to_import), len(serials)
	)
	if left_out := len(rows) - len(rows_to_import):
		order_import.serial_number_summary += "; " + _(
			"{0} rows left out, as their orders are imported already"
		).format(left_out)
	return dict(serials)


def check_serials_on_site(rows: list[SerialRow], warehouse: str | None, company: str | None) -> list[str]:
	"""
	What is wrong with the serials of these rows on the site
	"""
	found = {}
	names = [row.serial_no for row in rows]
	for start in range(0, len(names), LOOKUP_BATCH_SIZE):
		for serial in frappe.get_all(
			"Serial No",
			filters={"name": ["in", names[start : start + LOOKUP_BATCH_SIZE]]},
			fields=["name", "status", "warehouse", "company"],
		):
			# The database finds a serial whatever its case, the import wants it spelled as the site has it
			found[serial.name.casefold()] = serial

	errors = []
	for row in rows:
		serial = found.get(row.serial_no.casefold())
		if not serial:
			errors.append(_("Row {0}: there is no Serial No {1}").format(row.line, row.serial_no))
		elif serial.name != row.serial_no:
			errors.append(
				_("Row {0}: the site spells {1} as {2}").format(row.line, row.serial_no, serial.name)
			)
		elif serial.status != "Active":
			errors.append(
				_("Row {0}: Serial No {1} is {2}, not Active").format(row.line, serial.name, serial.status)
			)
		elif serial.warehouse != warehouse:
			errors.append(
				_("Row {0}: Serial No {1} is in {2}, not in {3}, which the shop's orders ship from").format(
					row.line, serial.name, serial.warehouse or _("no warehouse"), warehouse
				)
			)
		elif serial.company != company:
			errors.append(
				_("Row {0}: Serial No {1} is of company {2}, not {3}").format(
					row.line, serial.name, serial.company, company
				)
			)
	return errors


def get_imported_order_numbers(woocommerce_server: str, order_numbers: set[str]) -> set[str]:
	"""
	The order numbers that have a Sales Order of the shop, which keeps them in its Customer's Purchase Order
	"""
	numbers = list(order_numbers)
	imported = set()
	for start in range(0, len(numbers), LOOKUP_BATCH_SIZE):
		imported.update(
			frappe.get_all(
				"Sales Order",
				filters={
					"woocommerce_server": woocommerce_server,
					"po_no": ["in", numbers[start : start + LOOKUP_BATCH_SIZE]],
				},
				pluck="po_no",
			)
		)
	return imported


def throw_problems(errors: list[str]) -> None:
	listed = [frappe.utils.escape_html(error) for error in errors[:MAX_LISTED_PROBLEMS]]
	if len(errors) > MAX_LISTED_PROBLEMS:
		listed.append(_("and {0} more").format(len(errors) - MAX_LISTED_PROBLEMS))
	frappe.throw("<br>".join(listed), title=_("The serial number file can't be used"))


def assign_serial_numbers(delivery_note, serial_nos: list[str]) -> None:
	"""
	Give each line of a serialized Item of a Delivery Note its serials from the file, handed out over the Item's
	lines in turn. ERPNext makes the Serial and Batch Bundle from the line's `serial_no`. Throws when they don't
	add up, so an order is never delivered with the wrong serials, or with ones ERPNext picks itself.
	"""
	delivered_on = getdate(delivery_note.posting_date)
	details = get_serial_details(serial_nos)
	by_item: dict[str, list[str]] = defaultdict(list)
	for serial_no in serial_nos:
		detail = details.get(serial_no)
		if not detail:
			frappe.throw(_("Serial No {0} is no longer on the site").format(serial_no))
		# A back-dated delivery can't take a serial that came into stock after it
		if detail.posting_date and getdate(detail.posting_date) > delivered_on:
			frappe.throw(
				_("Serial No {0} came into stock on {1}, after the order was completed on {2}").format(
					serial_no, formatdate(detail.posting_date), formatdate(delivered_on)
				)
			)
		by_item[detail.item_code].append(serial_no)

	items = {row.item_code for row in delivery_note.items}
	for item_code, serials in by_item.items():
		if item_code not in items:
			frappe.throw(
				_("Serial Nos {0} are of Item {1}, which isn't on the order").format(
					", ".join(serials), item_code
				)
			)

	for item_code in sorted(items):
		if not frappe.get_cached_value("Item", item_code, "has_serial_no"):
			continue
		lines = [row for row in delivery_note.items if row.item_code == item_code]
		units = [get_stock_units(row) for row in lines]
		serials = by_item.get(item_code, [])
		if len(serials) != sum(units):
			frappe.throw(
				_("Item {0} needs {1} serial numbers on the order, the file has {2}").format(
					item_code, sum(units), len(serials)
				)
			)
		start = 0
		for row, count in zip(lines, units, strict=True):
			row.serial_no = "\n".join(serials[start : start + count])
			start += count


def get_serial_details(serial_nos: list[str]) -> dict:
	"""
	The Item of each serial and when it first came into stock
	"""
	details = {}
	for start in range(0, len(serial_nos), LOOKUP_BATCH_SIZE):
		for serial in frappe.get_all(
			"Serial No",
			filters={"name": ["in", serial_nos[start : start + LOOKUP_BATCH_SIZE]]},
			fields=["name", "item_code", "posting_date"],
		):
			details[serial.name] = serial
	return details


def get_stock_units(row) -> int:
	"""
	The units of a Delivery Note line in its Item's stock UOM, one serial each
	"""
	return cint(flt(row.get("stock_qty")) or flt(row.qty) * flt(row.get("conversion_factor") or 1))
