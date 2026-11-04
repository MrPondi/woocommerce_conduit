import re
from collections import defaultdict
from datetime import datetime

import frappe
import frappe.utils
from erpnext.selling.doctype.sales_order.sales_order import SalesOrder
from erpnext.selling.doctype.sales_order_item.sales_order_item import SalesOrderItem
from frappe import _
from frappe.utils import cint, cstr, get_datetime
from frappe.utils.data import now
from frappe.utils.synchronization import filelock

from woocommerce_conduit.exceptions import SyncDisabledError
from woocommerce_conduit.tasks.order_customer import SalesOrderCustomerMixin
from woocommerce_conduit.tasks.order_lines import SalesOrderLinesMixin, get_json_lines
from woocommerce_conduit.tasks.order_payment import SalesOrderPaymentMixin, get_payment_method
from woocommerce_conduit.tasks.order_status import (
	SalesOrderStatusMixin,
	get_status_to_push,
	get_statuses_to_sync,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_HOLD,
	ACTION_INVOICE,
	ACTION_SUBMIT,
	ENDING_ACTIONS,
	normalise_status,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_settings.woocommerce_settings import (
	WooCommerceSettings,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
	get_domain_and_id_from_woocommerce_record_name,
)

# The most orders one sync looks at, fetched 100 per request
MAX_ORDERS_PER_SYNC = 10_000


class SyncedOrderItem(SalesOrderItem):
	woocommerce_id: int


class SyncedOrder(SalesOrder):
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		woocommerce_server: DF.Data
		woocommerce_id: DF.Data
		woocommerce_last_sync_hash: DF.Datetime
		woocommerce_status: DF.Data
		woocommerce_payment_method: DF.Data
		woocommerce_sync_issue: DF.SmallText
		woocommerce_customer_note: DF.Data
		items: DF.Table[SyncedOrderItem]  # pyright: ignore[reportIncompatibleVariableOverride]


def enqueue_sales_order_sync(
	woocommerce_order_name: str | None = None, sales_order_name: str | None = None
) -> None:
	"""
	Sync an order in the background. The job gets names and loads the documents itself. A job that is
	still waiting in the queue will pick up everything that changed, so a second one is dropped.
	"""
	# Picked up again when the sync is unpaused
	if woocommerce_order_name and is_order_sync_paused(
		get_domain_and_id_from_woocommerce_record_name(woocommerce_order_name)[0]
	):
		return
	frappe.enqueue(
		run_sales_order_sync,
		queue="long",
		timeout=300,
		job_id=f"woocommerce_order_sync::{woocommerce_order_name or sales_order_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		woocommerce_order_name=woocommerce_order_name,
		sales_order_name=sales_order_name,
	)


def run_sales_order_sync_from_hook(doc, method):
	"""
	Intended to be triggered by a Document Controller hook from Sales Order
	"""
	if not doc.flags.get("created_by_sync", None) and doc.woocommerce_server and doc.woocommerce_id:
		frappe.msgprint(
			_("Background sync to WooCommerce triggered for {0} {1}").format(frappe.bold(doc.name), method),
			indicator="blue",
			alert=True,
		)
		enqueue_sales_order_sync(sales_order_name=doc.name)


def sync_woocommerce_orders_modified_since(date_time_from=None):
	"""
	Sync the WooCommerce orders modified since date_time_from (by default since the last run)
	"""
	settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	if not date_time_from:
		date_time_from = getattr(settings, "wc_last_sync_date_orders", None)

	# Taken before fetching, so an order that changes while this runs is found by the next run. If
	# WooCommerce can't be reached this raises and the cursor stays where it was.
	sync_started = now()
	for order_name in get_names_of_wc_orders(date_time_from=date_time_from, status=get_statuses_to_sync()):
		enqueue_sales_order_sync(woocommerce_order_name=order_name)

	frappe.db.set_single_value("WooCommerce Settings", "wc_last_sync_date_orders", sync_started)


def is_order_sync_paused(woocommerce_server: str | None) -> bool:
	"""
	Whether the orders of a server are left alone for now, while its Items are being set up
	"""
	return bool(
		woocommerce_server
		and frappe.get_cached_value("WooCommerce Server", woocommerce_server, "pause_order_sync")
	)


def sync_paused_orders(woocommerce_server: str, since):
	"""
	Sync the orders of a server that changed while its order sync was paused. Enqueued when it is unpaused.
	"""
	# Without a date every order of the shop would be synced
	if not since:
		return
	for order_name in get_names_of_wc_orders(
		date_time_from=since, status=get_statuses_to_sync(), servers=[woocommerce_server]
	):
		enqueue_sales_order_sync(woocommerce_order_name=order_name)


@frappe.whitelist(methods=["POST"])
def sync_woocommerce_order(woocommerce_order_name: str):
	"""
	Sync one WooCommerce Order with its Sales Order, from the WooCommerce Order form
	"""
	frappe.has_permission("Sales Order", "create", throw=True)
	return run_sales_order_sync(woocommerce_order_name=woocommerce_order_name)


def run_sales_order_sync(
	sales_order_name: str | None = None,
	sales_order: SyncedOrder | None = None,
	woocommerce_order_name: str | None = None,
	woocommerce_order: WooCommerceOrder | None = None,
	enqueue=False,
) -> tuple[SyncedOrder | None, WooCommerceOrder | None]:
	"""
	Synchronize an ERPNext Sales Order with its corresponding WooCommerce Order.

	It can be initiated from either the ERPNext side (a Sales Order) or the WooCommerce side (a
	WooCommerce Order). Either is given by name, or as a document of which only the name is used: the
	sync loads current copies of both itself.

	Parameters:
	    sales_order_name: ERPNext Sales Order name to synchronize
	    sales_order: ERPNext Sales Order document to synchronize
	    woocommerce_order_name: Name of WooCommerce Order to synchronize
	    woocommerce_order: WooCommerce Order document to synchronize
	    enqueue: Whether to process synchronization in background

	Returns:
	    tuple: (ERPNext Sales Order, WooCommerce Order) after synchronization, (None, None) when enqueued

	Raises:
	    ValueError: If no valid input parameters are provided
	    ValidationError: If the Sales Order is not linked to a WooCommerce Order
	"""
	woocommerce_order_name = woocommerce_order_name or (woocommerce_order.name if woocommerce_order else None)
	sales_order_name = sales_order_name or (sales_order.name if sales_order else None)

	if not (woocommerce_order_name or sales_order_name):
		raise ValueError(
			"At least one of sales_order_name, sales_order, woocommerce_order_name, woocommerce_order is required"
		)

	if enqueue:
		enqueue_sales_order_sync(
			woocommerce_order_name=woocommerce_order_name,
			sales_order_name=None if woocommerce_order_name else sales_order_name,
		)
		return None, None

	sync = SynchroniseSalesOrder(
		sales_order_name=None if woocommerce_order_name else sales_order_name,
		woocommerce_order_name=woocommerce_order_name,
	)
	sync.run()
	return sync.sales_order, sync.woocommerce_order


class SynchroniseSalesOrder(
	SalesOrderCustomerMixin, SalesOrderStatusMixin, SalesOrderPaymentMixin, SalesOrderLinesMixin
):
	"""
	Class for managing synchronisation of a WooCommerce Order with an ERPNext Sales Order
	"""

	def __init__(
		self,
		sales_order_name: str | None = None,
		woocommerce_order_name: str | None = None,
	) -> None:
		self.sales_order_name = sales_order_name
		self.woocommerce_order_name = woocommerce_order_name
		self.sales_order: SyncedOrder | None = None
		self.woocommerce_order: WooCommerceOrder | None = None
		# The shop deleted the order for good, so there is no WooCommerce Order to load
		self.deleted_in_woocommerce = False
		self.customer_issues: list[str] = []

	def get_lock_name(self) -> str:
		"""
		One lock per WooCommerce Order, whichever side the sync starts from
		"""
		return "woocommerce_order_" + re.sub(r"[^\w.~-]", "_", self.get_record_name())

	def get_record_name(self) -> str:
		"""
		The name of the WooCommerce Order, also when the sync starts from its Sales Order
		"""
		record_name = self.woocommerce_order_name
		if not record_name:
			link = frappe.db.get_value(
				"Sales Order", self.sales_order_name, ["woocommerce_server", "woocommerce_id"], as_dict=True
			)
			if not link:
				frappe.throw(_("Sales Order {0} not found").format(self.sales_order_name))
			if not (link.woocommerce_server and link.woocommerce_id):
				frappe.throw(
					_("Sales Order {0} is not linked to a WooCommerce Order").format(self.sales_order_name)
				)
			record_name = generate_woocommerce_record_name_from_domain_and_id(
				link.woocommerce_server, link.woocommerce_id
			)
		return record_name

	def run(self):
		"""
		Run synchronisation
		"""
		self.run_locked(self.sync_wc_order_with_erpnext_order)

	def push_status(self):
		"""
		Set the WooCommerce Order to the status that its Sales Order has now
		"""
		self.run_locked(self.push_status_to_woocommerce)

	def run_locked(self, sync):
		"""
		Load both orders and sync them. The documents are loaded inside the lock, so the webhook, the hourly
		sync and the Sales Order hook can't all create the Sales Order of the same WooCommerce Order, or
		change it on top of each other.
		"""
		woocommerce_server = get_domain_and_id_from_woocommerce_record_name(self.get_record_name())[0]
		if is_order_sync_paused(woocommerce_server):
			# Asked for in the form, rather than by a job that was queued before the pause
			if frappe.request:
				frappe.throw(
					_("The order sync of WooCommerce Server {0} is paused").format(
						frappe.bold(woocommerce_server)
					)
				)
			return

		with filelock(self.get_lock_name(), timeout=120):
			try:
				self.load_orders()
				self.get_corresponding_sales_order_or_woocommerce_order()
				sync()
			except Exception:
				error_message = (
					f"{frappe.get_traceback()}\n\n"
					f"Sales Order Data: \n{describe_doc(self.sales_order)}\n\n"
					f"WC Order Data \n{describe_doc(self.woocommerce_order)})"
				)
				frappe.log_error("WooCommerce Error", error_message)
				raise

	def load_orders(self):
		if self.woocommerce_order_name:
			self.load_woocommerce_order(self.woocommerce_order_name)
		else:
			self.sales_order = frappe.get_doc("Sales Order", self.sales_order_name)  # type: ignore

	def load_woocommerce_order(self, woocommerce_order_name: str) -> None:
		"""
		Load the WooCommerce Order. One that the shop deleted for good (it says the id is invalid) is no error:
		its Sales Order is synced as that of an order in the trash.
		"""
		try:
			self.woocommerce_order = load_wc_order(woocommerce_order_name)
		except frappe.DoesNotExistError:
			self.deleted_in_woocommerce = True

	def get_corresponding_sales_order_or_woocommerce_order(self):
		"""
		If we have an ERPNext Sales Order, get the corresponding WooCommerce Order
		If we have a WooCommerce Order, get the corresponding ERPNext Sales Order

		Assumes that both exist, and that the Sales Order is linked to the WooCommerce Order
		"""
		if self.sales_order and not self.woocommerce_order:
			if not self.sales_order.woocommerce_server or not self.sales_order.woocommerce_id:
				frappe.throw(
					_("Sales Order {0} is not linked to a WooCommerce Order").format(self.sales_order.name)
				)

			# Validate that this Sales Order's WooCommerce Server has sync enabled
			wc_server: WooCommerceServer = frappe.get_cached_doc(
				"WooCommerce Server",
				self.sales_order.woocommerce_server,  # type: ignore
			)
			if not wc_server.enabled:
				raise SyncDisabledError(wc_server)

			self.load_woocommerce_order(
				generate_woocommerce_record_name_from_domain_and_id(
					self.sales_order.woocommerce_server, self.sales_order.woocommerce_id
				)
			)

		if not self.sales_order and (self.woocommerce_order or self.deleted_in_woocommerce):
			self.get_erpnext_sales_order()

	def get_erpnext_sales_order(self):
		"""
		Get the ERPNext Sales Order of the WooCommerce Order, found by its name, as the shop may have deleted it
		"""
		woocommerce_server, woocommerce_id = get_domain_and_id_from_woocommerce_record_name(
			self.get_record_name()
		)
		if name := find_sales_order(woocommerce_server, woocommerce_id):
			self.sales_order: SyncedOrder = frappe.get_doc("Sales Order", name)  # type: ignore

	def sync_wc_order_with_erpnext_order(self):
		"""
		Synchronise Sales Order between ERPNext and WooCommerce
		"""
		if self.deleted_in_woocommerce:
			self.sync_deleted_order()
			return
		if not self.woocommerce_order:
			return

		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server
		)  # type: ignore
		status_row = wc_server.get_order_status_row(self.woocommerce_order.status)
		# A status that the map doesn't know leaves ERPNext as it is
		actions = status_row.get_actions() if status_row else frozenset()

		if not self.sales_order:
			# Only some statuses mean there is an order to keep. For the others nothing is made, and that
			# includes the Customer, Address and Contact that a Sales Order would need.
			if ACTION_SUBMIT not in actions:
				return
			self.create_sales_order(actions)
		# WooCommerce only wins when it changed since the last pull or push
		elif wc_order_changed(
			self.woocommerce_order.woocommerce_date_modified, self.sales_order.woocommerce_last_sync_hash
		):
			self.update_sales_order(actions)

		# Also when nothing changed, as the Sales Order may have been submitted in the mean time
		self.sync_invoice_and_payment(actions)

	def push_status_to_woocommerce(self):
		"""
		Set the WooCommerce Order to the status that the server maps the status of its Sales Order to. If
		WooCommerce changed the order in the meantime, say a payment came in, it wins and the order is
		pulled instead. Otherwise that payment would be undone with a stale status.
		"""
		# Deleting the order is the change WooCommerce made, and there is nothing to push it to
		if self.deleted_in_woocommerce:
			self.sync_deleted_order()
			return
		if not (self.sales_order and self.woocommerce_order):
			return

		if wc_order_changed(
			self.woocommerce_order.woocommerce_date_modified, self.sales_order.woocommerce_last_sync_hash
		):
			self.sync_wc_order_with_erpnext_order()
			return

		status = get_status_to_push(self.sales_order)
		if not status:
			return

		if normalise_status(self.woocommerce_order.status) != status:
			self.woocommerce_order.status = status
			self.woocommerce_order.flags.ignore_permissions = True
			# The Sales Order has the status already, it needn't be pulled for it
			self.woocommerce_order.flags.created_by_sync = True
			self.woocommerce_order.save()

		# The date that WooCommerce gave back for the change is what the next pull compares with
		self.sales_order.db_set(
			{
				"woocommerce_status": status,
				"woocommerce_last_sync_hash": get_datetime(self.woocommerce_order.woocommerce_date_modified),
			},
			update_modified=False,
		)

	def update_sales_order(self, actions: frozenset[str] = frozenset()):
		"""
		Update the ERPNext Sales Order with fields from it's corresponding WooCommerce Order. A cancelled one
		is left as it is, unless the order came back (came_back): then it is amended, and a closed one opened
		again.
		"""
		if not self.woocommerce_order or not self.sales_order:
			return

		# Asked before the new status is written over the one the Sales Order last saw
		came_back = self.came_back(actions)
		if self.sales_order.docstatus == 2:
			if not came_back:
				return
			# What is flagged on the cancelled Sales Order must not push its status back to WooCommerce
			self.sales_order.flags.created_by_sync = True
			self.run_flagged(
				self.amend_sales_order,
				_("Back in WooCommerce, but the Sales Order could not be amended: {0}"),
			)
			if self.sales_order.docstatus == 2:
				return

		wc_order_status = normalise_status(self.woocommerce_order.status)
		status_changed = normalise_status(self.sales_order.woocommerce_status) != wc_order_status
		values = {
			"woocommerce_status": wc_order_status,
			# Always note what was seen, so an unchanged WooCommerce Order isn't looked at again
			"woocommerce_last_sync_hash": get_datetime(self.woocommerce_order.woocommerce_date_modified),
		}
		if self.woocommerce_order.customer_note:
			values["woocommerce_customer_note"] = self.woocommerce_order.customer_note
		if payment_method := get_payment_method(self.woocommerce_order):
			values["woocommerce_payment_method"] = payment_method

		# Only the sync's own fields change, so they are written without a save of the submitted Sales
		# Order, which would validate it again and keep a Version of every pull
		self.sales_order.flags.created_by_sync = True
		self.sales_order.db_set(values, update_modified=False)

		if came_back and self.sales_order.status == "Closed":
			self.run_flagged(
				lambda: self.reopen_sales_order(wc_order_status),
				_("Back in WooCommerce, but the Sales Order could not be reopened: {0}"),
			)

		# The lines of an order that is being cancelled or closed don't matter any more
		if self.sales_order.docstatus == 1 and not (status_changed and actions & ENDING_ACTIONS):
			self.run_flagged(self.update_sales_order_items, _("The lines could not be updated: {0}"))
			self.sales_order.reload()

		# What a status means is done once, when it changes. Otherwise editing the note of an order would
		# undo a hold that was put on its Sales Order in ERPNext.
		if status_changed:
			self.apply_status_actions(actions)

	def create_sales_order(self, actions: frozenset[str]) -> None:
		"""
		Create an ERPNext Sales Order from the given WooCommerce Order
		"""
		if not self.woocommerce_order:
			return

		customer_docname = self.create_or_link_customer_and_address()
		if not customer_docname:
			frappe.log_error(
				"WooCommerce Sync",
				f"Failed to create/link customer for WC Order {self.woocommerce_order.woocommerce_id} ({self.woocommerce_order.name}). Aborting Sales Order creation.",
			)
			return
		self.create_missing_items()

		new_sales_order: SyncedOrder = frappe.new_doc("Sales Order")  # type: ignore
		self.set_customer_in_sales_order(new_sales_order)
		new_sales_order.woocommerce_id = self.woocommerce_order.woocommerce_id
		# The number the customer sees, which plugins can make differ from the id
		order_number = self.woocommerce_order.number or self.woocommerce_order.woocommerce_id
		new_sales_order.po_no = order_number
		if self.woocommerce_order.customer_note:
			new_sales_order.woocommerce_customer_note = self.woocommerce_order.customer_note

		new_sales_order.woocommerce_status = normalise_status(self.woocommerce_order.status)
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_order.woocommerce_server
		)  # type: ignore

		new_sales_order.woocommerce_server = self.woocommerce_order.woocommerce_server
		if payment_method := get_payment_method(self.woocommerce_order):
			new_sales_order.woocommerce_payment_method = payment_method
		created_date = str(self.woocommerce_order.woocommerce_date_created).split("T", 1)
		new_sales_order.transaction_date = created_date[0]
		delivery_after = 7 if wc_server.delivery_after_days is None else cint(wc_server.delivery_after_days)
		new_sales_order.delivery_date = frappe.utils.add_days(created_date[0], delivery_after)
		new_sales_order.company = wc_server.company
		new_sales_order.currency = self.woocommerce_order.currency

		if wc_server.enabled_shipping_methods and (
			shipping_lines := get_json_lines(self.woocommerce_order, "shipping_lines")
		):
			# A method without a rule is charged as WooCommerce charged it
			if shipping_rule := wc_server.get_shipping_rule(shipping_lines[0]):
				new_sales_order.shipping_rule = shipping_rule

		self.set_items_in_sales_order(new_sales_order)
		new_sales_order.woocommerce_last_sync_hash = self.woocommerce_order.woocommerce_date_modified
		new_sales_order.flags.ignore_mandatory = True
		new_sales_order.flags.created_by_sync = True
		# Named here rather than by a hook or a Sales Order class, which could clash with other apps and would
		# name the Sales Orders made by hand too. None leaves it to the naming series.
		new_sales_order.insert(set_name=get_sales_order_name(wc_server, order_number))

		# Kept, but flagged for a person to look at
		issues = [*self.customer_issues, *self.line_issues]
		if issue := self.check_totals(new_sales_order):
			issues.append(issue)
		if issues:
			new_sales_order.woocommerce_sync_issue = "; ".join(issues)
		new_sales_order.submit()

		new_sales_order.reload()
		new_sales_order.flags.created_by_sync = True

		self.sales_order = new_sales_order
		try:
			if ACTION_HOLD in actions and ACTION_INVOICE in actions:
				self.invoice_and_hold()
			elif ACTION_HOLD in actions:
				self.hold_sales_order()
		except Exception:
			# The Sales Order is made, so a hold that fails must not undo it
			error_message = f"{frappe.get_traceback()}\n\nSales Order Data{new_sales_order.as_dict()!s}"
			frappe.log_error("WooCommerce Error", error_message)


def push_order_status(sales_order_name: str) -> None:
	"""
	Background job: set the WooCommerce Order of a Sales Order to the status that the server maps the
	status of the Sales Order to
	"""
	SynchroniseSalesOrder(sales_order_name=sales_order_name).push_status()


def find_sales_order(woocommerce_server: str, woocommerce_id) -> str | None:
	"""
	The Sales Order of a WooCommerce Order: the newest that isn't cancelled, else the newest cancelled one.
	So an order that was cancelled and amended is found by its amendment, and one that was cancelled in
	ERPNext isn't made again.
	"""
	sales_orders = frappe.get_all(
		"Sales Order",
		# The id is text in the table. Compared as a number, MariaDB would read every row.
		filters={"woocommerce_server": woocommerce_server, "woocommerce_id": cstr(woocommerce_id)},
		fields=["name", "docstatus"],
		order_by="creation desc",
	)
	return next(
		(row.name for row in sales_orders if row.docstatus < 2),
		sales_orders[0].name if sales_orders else None,
	)


def get_sales_order_name(wc_server, order_number) -> str | None:
	"""
	The name of a new Sales Order, when its server names them by order number: the server's prefix and the
	number. None for the naming series: when the server doesn't, or when the name is taken (by another shop's
	order or a Sales Order made by hand) or isn't one Frappe takes.
	"""
	order_number = cstr(order_number).strip()
	if not (wc_server.get("name_sales_orders_by_number") and order_number):
		return None
	name = cstr(wc_server.get("sales_order_prefix")).strip() + order_number
	if len(name) > 140 or "<" in name or ">" in name or frappe.db.exists("Sales Order", name):
		return None
	return name


def wc_order_changed(woocommerce_date_modified, last_sync_hash) -> bool:
	"""
	Whether WooCommerce changed the order since it was last pulled or pushed. The hash is the
	`date_modified` WooCommerce had then, so both sides come from WooCommerce and the clock of
	ERPNext (or when the Sales Order was last saved) plays no part.
	"""
	if not last_sync_hash:
		return True
	return get_datetime(woocommerce_date_modified) > get_datetime(last_sync_hash)


def load_wc_order(woocommerce_order_name: str) -> WooCommerceOrder:
	"""
	Load the full WooCommerce Order, as the list view only has the fields it asked for
	"""
	wc_order: WooCommerceOrder = frappe.get_doc(
		{"doctype": "WooCommerce Order", "name": woocommerce_order_name}
	)  # type: ignore
	wc_order.load_from_db()

	if not wc_order.woocommerce_server or not wc_order.woocommerce_id:
		raise ValueError(
			f"WooCommerce Order {wc_order.name} is missing required fields: "
			f"server={wc_order.woocommerce_server}, id={wc_order.woocommerce_id}"
		)
	return wc_order


def describe_doc(doc) -> str:
	"""
	A document for an error log, which must not fail on a document that is half loaded
	"""
	if not doc:
		return ""
	try:
		return str(doc.as_dict())
	except Exception:
		return str(doc)


def get_names_of_wc_orders(
	date_time_from: datetime | str | None = None,
	status: list[str] | None = None,
	servers: list[str] | None = None,
) -> list[str]:
	"""
	The names of the WooCommerce Orders on all enabled servers, or on `servers`, that were modified after
	`date_time_from` and created after the minimum creation date of the settings, with one of the statuses.
	Orders whose Sales Order has seen that change already are left out.

	Raises when WooCommerce can't be reached, so an unreachable shop isn't taken for one without orders.
	"""
	settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	filters = []
	if date_time_from:
		# WooCommerce takes the date without microseconds
		filters.append(
			[
				"WooCommerce Order",
				"date_modified",
				">",
				get_datetime(date_time_from).strftime("%Y-%m-%dT%H:%M:%S"),
			]
		)
	if settings.minimum_creation_date:
		filters.append(["WooCommerce Order", "date_created", ">", settings.minimum_creation_date])
	if status:
		filters.append(["WooCommerce Order", "status", "in", status])

	records = WooCommerceOrder.get_list_of_records(
		{
			"doctype": "WooCommerce Order",
			"filters": filters,
			"fields": ["name"],
			"page_length": MAX_ORDERS_PER_SYNC,
			"servers": servers,
			"strict": True,
		}
	)
	return get_changed_order_names(records)


def get_changed_order_names(records: list[dict]) -> list[str]:
	"""
	The names of the orders that changed since their Sales Order last saw them (the sync hash). The webhook
	syncs most orders before the hourly sync lists them, and those needn't be loaded again.
	"""
	ids_by_server = defaultdict(set)
	for record in records:
		server, order_id = get_domain_and_id_from_woocommerce_record_name(record["name"])
		ids_by_server[server].add(cstr(order_id))

	last_seen = {}
	for server, order_ids in ids_by_server.items():
		for row in frappe.get_all(
			"Sales Order",
			filters={"woocommerce_server": server, "woocommerce_id": ["in", list(order_ids)]},
			fields=["woocommerce_id", "woocommerce_last_sync_hash"],
		):
			if not row.woocommerce_last_sync_hash:
				continue
			key = (server, row.woocommerce_id)
			last_seen[key] = max(
				last_seen.get(key) or row.woocommerce_last_sync_hash, row.woocommerce_last_sync_hash
			)

	names = []
	for record in records:
		server, order_id = get_domain_and_id_from_woocommerce_record_name(record["name"])
		date_modified = record.get("woocommerce_date_modified")
		if not date_modified or wc_order_changed(date_modified, last_seen.get((server, cstr(order_id)))):
			names.append(record["name"])
	return names
