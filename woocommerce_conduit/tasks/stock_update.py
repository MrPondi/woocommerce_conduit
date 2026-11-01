import math

import frappe
from frappe import _
from frappe.utils import create_batch

from woocommerce_conduit.tasks.sync import enqueue_unless_queued, record_sync_time
from woocommerce_conduit.tasks.sync_items import load_wc_product
from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

# Documents whose submission or cancellation changes an Item's stock (or reserved stock)
STOCK_DOCTYPES = (
	"Stock Entry",
	"Stock Reconciliation",
	"Delivery Note",
	"Purchase Receipt",
	"Sales Invoice",
	"Purchase Invoice",
	"Sales Order",
)
# Invoices only move stock when they are set to update it
INVOICE_DOCTYPES = ("Sales Invoice", "Purchase Invoice")
# The Items one job of the daily push takes: as many as one WooCommerce batch request takes
ITEMS_PER_JOB = 100
# The Items whose stock the current transaction changed, kept in frappe.flags until it is committed
PENDING_ITEMS_FLAG = "woocommerce_stock_items"


def stock_sync_is_enabled() -> bool:
	"""
	Whether any WooCommerce Server has its stock sync switched on
	"""
	return bool(
		frappe.db.exists("WooCommerce Server", {"enabled": 1, "enable_stock_level_synchronisation": 1})
	)


def reserved_stock_is_subtracted() -> bool:
	"""
	Whether a WooCommerce Server with stock sync subtracts the stock that Sales Orders reserve
	"""
	return bool(
		frappe.db.exists(
			"WooCommerce Server",
			{"enabled": 1, "enable_stock_level_synchronisation": 1, "subtract_reserved_stock": 1},
		)
	)


def update_stock_levels_for_woocommerce_item(doc, method=None):
	"""
	Intended to be triggered by a Document Controller hook (on_submit and on_cancel) from the documents in
	STOCK_DOCTYPES. Pushes the stock of each Item in the document once the change is committed.
	"""
	if frappe.flags.in_test or doc.doctype not in STOCK_DOCTYPES or not stock_sync_is_enabled():
		return
	if doc.doctype in INVOICE_DOCTYPES and not doc.get("update_stock"):
		return
	# A Sales Order only reserves stock, which only counts on a server that subtracts it
	if doc.doctype == "Sales Order" and not reserved_stock_is_subtracted():
		return

	enqueue_stock_updates({row.item_code for row in doc.get("items") or [] if row.item_code})


def enqueue_stock_update(item_code: str):
	"""
	Push an Item's stock to WooCommerce in the background, once the current transaction is committed.
	Public, so other apps can request a push when something they feed into the stock changes (see the
	woocommerce_stock_values hook).
	"""
	enqueue_stock_updates({item_code})


def enqueue_stock_updates(item_codes: set[str]):
	"""
	Push the stock of these Items once the transaction is committed. Each Item is pushed once, however many
	documents and rows of the transaction move it, and only when it is linked to WooCommerce. A rollback
	drops them with the transaction.
	"""
	pending = frappe.flags.get(PENDING_ITEMS_FLAG)
	if pending is None:
		pending = frappe.flags[PENDING_ITEMS_FLAG] = set()
		frappe.db.after_commit.add(push_pending_stock_updates)
		frappe.db.after_rollback.add(forget_pending_stock_updates)
	pending.update(item_codes)


def push_pending_stock_updates():
	for item_code in get_linked_items(frappe.flags.pop(PENDING_ITEMS_FLAG, None)):
		enqueue_unless_queued(
			"woocommerce_conduit.tasks.stock_update.update_stock_levels_on_woocommerce_site",
			job_id=f"woocommerce_stock_update::{item_code}",
			item_code=item_code,
		)


def forget_pending_stock_updates():
	frappe.flags.pop(PENDING_ITEMS_FLAG, None)


def get_linked_items(item_codes) -> list[str]:
	"""
	The Items among these that are linked to a WooCommerce Product with their sync on
	"""
	if not item_codes:
		return []
	return sorted(
		set(
			frappe.get_all(
				"Item WooCommerce Server",
				filters={"parenttype": "Item", "parent": ["in", list(item_codes)], "enable_sync": 1},
				pluck="parent",
			)
		)
	)


def update_stock_levels_for_all_enabled_items_in_background():
	"""
	Push the stock of every Item linked to a WooCommerce Product, ITEMS_PER_JOB Items a job. A safety net for
	stock that changed without one of the hooked documents.
	"""
	if not stock_sync_is_enabled():
		return

	item_codes = sorted(
		set(
			frappe.get_all(
				"Item WooCommerce Server", filters={"parenttype": "Item", "enable_sync": 1}, pluck="parent"
			)
		)
	)
	for item_codes_of_job in create_batch(item_codes, ITEMS_PER_JOB):
		frappe.enqueue(
			"woocommerce_conduit.tasks.stock_update.update_stock_levels_of_items",
			queue="long",
			timeout=1800,
			item_codes=list(item_codes_of_job),
		)


def update_stock_levels_of_items(item_codes: list[str]):
	"""
	Background job: push the stock of these Items, carrying on past an Item that fails
	"""
	for item_code in item_codes:
		try:
			update_stock_levels_on_woocommerce_site(item_code)
		except Exception:
			# Each product that failed was logged
			continue


@frappe.whitelist(methods=["POST"])
def sync_item_stock_to_woocommerce(item_code: str):
	"""
	Push one Item's stock to WooCommerce, from the Item form
	"""
	frappe.has_permission("Item", "write", doc=item_code, throw=True)
	if not stock_sync_is_enabled():
		frappe.throw(_("Stock sync is not enabled on any WooCommerce Server"))
	return update_stock_levels_on_woocommerce_site(item_code)


def update_stock_levels_on_woocommerce_site(item_code: str) -> bool:
	"""
	Push an Item's stock to the WooCommerce Product of each shop it is linked to that has stock sync on.
	Only what differs from the product is written, as WooCommerce dates every save as a change.

	Returns:
		bool: whether the Item has stock to sync at all
	"""
	item = frappe.get_doc("Item", item_code)
	# A template's stock lives on its variants, as in WooCommerce
	if not item.get("woocommerce_servers") or not item.is_stock_item or item.has_variants:
		return False

	bins = frappe.get_all(
		"Bin", filters={"item_code": item_code}, fields=["warehouse", "actual_qty", "reserved_qty"]
	)

	errors = []
	for connection in item.woocommerce_servers:
		# A connection can be left out of the stock (and price) sync, to manage it by hand in that shop
		if not (connection.woocommerce_server and connection.woocommerce_id and connection.enable_sync):
			continue
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", connection.woocommerce_server
		)  # type: ignore
		if not (wc_server.enabled and wc_server.enable_stock_level_synchronisation):
			continue
		if item.disabled and not wc_server.push_zero_stock_for_disabled_items:
			continue

		stock_values = apply_stock_value_hooks(
			item,
			wc_server,
			bins,
			get_woocommerce_stock_values(wc_server, bins, item_disabled=bool(item.disabled)),
		)
		wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
			wc_server.name, connection.woocommerce_id
		)
		try:
			# Variations come back with their parent_id, so the write goes through the parent's endpoint
			wc_product = load_wc_product(wc_product_name)
			changes = wc_product.get_changes(stock_values)
			if changes and wc_product.write(changes):
				record_sync_time(wc_server.name)
		except Exception as err:
			frappe.log_error(
				"WooCommerce Error: Stock Sync",
				f"{frappe.get_traceback()}\n\nProduct: {wc_product_name}\nStock: {stock_values}",
			)
			errors.append(err)

	if errors:
		raise errors[0]
	return True


def get_woocommerce_stock_values(wc_server: WooCommerceServer, bins: list, item_disabled: bool) -> dict:
	"""
	The WooCommerce Product's stock fields for an Item's Bins.

	The quantity is the stock of the server's warehouses, rounded down, as WooCommerce only takes whole
	numbers. The shop's backorder setting is left as it is; another app can change it, or anything else
	in writable_fields, through the woocommerce_stock_values hook.
	"""
	own_qty = 0
	if not item_disabled:
		own_warehouses = get_warehouses_with_descendants(wc_server.warehouses)
		own_qty = sum(
			(row.actual_qty or 0) - ((row.reserved_qty or 0) if wc_server.subtract_reserved_stock else 0)
			for row in bins
			if row.warehouse in own_warehouses
		)
	return {"manage_stock": 1, "stock_quantity": max(math.floor(own_qty), 0)}


def apply_stock_value_hooks(item, wc_server: WooCommerceServer, bins: list, stock_values: dict) -> dict:
	"""
	Let other apps adjust the stock values before they are pushed. Each method listed under the
	`woocommerce_stock_values` hook is called as

		method(item=item, wc_server=wc_server, bins=bins, stock_values=stock_values) -> dict | None

	and returns the values to push, or None to keep them. Only WooCommerce Product writable_fields
	(stock_quantity, stock_status, backorders, ...) reach WooCommerce.
	"""
	for method in frappe.get_hooks("woocommerce_stock_values"):
		result = frappe.get_attr(method)(
			item=item, wc_server=wc_server, bins=bins, stock_values=dict(stock_values)
		)
		if result is not None:
			stock_values = result
	return stock_values


def get_warehouses_with_descendants(rows) -> set[str]:
	"""
	The warehouses in a server's warehouse table, with the warehouses under any group among them
	"""
	warehouses = set()
	for row in rows or []:
		if not row.warehouse:
			continue
		warehouses.add(row.warehouse)
		bounds = frappe.db.get_value("Warehouse", row.warehouse, ["lft", "rgt", "is_group"], as_dict=True)
		if bounds and bounds.is_group:
			warehouses.update(
				frappe.get_all(
					"Warehouse",
					filters={"lft": [">", bounds.lft], "rgt": ["<", bounds.rgt]},
					pluck="name",
				)
			)
	return warehouses
