"""
Which products of a shop's recent orders have no ERPNext Item yet, so they can be linked to an existing
Item or created on purpose before the order sync is unpaused. An order whose product has no Item still
gets one made for it by the sync; this is for keeping the Items clean.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from woocommerce_conduit.tasks.order_lines import find_item_code, get_line_items, get_product_id
from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
from woocommerce_conduit.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

# The most orders one check reads, fetched 100 per request
MAX_ORDERS_TO_CHECK = 2000


@frappe.whitelist()
def get_unlinked_order_products(woocommerce_server: str, order_count: int = 200) -> list[dict]:
	"""
	The products in the newest `order_count` orders of a server that no enabled Item is linked to, most
	ordered first, each with an Item that looks like it (same code or barcode as the SKU) if there is one
	"""
	frappe.get_doc("WooCommerce Server", woocommerce_server).check_permission("read")
	order_count = min(max(cint(order_count), 1), MAX_ORDERS_TO_CHECK)

	products: dict[str, dict] = {}
	for order in get_recent_orders(woocommerce_server, order_count):
		if order.get("status") == "checkout-draft":
			continue
		seen_in_order = set()
		for line in get_line_items(frappe._dict(order)):
			product_id = get_product_id(line)
			# Deleted in WooCommerce, so there is nothing to link
			if product_id == "0":
				continue
			product = products.setdefault(
				product_id,
				{
					"product_id": product_id,
					"woocommerce_product_name": generate_woocommerce_record_name_from_domain_and_id(
						woocommerce_server, product_id
					),
					"name": line.get("name"),
					"sku": cstr(line.get("sku")),
					"orders": 0,
					"qty": 0,
					"last_ordered": order.get("woocommerce_date_created"),
				},
			)
			product["qty"] += flt(line.get("quantity"))
			if product_id not in seen_in_order:
				product["orders"] += 1
				seen_in_order.add(product_id)

	unlinked = [
		product
		for product_id, product in products.items()
		if not find_item_code(woocommerce_server, product_id)
	]
	for product in unlinked:
		product["suggested_item"] = suggest_item(woocommerce_server, product["sku"])
	return sorted(unlinked, key=lambda product: (-product["orders"], -product["qty"]))


def get_recent_orders(woocommerce_server: str, order_count: int) -> list[dict]:
	"""
	The newest orders of the server, with their lines. Raises when the shop can't be reached.
	"""
	return WooCommerceOrder.get_list_of_records(
		{
			"doctype": "WooCommerce Order",
			"fields": ["name", "status", "line_items", "woocommerce_date_created"],
			"servers": [woocommerce_server],
			"page_length": order_count,
			"strict": True,
		}
	)


def suggest_item(woocommerce_server: str, sku: str) -> str | None:
	"""
	An enabled Item whose code or barcode is the SKU, and that isn't linked to a product of this server yet
	"""
	if not sku:
		return None

	candidates = [sku] if frappe.db.exists("Item", {"name": sku, "disabled": 0}) else []
	candidates += frappe.get_all(
		"Item Barcode", filters={"barcode": sku, "parenttype": "Item"}, pluck="parent", order_by="idx"
	)
	for item_code in dict.fromkeys(candidates):
		if frappe.db.get_value("Item", item_code, "disabled"):
			continue
		if frappe.db.exists(
			"Item WooCommerce Server",
			{"parenttype": "Item", "parent": item_code, "woocommerce_server": woocommerce_server},
		):
			continue
		return item_code
	return None


@frappe.whitelist(methods=["POST"])
def link_product_to_item(woocommerce_server: str, product_id: str, item_code: str) -> None:
	"""
	Link a product of a shop to an Item that exists, instead of having the sync make a new one for it
	"""
	product_id = cstr(product_id)
	linked_item = find_item_code(woocommerce_server, product_id)
	if linked_item:
		frappe.throw(
			_("Product {0} is linked to Item {1} already").format(product_id, frappe.bold(linked_item))
		)

	item = frappe.get_doc("Item", item_code)
	item.check_permission("write")
	item.append(
		"woocommerce_servers",
		{"woocommerce_server": woocommerce_server, "woocommerce_id": product_id, "enable_sync": 1},
	)
	item.save()
