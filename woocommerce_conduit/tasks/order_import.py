"""
The WooCommerce Order Import: the completed orders of a shop that have no Sales Order yet, each made with its
Sales Order and, when its payment can be booked, its Sales Invoice and Payment Entry, submitted together.

An invoice is only made when its payment is processed, so the order sync makes neither. This is the one place
the app books them, for orders that were done before ERPNext saw them: the shop's old orders, and those
completed while the sync wasn't watching.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.party import get_due_date
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note, make_sales_invoice
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.query_builder.functions import Count
from frappe.utils import add_days, cstr, escape_html, flt, get_datetime, getdate, strip_html
from frappe.utils.user import get_users_with_role
from rq.timeouts import JobTimeoutException

from woocommerce_conduit.tasks.order_import_serials import assign_serial_numbers, check_serial_numbers
from woocommerce_conduit.tasks.order_lines import get_json_lines
from woocommerce_conduit.tasks.sync_sales_orders import SynchroniseSalesOrder, is_order_sync_paused
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
from woocommerce_conduit.woocommerce.doctype.woocommerce_order_import.woocommerce_order_import import (
	get_import_job_id,
	get_mode_of_payment_account,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order_import_log.woocommerce_order_import_log import (
	RESULT_FAILED,
	RESULT_NEEDS_REVIEW,
	RESULT_PAID,
	RESULT_SKIPPED,
	RETRIED_RESULTS,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order_import_payment_method.woocommerce_order_import_payment_method import (
	PAYMENT_DATE_COMPLETED,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_SUBMIT,
	normalise_status,
)
from woocommerce_conduit.woocommerce.woocommerce_api import get_domain_and_id_from_woocommerce_record_name

# The WooCommerce status of the orders an import takes: paid and done in the shop
IMPORTED_STATUS = "completed"
ORDER_IMPORT_EVENT = "woocommerce_order_import"
# An import that stops is carried on by starting it again, as it leaves out the orders it imported
ORDER_IMPORT_TIMEOUT = 2 * 60 * 60
# The most orders one import lists, read 100 a request
MAX_ORDERS_TO_IMPORT = 100_000
# Sales Orders are looked up for this many order ids at a time
LOOKUP_BATCH_SIZE = 1000
# The longest reference a Payment Entry takes
REFERENCE_NO_LENGTH = 140
# The order numbers a notification of orders waiting for an import names, the rest are counted
MAX_ORDERS_NAMED = 20
# Shown in the desk's bell and sent by no email (Frappe's notification_skip_email_types), also to its own actor
NOTIFICATION_TYPE = "Alert"

STATUS_IN_PROGRESS = "In Progress"
STATUS_SUCCESS = "Success"
STATUS_PARTIAL_SUCCESS = "Partial Success"
STATUS_TIMED_OUT = "Timed Out"
STATUS_ERROR = "Error"

# The count field of the import for each result
COUNT_FIELDS = {
	RESULT_PAID: "paid",
	RESULT_NEEDS_REVIEW: "needs_review",
	RESULT_SKIPPED: "skipped",
	RESULT_FAILED: "failed",
}


@dataclass
class ImportResult:
	"""
	What the import did with one order
	"""

	result: str
	message: str | None = None
	sales_order: str | None = None
	sales_invoice: str | None = None
	payment_entry: str | None = None


@frappe.whitelist(methods=["POST"])
def start_order_import(order_import: str) -> bool:
	"""
	Import the orders of a WooCommerce Order Import in the background. False when an import of its shop is
	running or waiting already.
	"""
	doc = frappe.get_doc("WooCommerce Order Import", order_import)
	doc.check_permission("write")
	server = frappe.get_cached_doc("WooCommerce Server", doc.woocommerce_server)
	if not server.enabled:
		frappe.throw(_("Enable WooCommerce Server {0} first").format(frappe.bold(server.name)))

	# The job makes the documents as the user who starts it, and would fail at every order
	documents = ["Sales Order", "Sales Invoice", "Payment Entry"]
	if doc.deliver:
		documents.append("Delivery Note")
	for doctype in documents:
		frappe.has_permission(doctype, "create", throw=True)
		frappe.has_permission(doctype, "submit", throw=True)
	for doctype in ("Customer", "Address", "Contact", "Item"):
		frappe.has_permission(doctype, "create", throw=True)
	if server.price_list or server.sales_price_list:
		frappe.has_permission("Item Price", "create", throw=True)
	# The site's serials may have moved since the file was saved
	check_serial_numbers(doc)

	return enqueue_order_import(doc.name, doc.woocommerce_server)


def enqueue_order_import(order_import: str, woocommerce_server: str) -> bool:
	"""
	Import the orders of a WooCommerce Order Import, once at a time per shop. Also for bench.
	"""
	return bool(
		frappe.enqueue(
			import_orders,
			queue="long",
			timeout=ORDER_IMPORT_TIMEOUT,
			job_id=get_import_job_id(woocommerce_server),
			deduplicate=True,
			order_import=order_import,
		)
	)


def import_orders(order_import: str) -> None:
	"""
	Background job: import the orders of a WooCommerce Order Import one after the other. Each order is committed
	when it is done, and rolled back and logged when it fails, so an import that stops keeps what it made and
	is carried on by starting it again.
	"""
	doc = frappe.get_doc("WooCommerce Order Import", order_import)
	# ERPNext changes a Sales Order's status on a copy of its own, which the status push would send to the shop
	frappe.flags.woocommerce_order_import = True
	listed = timed_out = False
	unused_serials = 0
	try:
		doc.db_set("status", STATUS_IN_PROGRESS)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit

		try:
			serials = check_serial_numbers(doc)
			names = get_orders_to_import(doc)
		except JobTimeoutException:
			raise
		except Exception:
			frappe.log_error(
				"WooCommerce Order Import Error", f"{frappe.get_traceback()}\n\nOrder Import: {doc.name}"
			)
			return
		listed = True

		imported = set()
		for done, name in enumerate(names, start=1):
			result, order_number = import_order(doc, name, serials)
			if result.result in (RESULT_PAID, RESULT_NEEDS_REVIEW):
				imported.add(order_number)
			publish_import_progress(doc.name, done=done, total=len(names))
		unused_serials = sum(
			len(serial_nos) for number, serial_nos in serials.items() if number not in imported
		)
	except JobTimeoutException:
		timed_out = True
		raise
	finally:
		frappe.flags.woocommerce_order_import = False
		finish_import(doc, listed=listed, timed_out=timed_out, unused_serials=unused_serials)


def import_order(
	order_import, woocommerce_order_name: str, serials: dict[str, list[str]] | None = None
) -> tuple[ImportResult, str | None]:
	"""
	Import one order and log what was done with it, and return that with the order's number. The order is its
	own transaction: what it made is committed, or all of it rolled back.
	"""
	frappe.clear_messages()
	importer = OrderImport(order_import, woocommerce_order_name, serials)
	try:
		result = importer.run()
	except JobTimeoutException:
		raise
	except Exception as error:
		# Logged where it failed. The Error Log keeps it through the rollback.
		frappe.db.rollback()
		frappe.clear_messages()
		result = ImportResult(RESULT_FAILED, strip_html(cstr(error)) or error.__class__.__name__)

	write_log(order_import, importer, result)
	# A manual commit, as the docstring says why
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return result, importer.order_number


def write_log(order_import, importer: "OrderImport", result: ImportResult) -> None:
	"""
	Note what was done with the order. An order that was skipped or failed in an earlier run of the import has
	that row replaced, so the import counts each order once.
	"""
	woocommerce_id = cstr(get_domain_and_id_from_woocommerce_record_name(importer.woocommerce_order_name)[1])
	frappe.db.delete(
		"WooCommerce Order Import Log",
		{
			"order_import": order_import.name,
			"woocommerce_id": woocommerce_id,
			"result": ["in", RETRIED_RESULTS],
		},
	)
	frappe.get_doc(
		{
			"doctype": "WooCommerce Order Import Log",
			"order_import": order_import.name,
			"woocommerce_id": woocommerce_id,
			"order_number": importer.order_number,
			"result": result.result,
			"message": result.message,
			"sales_order": result.sales_order,
			"sales_invoice": result.sales_invoice,
			"payment_entry": result.payment_entry,
		}
	).insert(ignore_permissions=True)


def finish_import(order_import, listed: bool, timed_out: bool, unused_serials: int = 0) -> None:
	"""
	Note how the import went, from its log, and tell the user who started it
	"""
	# What an order had made when the job stopped wasn't committed
	frappe.db.rollback()
	counts = count_results(order_import.name)
	if timed_out:
		status = STATUS_TIMED_OUT
	elif not listed:
		status = STATUS_ERROR
	elif counts[RESULT_FAILED]:
		status = STATUS_PARTIAL_SUCCESS
	else:
		status = STATUS_SUCCESS

	order_import.db_set(
		{"status": status, **{COUNT_FIELDS[result]: count for result, count in counts.items()}}
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit

	publish_import_progress(order_import.name, finished=True, status=status)
	if not listed and not timed_out:
		message = _("The orders of {0} couldn't be listed, see the Error Log").format(
			frappe.bold(order_import.woocommerce_server)
		)
	else:
		message = _("{0}: {1} paid, {2} to review, {3} skipped, {4} failed").format(
			frappe.bold(order_import.name),
			counts[RESULT_PAID],
			counts[RESULT_NEEDS_REVIEW],
			counts[RESULT_SKIPPED],
			counts[RESULT_FAILED],
		)
		if timed_out:
			message += " " + _("It ran out of time. Start it again to import the rest.")
		elif unused_serials:
			message += " " + _(
				"{0} serial numbers of the file weren't used, as their orders weren't imported."
			).format(unused_serials)
	frappe.msgprint(
		message,
		indicator="green" if status == STATUS_SUCCESS else "orange" if listed else "red",
		realtime=True,
	)


def count_results(order_import: str) -> dict[str, int]:
	"""
	How many of the import's orders got each result
	"""
	log = frappe.qb.DocType("WooCommerce Order Import Log")
	rows = (
		frappe.qb.from_(log)
		.select(log.result, Count("*").as_("count"))
		.where(log.order_import == order_import)
		.groupby(log.result)
	).run(as_dict=True)
	counts = dict.fromkeys(COUNT_FIELDS, 0)
	counts.update({row.result: row.count for row in rows if row.result in counts})
	return counts


def publish_import_progress(order_import: str, **progress) -> None:
	"""
	Show the progress on the form of the import, which only its viewers get
	"""
	frappe.publish_realtime(
		ORDER_IMPORT_EVENT,
		{"order_import": order_import, **progress},
		doctype="WooCommerce Order Import",
		docname=order_import,
	)


def get_orders_to_import(order_import) -> list[str]:
	"""
	The names of the orders the import takes, and nothing else of them: a full order of every one of them would
	fill the memory of the worker
	"""
	return [record["name"] for record in list_orders_to_import(order_import, ["name"])]


def list_orders_to_import(order_import, fields: list[str]) -> list[dict]:
	"""
	The completed orders of the import's shop in its date range that have no Sales Order, with `fields`. Raises
	when the shop can't be read, so it isn't taken for one without orders.
	"""
	records = WooCommerceOrder.get_list_of_records(
		{
			"doctype": "WooCommerce Order",
			"filters": [
				["WooCommerce Order", "status", "=", IMPORTED_STATUS],
				*get_date_filters(order_import.created_from, order_import.created_to),
			],
			"fields": fields,
			# By id, so an order placed while the pages are read doesn't move the next page on
			"order_by": "name asc",
			"page_length": MAX_ORDERS_TO_IMPORT,
			"servers": [order_import.woocommerce_server],
			"strict": True,
		}
	)
	if len(records) >= MAX_ORDERS_TO_IMPORT:
		frappe.msgprint(
			_("Only the first {0} orders are imported. Import the rest with a narrower date range.").format(
				MAX_ORDERS_TO_IMPORT
			),
			indicator="orange",
			realtime=True,
		)
	waiting = set(leave_out_imported(order_import.woocommerce_server, [record["name"] for record in records]))
	return [record for record in records if record["name"] in waiting]


@frappe.whitelist()
def count_orders_to_import(order_import: str) -> dict:
	"""
	How many orders an import would take now, and those of them it would skip, as their payment method has no
	Mode of Payment, by method: for the form to say before it starts. It reads the shop as the import does, with
	only what the count needs. Why the shop couldn't be read is returned rather than raised, as an import can
	start without a count.
	"""
	doc = frappe.get_doc("WooCommerce Order Import", order_import)
	doc.check_permission("read")
	try:
		records = list_orders_to_import(doc, ["name", "payment_method", "payment_method_title", "total"])
	except Exception as error:
		frappe.log_error("WooCommerce Error", f"{frappe.get_traceback()}\n\nOrder Import: {doc.name}")
		return {"total": None, "skipped": [], "error": strip_html(cstr(error)) or error.__class__.__name__}

	skipped: dict[str, dict] = {}
	for record in records:
		if flt(record.get("total")) <= 0 or get_booking_row(doc, record):
			continue
		method = cstr(record.get("payment_method"))
		row = skipped.setdefault(method, {"payment_method": method, "title": None, "orders": 0})
		row["orders"] += 1
		# A method's title changes over time, and the orders come by id, so the last one is the newest
		row["title"] = cstr(record.get("payment_method_title")) or row["title"]
	return {
		"total": len(records),
		"skipped": sorted(skipped.values(), key=lambda row: -row["orders"]),
		"error": None,
	}


def get_booking_row(order_import, order):
	"""
	The row of the import's Payment Method Map that books an order's payment: that of its payment method, when it
	has a Mode of Payment. Without one an order with something to pay is skipped.
	"""
	row = order_import.get_payment_method_row(order.get("payment_method"))
	return row if row and row.mode_of_payment else None


def notify_orders_to_import(records: list[dict]) -> None:
	"""
	Tell the System Managers about the orders the order sync listed completed that have no Sales Order. The sync
	makes none for them, as an order it first sees completed is the import's, so they wait for one, and nothing
	else says so: an order completed while the sync was paused, entered in wp-admin as completed, or completed
	within the hour after its webhook failed. One notification per shop and run, naming the orders.
	"""
	completed = defaultdict(list)
	for record in records:
		if normalise_status(record.get("status")) == IMPORTED_STATUS:
			completed[get_domain_and_id_from_woocommerce_record_name(record["name"])[0]].append(record)

	for woocommerce_server, server_records in completed.items():
		# Listed again when the sync is unpaused (sync_paused_orders), and brought up then
		if is_order_sync_paused(woocommerce_server):
			continue
		# The sync makes their Sales Orders itself
		row = frappe.get_cached_doc("WooCommerce Server", woocommerce_server).get_order_status_row(
			IMPORTED_STATUS
		)
		if row and ACTION_SUBMIT in row.get_actions():
			continue
		waiting = set(leave_out_imported(woocommerce_server, [record["name"] for record in server_records]))
		numbers = [
			cstr(record.get("number") or get_domain_and_id_from_woocommerce_record_name(record["name"])[1])
			for record in server_records
			if record["name"] in waiting
		]
		if numbers:
			send_orders_to_import_notification(woocommerce_server, numbers)


def send_orders_to_import_notification(woocommerce_server: str, numbers: list[str]) -> None:
	"""
	A notification in the desk's bell, without an email, that leads to the shop's imports
	"""
	named = ", ".join(f"#{escape_html(number)}" for number in numbers[:MAX_ORDERS_NAMED])
	if len(numbers) > MAX_ORDERS_NAMED:
		named += " " + _("and {0} more").format(len(numbers) - MAX_ORDERS_NAMED)
	enqueue_create_notification(
		get_notified_users(),
		{
			"type": NOTIFICATION_TYPE,
			"document_type": "WooCommerce Server",
			"document_name": woocommerce_server,
			"link": f"/app/woocommerce-order-import?woocommerce_server={quote(woocommerce_server)}",
			"subject": _("Completed orders of {0} without a Sales Order: {1}").format(
				escape_html(woocommerce_server), named
			),
			"email_content": _(
				"The shop completed them before ERPNext saw them, so the order sync made no Sales Order for them."
				" A WooCommerce Order Import books them with their payment."
			),
		},
	)


def get_notified_users() -> list[str]:
	"""
	The emails of the enabled System Managers, who may import orders, Administrator among them: on a site one
	person set up it is the only one. Frappe finds the users of a notification by their email.
	"""
	users = ["Administrator", *get_users_with_role("System Manager")]
	return frappe.get_all("User", filters={"name": ["in", users], "enabled": 1}, pluck="email")


def get_date_filters(created_from, created_to) -> list[list]:
	"""
	The filters for orders created between two days, both included, either of them open. Frappe's between
	takes whole days, but needs both: an open end would be read as today.
	"""
	field = "date_created"
	if created_from and created_to:
		return [["WooCommerce Order", field, "between", [getdate(created_from), getdate(created_to)]]]
	if created_from:
		return [["WooCommerce Order", field, ">", getdate(created_from)]]
	if created_to:
		return [["WooCommerce Order", field, "<", add_days(getdate(created_to), 1)]]
	return []


def leave_out_imported(woocommerce_server: str, names: list[str]) -> list[str]:
	"""
	The orders that have no Sales Order, whatever its status: a cancelled one counts, as the sync knows what to
	do when its order comes back
	"""
	ids = {name: cstr(get_domain_and_id_from_woocommerce_record_name(name)[1]) for name in names}
	order_ids = list(ids.values())
	imported = set()
	for start in range(0, len(order_ids), LOOKUP_BATCH_SIZE):
		imported.update(
			frappe.get_all(
				"Sales Order",
				filters={
					"woocommerce_server": woocommerce_server,
					"woocommerce_id": ["in", order_ids[start : start + LOOKUP_BATCH_SIZE]],
				},
				pluck="woocommerce_id",
			)
		)
	return [name for name in names if ids[name] not in imported]


class OrderImport(SynchroniseSalesOrder):
	"""
	One order of a WooCommerce Order Import. It is made as the sync makes a Sales Order, and then invoiced and
	paid in one go, or not at all. Any error fails the whole order, which the job rolls back, so a missing
	Fiscal Year or a delivery without stock is tried again by the next run rather than left half made.
	"""

	# An imported order shipped long ago, and a flag would hold back its invoice: a phone number ERPNext can't
	# take is told in a comment on its Sales Order instead
	flag_refused_phones = False

	def __init__(
		self, order_import, woocommerce_order_name: str, serials: dict[str, list[str]] | None = None
	) -> None:
		super().__init__(woocommerce_order_name=woocommerce_order_name)
		self.order_import = order_import
		# The serial number file's serials by order number
		self.serials = serials or {}
		self.result: ImportResult | None = None

	@property
	def order_number(self) -> str | None:
		"""
		The number the customer saw, which the serial number file names an order by
		"""
		wc_order = self.woocommerce_order
		return cstr(wc_order.number or wc_order.woocommerce_id) if wc_order else None

	def run(self) -> ImportResult:
		# Under the order's lock, as a webhook may sync it meanwhile. An import runs while the order sync is
		# paused for the setup of the shop, so the pause doesn't stop it.
		self.run_locked(self.import_wc_order, while_paused=True)
		return self.result or ImportResult(RESULT_SKIPPED, _("Nothing was done"))

	def skip(self, message: str) -> None:
		self.result = ImportResult(RESULT_SKIPPED, message)

	def import_wc_order(self) -> None:
		wc_order = self.woocommerce_order
		if self.deleted_in_woocommerce or not wc_order:
			return self.skip(_("The shop no longer has the order"))
		if self.sales_order:
			return self.skip(_("It has Sales Order {0} by now").format(self.sales_order.name))
		if normalise_status(wc_order.status) != IMPORTED_STATUS:
			return self.skip(_("It is {0} now, not completed").format(wc_order.status))

		# An order nothing was paid for has no method to look for, and is imported without an invoice
		method = account = None
		if flt(wc_order.total) > 0:
			method = get_booking_row(self.order_import, wc_order)
			if not method:
				return self.skip(
					_("It was paid with {0}, which has no Mode of Payment in the Payment Method Map").format(
						wc_order.payment_method_title or wc_order.payment_method or _("no payment method")
					)
				)
			account = get_mode_of_payment_account(method.mode_of_payment, self.order_import.company)
			if not account:
				return self.skip(
					_("Mode of Payment {0} has no default account for company {1}").format(
						method.mode_of_payment, self.order_import.company
					)
				)

		self.create_sales_order(frozenset())
		if not self.sales_order:
			frappe.throw(_("No Sales Order could be made, as its Customer couldn't be made or found"))

		paid_at = self.get_paid_at(method)
		reason = self.get_review_reason(method, paid_at)
		invoice = payment_entry = None
		if not reason:
			invoice = self.make_invoice(paid_at)
			payment_entry = self.make_payment_entry(invoice, method, account, paid_at)
		if self.order_import.deliver:
			self.deliver()
		closed = self.close_if_undelivered()

		if reason:
			self.flag_review(reason, closed)
			self.result = ImportResult(RESULT_NEEDS_REVIEW, reason, self.sales_order.name)
		else:
			self.result = ImportResult(
				RESULT_PAID, None, self.sales_order.name, invoice.name, payment_entry.name
			)

	def get_paid_at(self, method) -> datetime | None:
		"""
		When the order's invoice and payment are dated: when WooCommerce took it as paid, or, for a payment method
		whose row says so, when it was completed. WooCommerce takes cash on delivery as paid at checkout, and a
		transfer for a reserved product comes in after it. An order with nothing to pay has no row.
		"""
		value = self.woocommerce_order.get("date_completed" if is_paid_on_completion(method) else "date_paid")
		return get_datetime(value) if value else None

	def get_review_reason(self, method, paid_at: datetime | None) -> str | None:
		"""
		Why the order can't be invoiced with its payment, so a person has to: None when it can
		"""
		wc_order, sales_order = self.woocommerce_order, self.sales_order
		if sales_order.woocommerce_sync_issue:
			return _("Its Sales Order is flagged: {0}").format(sales_order.woocommerce_sync_issue)
		if not paid_at:
			if is_paid_on_completion(method):
				return _("WooCommerce has no completion date for it")
			return _("WooCommerce has no payment date for it")
		# WooCommerce's total doesn't subtract them, so the invoice would bill too much
		if get_json_lines(wc_order, "refunds"):
			return _("It was partly refunded in the shop, so it needs a Credit Note")
		if flt(sales_order.grand_total) <= 0:
			return _("Nothing was paid for it")
		return None

	def flag_review(self, reason: str, closed: bool) -> None:
		if self.sales_order.woocommerce_sync_issue:
			message = _("Imported by {0} without an invoice, as it is flagged.").format(
				self.order_import.name
			)
		else:
			message = _("Imported by {0} without an invoice: {1}.").format(self.order_import.name, reason)
		if closed:
			message += " " + _("Re-open it to invoice it.")
		self.flag_sync_issue(message)

	def make_invoice(self, paid_at: datetime):
		"""
		The Sales Invoice of the order, dated when it was paid, as an invoice is made when its payment is
		processed. Submitted at once, as its Payment Entry follows.
		"""
		sales_order = self.sales_order
		paid_on = getdate(paid_at)
		invoice = make_sales_invoice(sales_order.name)
		# Delivering is a separate step, with its own Delivery Note
		invoice.update_stock = 0
		invoice.set_posting_time = 1
		invoice.posting_date = paid_on
		invoice.posting_time = paid_at.strftime("%H:%M:%S")
		# The mapper dated them from today, which a Payment Terms Template refuses for an invoice of an earlier
		# day. ERPNext makes the schedule again from the posting date.
		invoice.due_date = get_due_date(
			paid_on,
			"Customer",
			invoice.customer,
			invoice.company,
			template_name=invoice.payment_terms_template,
		)
		invoice.payment_schedule = []
		invoice.flags.created_by_sync = True
		invoice.insert()
		invoice.submit()
		return invoice

	def make_payment_entry(self, invoice, method, account: str, paid_at: datetime):
		"""
		The payment of the invoice, on the account of the order's Mode of Payment, dated as the invoice
		"""
		wc_order = self.woocommerce_order
		paid_on = getdate(paid_at)
		payment_entry = get_payment_entry(
			"Sales Invoice", invoice.name, bank_account=account, reference_date=paid_on
		)
		payment_entry.posting_date = paid_on
		set_exchange_rates(payment_entry, invoice)
		payment_entry.mode_of_payment = method.mode_of_payment
		payment_entry.reference_no = cstr(
			wc_order.get("transaction_id") or wc_order.payment_method_title or wc_order.payment_method
		)[:REFERENCE_NO_LENGTH]
		# get_payment_entry gives it the company's default Bank Account, which a cash payment must not carry
		payment_entry.bank_account = method.bank_account or None
		if method.bank_account:
			payment_entry.set_bank_account_data()
		else:
			payment_entry.bank = payment_entry.bank_account_no = None
		payment_entry.flags.created_by_sync = True
		payment_entry.insert()
		payment_entry.submit()
		return payment_entry

	def deliver(self):
		"""
		Deliver the order, dated when WooCommerce completed it, else when it was placed
		"""
		sales_order, wc_order = self.sales_order, self.woocommerce_order
		delivery_note = make_delivery_note(sales_order.name)
		completed = wc_order.get("date_completed")
		delivery_note.set_posting_time = 1
		delivery_note.posting_date = getdate(completed) if completed else sales_order.transaction_date
		if completed:
			delivery_note.posting_time = get_datetime(completed).strftime("%H:%M:%S")
		assign_serial_numbers(delivery_note, self.serials.get(self.order_number, []))
		delivery_note.flags.created_by_sync = True
		delivery_note.insert()
		delivery_note.submit()
		return delivery_note

	def close_if_undelivered(self) -> bool:
		"""
		Close the Sales Order when it wasn't delivered, so an order the shop is done with reserves no stock.
		Whether it was closed.
		"""
		sales_order = self.sales_order
		# Its invoice and delivery changed it, and ERPNext refuses to change the status of a stale copy
		sales_order.reload()
		sales_order.flags.created_by_sync = True
		if flt(sales_order.per_delivered) >= 100:
			return False
		self.close_sales_order()
		# Closing leaves the reservations that ERPNext's auto-reserve made when the order was submitted
		if frappe.db.exists(
			"Stock Reservation Entry",
			{"voucher_type": "Sales Order", "voucher_no": sales_order.name, "docstatus": 1},
		):
			sales_order.cancel_stock_reservation_entries(notify=False)
		return True


def is_paid_on_completion(method) -> bool:
	"""
	Whether the orders paid with a row's payment method are dated when they were completed
	"""
	return bool(method) and method.get("payment_date") == PAYMENT_DATE_COMPLETED


def set_exchange_rates(payment_entry, invoice) -> None:
	"""
	The exchange rates of a Payment Entry for its posting date. get_payment_entry took today's, and validate
	keeps rates that are set.
	"""
	company_currency = frappe.get_cached_value("Company", payment_entry.company, "default_currency")
	if {payment_entry.paid_from_account_currency, payment_entry.paid_to_account_currency} <= {
		company_currency
	}:
		return
	payment_entry.source_exchange_rate = payment_entry.target_exchange_rate = 0
	payment_entry.set_exchange_rate(ref_doc=invoice)
	payment_entry.set_amounts()
