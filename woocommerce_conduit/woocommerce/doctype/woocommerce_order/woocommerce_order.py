# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

import json

import frappe
import frappe.utils
import requests
from frappe import _dict
from frappe.model.document import Document
from frappe.utils import flt

from woocommerce_conduit.woocommerce.woocommerce_api import WooCommerceDocument

WC_ORDER_STATUS_MAPPING = {
	"Pending Payment": "pending",
	"On hold": "on-hold",
	"Failed": "failed",
	"Cancelled": "cancelled",
	"Processing": "processing",
	"Refunded": "refunded",
	"Completed": "completed",
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

# WooCommerce nests the addresses as `billing` and `shipping`, the DocType has them as
# `billing_<key>` and `shipping_<key>` fields
ADDRESS_PREFIXES = ("billing", "shipping")
# Prefixed fields that are not part of the nested address
NON_ADDRESS_FIELDS = {"shipping_total", "shipping_tax", "shipping_lines", "billing_map", "shipping_map"}
GEOCODER_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_CACHE_SECONDS = 60 * 60 * 24 * 30


def get_address_data(order, prefix: str) -> dict:
	"""The `billing` or `shipping` dict as WooCommerce has it, from the flattened fields of the order"""
	data = order if isinstance(order, dict) else order.as_dict()
	return {
		key.removeprefix(f"{prefix}_"): value or ""
		for key, value in data.items()
		if key.startswith(f"{prefix}_") and key not in NON_ADDRESS_FIELDS
	}


def geocode_address(address: dict) -> tuple[float, float] | None:
	"""
	Look up the coordinates of an address, caching the result (also when nothing was found)

	Returns:
		(latitude, longitude), or None when the address can't be located
	"""
	query = ", ".join(
		part.strip()
		for part in (
			address.get("address_1"),
			address.get("address_2"),
			f"{address.get('postcode') or ''} {address.get('city') or ''}",
			address.get("state"),
			address.get("country"),
		)
		if part and part.strip()
	)
	if not address.get("address_1") and not address.get("city"):
		return None

	cache_key = f"wc_geocode_{frappe.generate_hash(query, 16)}"
	cached = frappe.cache.get_value(cache_key)
	if cached:
		return tuple(cached) if cached != "not_found" else None

	params = {"format": "jsonv2", "limit": 1, "q": query}
	if country := address.get("country"):
		params["countrycodes"] = country.lower()
	try:
		response = requests.get(
			GEOCODER_URL,
			params=params,
			headers={"User-Agent": "woocommerce_conduit (ERPNext)"},
			timeout=5,
		)
		response.raise_for_status()
		results = response.json()
	except Exception as e:
		# Not cached, so it is retried the next time the order is opened
		frappe.log_error("WooCommerce Geocode Error", f"Geocoding '{query}' failed: {e!s}")
		return None

	if not results:
		frappe.cache.set_value(cache_key, "not_found", expires_in_sec=GEOCODE_CACHE_SECONDS)
		return None

	coordinates = (float(results[0]["lat"]), float(results[0]["lon"]))
	frappe.cache.set_value(cache_key, list(coordinates), expires_in_sec=GEOCODE_CACHE_SECONDS)
	return coordinates


def to_geolocation_value(coordinates: tuple[float, float]) -> str:
	"""The GeoJSON the Geolocation field (leaflet widget) expects. GeoJSON is longitude first."""
	latitude, longitude = coordinates
	return json.dumps(
		{
			"type": "FeatureCollection",
			"features": [
				{
					"type": "Feature",
					"properties": {},
					"geometry": {"type": "Point", "coordinates": [longitude, latitude]},
				}
			],
		}
	)


class WooCommerceOrder(WooCommerceDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		_links: DF.JSON
		billing_address_1: DF.Data | None
		billing_address_2: DF.Data | None
		billing_city: DF.Data | None
		billing_company: DF.Data | None
		billing_country: DF.Data | None
		billing_email: DF.Data | None
		billing_first_name: DF.Data | None
		billing_last_name: DF.Data | None
		billing_map: DF.Geolocation | None
		billing_phone: DF.Data | None
		billing_postcode: DF.Data | None
		billing_state: DF.Data | None
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
		prices_include_tax: DF.Check
		refunds: DF.JSON | None
		shipping_address_1: DF.Data | None
		shipping_address_2: DF.Data | None
		shipping_city: DF.Data | None
		shipping_company: DF.Data | None
		shipping_country: DF.Data | None
		shipping_first_name: DF.Data | None
		shipping_last_name: DF.Data | None
		shipping_lines: DF.JSON
		shipping_map: DF.Geolocation | None
		shipping_phone: DF.Data | None
		shipping_postcode: DF.Data | None
		shipping_state: DF.Data | None
		shipping_tax: DF.Currency
		shipping_total: DF.Currency
		status: DF.Literal["pending", "on-hold", "failed", "cancelled", "processing", "refunded", "completed", "ready-pickup", "pickup", "delivered", "processing-lp", "checkout-draft", "gplsquote-req", "trash"]
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
	field_getter_map = {  # noqa: RUF012
		"billing_map": ["billing"],
		"shipping_map": ["shipping"],
	}

	@classmethod
	def get_wc_fields(cls, frappe_fields=None, extra=None) -> str:
		# The flattened address fields are all requested as the nested `billing` / `shipping`
		wc_fields = super().get_wc_fields(frappe_fields, extra).split(",")
		wc_fields = [cls._to_nested_address_field(field) for field in wc_fields]
		return ",".join(dict.fromkeys(wc_fields))

	@staticmethod
	def _to_nested_address_field(field: str) -> str:
		for prefix in ADDRESS_PREFIXES:
			if field.startswith(f"{prefix}_") and field not in NON_ADDRESS_FIELDS:
				return prefix
		return field

	@classmethod
	def pre_init_document(cls, record: dict, woocommerce_server_url: str):
		# Rebind the keys of `billing` and `shipping` with their prefix
		for prefix in ADDRESS_PREFIXES:
			for key, value in (record.pop(prefix, None) or {}).items():
				record[f"{prefix}_{key}"] = value
		# WooCommerce sends prices as strings ("1058.00"), which the Currency fields would parse
		# with the locale's separators. Numbers are not affected by that.
		for df in frappe.get_meta(cls.doctype).get("fields", {"fieldtype": "Currency"}):
			if isinstance(record.get(df.fieldname), str):
				record[df.fieldname] = flt(record[df.fieldname])
		return super().pre_init_document(record, woocommerce_server_url)

	def after_load_from_db(self, record: dict):
		# Indent the JSON fields so they are readable in the form
		for fieldname in ("line_items", "shipping_lines"):
			if isinstance(record.get(fieldname), str):
				record[fieldname] = json.dumps(json.loads(record[fieldname]), indent=2, ensure_ascii=False)
		# Geocoding is a network call, so only the form gets the maps, not the list
		for prefix in ADDRESS_PREFIXES:
			if coordinates := geocode_address(get_address_data(record, prefix)):
				record[f"{prefix}_map"] = to_geolocation_value(coordinates)
		return record

	def db_insert(self, *args, **kwargs):
		pass

	def load_from_db(self):
		return super().load_from_db()

	def db_update(self):
		pass

	def delete(self, *args, **kwargs):
		pass

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
		cache = None if skip_cache else frappe.cache
		cache_key: str | None = None
		if cache:
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
				cached_data = cache.get_value(cache_key)
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
			if cache and cache_key:
				try:
					cache.set_value(cache_key, json.dumps(orders), expires_in_sec=cache_timeout)
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
