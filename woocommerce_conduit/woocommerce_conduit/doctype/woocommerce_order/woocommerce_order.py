# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

import json

import frappe
import frappe.utils
from frappe import _dict
from frappe.model.document import Document

from woocommerce_conduit.woocommerce_conduit.woocommerce_api import WooCommerceDocument

WC_ORDER_STATUS_MAPPING = {
	"Pending Payment": "pending",
	"On hold": "on-hold",
	"Failed": "failed",
	"Cancelled": "cancelled",
	"Processing": "processing",
	"Refunded": "refunded",
	"Shipped": "completed",
	"Ready for Pickup": "ready-pickup",
	"Picked up": "pickup",
	"Delivered": "delivered",
	"Processing LP": "processing-lp",
	"Draft": "checkout-draft",
	"Quote Sent": "gplsquote-req",
	"Trash": "trash",
	"Partially Shipped": "partial-shipped",
}
WC_ORDER_STATUS_MAPPING_REVERSE = {v: k for k, v in WC_ORDER_STATUS_MAPPING.items()}


class WooCommerceOrder(WooCommerceDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		_links: DF.JSON
		billing: DF.JSON
		cart_hash: DF.Data | None
		cart_tax: DF.Currency
		coupon_lines: DF.JSON | None
		created_via: DF.Data | None
		currency: DF.Data
		customer_id: DF.Data
		customer_ip_address: DF.Data | None
		customer_note: DF.SmallText | None
		customer_user_agent: DF.SmallText | None
		date_paid: DF.Datetime | None
		discount_tax: DF.Currency
		discount_total: DF.Currency
		fee_lines: DF.JSON | None
		line_items: DF.JSON
		number: DF.Data
		order_key: DF.Data
		parent_id: DF.Data | None
		payment_method: DF.Data | None
		payment_method_title: DF.SmallText | None
		payment_url: DF.SmallText | None
		prices_include_tax: DF.Check
		refunds: DF.JSON | None
		shipping: DF.JSON
		shipping_lines: DF.JSON
		shipping_tax: DF.Currency
		shipping_total: DF.Currency
		status: DF.Literal[
			"pending",
			"on-hold",
			"failed",
			"cancelled",
			"processing",
			"refunded",
			"completed",
			"ready-pickup",
			"pickup",
			"delivered",
			"processing-lp",
			"checkout-draft",
			"gplsquote-req",
			"trash",
		]
		tax_lines: DF.JSON | None
		total: DF.Currency
		total_tax: DF.Currency
		transaction_id: DF.Data | None
		version: DF.Data | None
		woocommerce_date_created: DF.Datetime
		woocommerce_date_modified: DF.Datetime
		woocommerce_id: DF.Data
		woocommerce_server: DF.Data
	# end: auto-generated types

	doctype = "WooCommerce Order"
	resource = "orders"
	field_setter_map = {"woocommerce_name": "name", "woocommerce_id": "id"}  # noqa: RUF012

	def db_insert(self, *args, **kwargs):
		pass

	def load_from_db(self):
		return super().load_from_db()

	def db_update(self):
		pass

	def delete(self):
		return super().delete()

	@staticmethod
	def get_list(args) -> list[_dict | Document] | None:
		"""
		Get list of orderss including their variations with intelligent caching.

		This method works with standard Frappe arguments for virtual DocTypes.
		Results are cached to improve performance for subsequent requests, with
		cache invalidation based on request parameters.

		Args:
			args: Arguments passed by Frappe (filters, limit, etc.)

		Returns:
			List: Orders with their variations
		"""
		# Validate input arguments
		if not isinstance(args, dict):
			frappe.log_error("WooCommerce Order Error", "Invalid arguments for get_list")
			return []

		# Extract settings early for consistency
		settings = frappe.get_cached_doc("WooCommerce Settings")
		cache_timeout = getattr(settings, "cache_timeout", 300)  # 5 minutes default

		# Skip cache for specific scenarios
		skip_cache = args.get("skip_cache", True)

		# For single order lookups, skip cache to ensure fresh data
		if args.get("filters") and any(f[1] == "id" and f[2] == "=" for f in args["filters"]):
			skip_cache = True

		# Generate a cache key based on args, excluding non-serializable items
		if not skip_cache:
			try:
				args_for_cache = {
					k: v
					for k, v in args.copy().items()
					if k not in ["metadata", "as_doc", "servers"] and v is not None
				}

				# Handle filters specially - convert to tuple for hashability
				if "filters" in args_for_cache and isinstance(args_for_cache["filters"], list):
					args_for_cache["filters"] = tuple(
						tuple(f) if isinstance(f, list) else f for f in args_for_cache["filters"]
					)

				# Add server names if specified
				if args.get("servers"):
					args_for_cache["servers"] = tuple(sorted(args["servers"]))

				# Create a deterministic representation for cache key
				cache_key = f"wc_orders_{frappe.utils.cstr(frappe.generate_hash(str(args_for_cache), 16))}"

				# Try to get cached data
				cached_data = frappe.cache().get_value(cache_key)
				if cached_data:
					cached_orders = json.loads(cached_data)

					# If requesting docs and cache contains dicts, convert to docs
					if args.get("as_doc") and cached_orders and isinstance(cached_orders[0], dict):
						return [frappe.get_doc(order) for order in cached_orders]

					return cached_orders
			except Exception as e:
				frappe.log_error("WooCommerce Cache Error", f"WooCommerce cache fetch error: {e!s}")

		# If cache miss or error, fetch from API
		try:
			# Fetch orders with normal implementation
			orders = WooCommerceOrder.get_list_of_records(args)

			if not orders:
				return []

			# Cache the results before returning (if caching is enabled)
			if not skip_cache:
				try:
					frappe.cache().set_value(cache_key, orders, expires_in_sec=cache_timeout)
				except Exception as e:
					frappe.log_error(
						f"WooCommerce Cache ErrorWooCommerce cache set error: {e!s}",
					)
			return orders

		except Exception as e:
			frappe.log_error(
				"WooCommerce Order Fetch Error",
				f"WooCommerce order fetch error: {e!s}\n{frappe.get_traceback()}",
			)
			return []

	@staticmethod
	def get_count(args) -> int:
		return WooCommerceOrder.get_count_of_records(args)

	@staticmethod
	def get_stats(args):
		pass
