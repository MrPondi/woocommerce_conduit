import math
from collections import defaultdict

import frappe
from erpnext.stock.doctype.warehouse.warehouse import get_child_warehouses
from frappe import _
from frappe.utils import create_batch, flt

from woocommerce_conduit.tasks.product_push import push_to_products
from woocommerce_conduit.tasks.sync import enqueue_unless_queued, get_sales_units, record_sync_time
from woocommerce_conduit.tasks.sync_items import load_wc_product
from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

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


def update_stock_levels_from_ledger(doc, method=None):
	"""
	Intended to be triggered by the on_submit of a Stock Ledger Entry. ERPNext submits one for every stock
	movement of every document, and one that reverses it when the document is cancelled, so no stock change
	is missed, whichever doctype makes it. Each Item is pushed once the transaction is committed.
	"""
	if frappe.in_test or not doc.item_code:
		return
	enqueue_stock_updates({doc.item_code})


def update_stock_levels_for_woocommerce_item(doc, method=None):
	"""
	Intended to be triggered by the on_submit and on_cancel of a Sales Order. A Sales Order reserves stock
	without a Stock Ledger Entry, and that only counts on a server that subtracts reserved stock.
	"""
	if frappe.in_test or doc.doctype != "Sales Order" or not reserved_stock_is_subtracted():
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
	item_codes = frappe.flags.pop(PENDING_ITEMS_FLAG, None)
	# Checked once a transaction, not for each of its Stock Ledger Entries
	if not item_codes or not stock_sync_is_enabled():
		return
	for item_code in get_linked_items(item_codes):
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
	Background job of the daily push: push the stock of these Items, their products read and written in
	batches (push_to_products). An Item or product that fails is logged, and the others go on.
	"""
	pushes = defaultdict(dict)
	for item_code in item_codes:
		try:
			item = frappe.get_doc("Item", item_code)
			if has_stock_to_sync(item):
				for server, woocommerce_id, stock_values in get_stock_pushes(item):
					pushes[server][woocommerce_id] = stock_values
		except Exception:
			frappe.log_error(
				"WooCommerce Error: Stock Sync", f"{frappe.get_traceback()}\n\nItem: {item_code}"
			)

	for server, values in pushes.items():
		if push_to_products(server, values, "WooCommerce Error: Stock Sync"):
			record_sync_time(server)


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
	if not has_stock_to_sync(item):
		return False

	errors = []
	for server, woocommerce_id, stock_values in get_stock_pushes(item):
		wc_product_name = generate_woocommerce_record_name_from_domain_and_id(server, woocommerce_id)
		try:
			# Variations come back with their parent_id, so the write goes through the parent's endpoint
			wc_product = load_wc_product(wc_product_name)
			changes = wc_product.get_changes(stock_values)
			if changes and wc_product.write(changes):
				record_sync_time(server)
		except Exception as err:
			frappe.log_error(
				"WooCommerce Error: Stock Sync",
				f"{frappe.get_traceback()}\n\nProduct: {wc_product_name}\nStock: {stock_values}",
			)
			errors.append(err)

	if errors:
		raise errors[0]
	return True


def has_stock_to_sync(item) -> bool:
	# A template's stock lives on its variants, as in WooCommerce
	return bool(item.get("woocommerce_servers") and item.is_stock_item and not item.has_variants)


def get_stock_pushes(item) -> list[tuple[str, str, dict]]:
	"""
	The stock to push for an Item, as (server, product id, stock values), for each shop it is linked to that
	has stock sync on
	"""
	bins = frappe.get_all(
		"Bin", filters={"item_code": item.name}, fields=["warehouse", "actual_qty", "reserved_qty"]
	)

	pushes = []
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

		sales_unit = (
			get_sales_units([item.name]).get(item.name) if wc_server.get("sync_in_sales_uom") else None
		)
		stock_values = apply_stock_value_hooks(
			item,
			wc_server,
			bins,
			get_woocommerce_stock_values(
				wc_server,
				bins,
				item_disabled=bool(item.disabled),
				conversion_factor=sales_unit.factor if sales_unit else 1.0,
			),
		)
		pushes.append((wc_server.name, connection.woocommerce_id, stock_values))
	return pushes


def get_woocommerce_stock_values(
	wc_server: WooCommerceServer, bins: list, item_disabled: bool, conversion_factor: float = 1.0
) -> dict:
	"""
	The WooCommerce Product's stock fields for an Item's Bins.

	The quantity is the stock of the server's warehouses, rounded down, as WooCommerce only takes whole
	numbers. It is in the Stock UOM, or in Sales UOMs of `conversion_factor` Stock UOMs each. The shop's backorder setting is left as it is; another app can change it, or anything else
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
	# Rounded to six places first, so 3 / 0.1 is 30 and not 29.999...
	return {"manage_stock": 1, "stock_quantity": max(math.floor(flt(own_qty / conversion_factor, 6)), 0)}


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
		if row.warehouse:
			# ERPNext's own tree walk, cached for the request
			warehouses.update(get_child_warehouses(row.warehouse))
	return warehouses
