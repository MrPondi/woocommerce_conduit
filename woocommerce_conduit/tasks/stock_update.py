import math

import frappe
from frappe import _

from woocommerce_conduit.tasks.sync import record_sync_time
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


def stock_sync_is_enabled() -> bool:
	"""
	Whether any WooCommerce Server has its stock sync switched on
	"""
	return bool(
		frappe.db.exists("WooCommerce Server", {"enabled": 1, "enable_stock_level_synchronisation": 1})
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

	for item_code in {row.item_code for row in doc.get("items") or [] if row.item_code}:
		enqueue_stock_update(item_code)


def enqueue_stock_update(item_code: str):
	"""
	Push an Item's stock to WooCommerce in the background, once the current transaction is committed.
	Public, so other apps can request a push when something they feed into the stock changes (see the
	woocommerce_stock_values hook).
	"""
	frappe.enqueue(
		"woocommerce_conduit.tasks.stock_update.update_stock_levels_on_woocommerce_site",
		enqueue_after_commit=True,
		item_code=item_code,
	)


def update_stock_levels_for_all_enabled_items_in_background():
	"""
	Push the stock of every Item linked to a WooCommerce Product. A safety net for stock that changed
	without one of the hooked documents.
	"""
	if not stock_sync_is_enabled():
		return

	item_codes = set(
		frappe.get_all(
			"Item WooCommerce Server", filters={"parenttype": "Item", "enable_sync": 1}, pluck="parent"
		)
	)
	for item_code in sorted(item_codes):
		frappe.enqueue(
			"woocommerce_conduit.tasks.stock_update.update_stock_levels_on_woocommerce_site",
			queue="long",
			item_code=item_code,
		)


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
			# Variations come back with their parent_id, so the save goes through the parent's endpoint
			wc_product = load_wc_product(wc_product_name)
			wc_product.update(stock_values)
			# The callers check permissions; whoever submitted a stock document may not have write
			# access to WooCommerce Products
			wc_product.flags.ignore_permissions = True
			wc_product.save()
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
