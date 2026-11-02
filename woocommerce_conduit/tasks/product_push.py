import time
from collections import defaultdict

import frappe
from frappe.query_builder import Criterion
from frappe.utils import cint, create_batch, cstr

from woocommerce_conduit.tasks.sync_items import load_wc_product
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	WC_BATCH_LIMIT,
	generate_woocommerce_record_name_from_domain_and_id,
)


def push_to_products(
	server: str, values: dict[str, dict], error_title: str, delay_per_product: float = 0
) -> int:
	"""
	Write values to the WooCommerce Products of one server in batches, as the daily price and stock pushes
	do. The products are read 100 a request with only the fields a push writes (load_products), and only
	what differs from them is sent to WooCommerce's batch endpoints, WC_BATCH_LIMIT updates a request, as
	WooCommerce dates every save as a change. A product that fails is logged, and the others go on.

	Args:
		values: the values to push to each product, by its id as Item WooCommerce Server has it
		error_title: the title of the Error Logs
		delay_per_product: seconds to wait for each product written, so a full push doesn't flood the shop

	Returns:
		How many products were written
	"""
	products = load_products(server, values, error_title)
	updates = defaultdict(list)
	for woocommerce_id, product_values in values.items():
		if not (product := products.get(woocommerce_id)):
			continue
		changes = product.get_changes(product_values)
		# Shaped as write() shapes it, which leaves out the prices of a variable product, for example
		if changes and (body := product.before_db_update(changes)):
			updates[product.get_batch_endpoint()].append({"id": cint(woocommerce_id), **body})

	written = 0
	for endpoint, endpoint_updates in updates.items():
		for chunk in create_batch(endpoint_updates, WC_BATCH_LIMIT):
			taken = send_updates(server, endpoint, list(chunk), error_title)
			written += taken
			if delay_per_product > 0 and taken and not frappe.in_test:
				time.sleep(delay_per_product * taken)
	return written


def load_products(server: str, values: dict[str, dict], error_title: str) -> dict[str, WooCommerceProduct]:
	"""
	The products of the server that values are for, by id, with only the fields a push writes. A variation
	is read with the others of its parent, which ERPNext knows from its Item's template. A product that its
	list leaves out is read on its own: a variation whose Item isn't a variant (linked by its SKU), or a
	product that was trashed or deleted. One that can't be read is logged and left out.
	"""
	parents = get_variation_parents(server, list(values))
	groups = defaultdict(list)
	for woocommerce_id in values:
		groups[parents.get(woocommerce_id)].append(woocommerce_id)

	products, unread = {}, set()
	for parent_id, ids in groups.items():
		try:
			products.update(
				WooCommerceProduct.load_records(
					server,
					ids,
					list(WooCommerceProduct.writable_fields),
					endpoint=f"products/{parent_id}/variations" if parent_id else None,
				)
			)
		except Exception:
			# Reading them one by one would only ask the failing shop again
			frappe.log_error(error_title, f"{frappe.get_traceback()}\n\nProducts: {get_names(server, ids)}")
			unread.update(ids)

	for woocommerce_id, product_values in values.items():
		if woocommerce_id in products or woocommerce_id in unread:
			continue
		name = generate_woocommerce_record_name_from_domain_and_id(server, woocommerce_id)
		try:
			products[woocommerce_id] = load_wc_product(name)
		except Exception:
			frappe.log_error(
				error_title, f"{frappe.get_traceback()}\n\nProduct: {name}\nValues: {product_values}"
			)
	return products


def get_variation_parents(server: str, woocommerce_ids: list[str]) -> dict[str, str]:
	"""
	The parents of those of these products of the server whose Item is a variant: the product of its
	template in the same shop, by id as text
	"""
	if not woocommerce_ids:
		return {}
	link = frappe.qb.DocType("Item WooCommerce Server")
	item = frappe.qb.DocType("Item")
	template_link = frappe.qb.DocType("Item WooCommerce Server").as_("template_link")
	rows = (  # pyright: ignore[reportCallIssue]
		frappe.qb.from_(link)
		.inner_join(item)
		.on(item.name == link.parent)
		.inner_join(template_link)
		.on(
			(template_link.parent == item.variant_of)
			& (template_link.parenttype == "Item")
			& (template_link.woocommerce_server == link.woocommerce_server)
		)
		.select(link.woocommerce_id, template_link.woocommerce_id.as_("parent_id"))
		.where(
			Criterion.all(
				[
					link.parenttype == "Item",
					link.woocommerce_server == server,
					link.woocommerce_id.isin(woocommerce_ids),
				]
			)
		)
		.run(as_dict=True)
	)
	return {row.woocommerce_id: row.parent_id for row in rows if row.parent_id}


def send_updates(server: str, endpoint: str, updates: list[dict], error_title: str) -> int:
	"""
	Send one batch request, and return how many of its updates WooCommerce took. A product it refused, or
	a request that failed, is logged.
	"""
	try:
		answers = WooCommerceProduct.write_batch(server, endpoint, updates)
	except Exception:
		# The request is logged where it failed. This says which products it held.
		names = get_names(server, [update["id"] for update in updates])
		frappe.log_error(error_title, f"{frappe.get_traceback()}\n\nProducts: {names}")
		return 0

	taken = 0
	for update in updates:
		answer = answers.get(cstr(update["id"])) or {}
		if answer and "error" not in answer:
			taken += 1
			continue
		name = generate_woocommerce_record_name_from_domain_and_id(server, update["id"])
		frappe.log_error(
			error_title,
			f"WooCommerce didn't take the update of {name}: {answer.get('error') or 'no answer'}\n\n"
			f"Update: {update}",
		)
	return taken


def get_names(server: str, woocommerce_ids) -> str:
	return ", ".join(
		generate_woocommerce_record_name_from_domain_and_id(server, woocommerce_id)
		for woocommerce_id in woocommerce_ids
	)
