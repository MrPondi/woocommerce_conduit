# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

import json

import frappe
import frappe.utils
from frappe.model.document import Document

from woocommerce_conduit.woocommerce.woocommerce_api import WooCommerceDocument


class WooCommerceProduct(WooCommerceDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attributes: DF.JSON
		backorders: DF.Literal["no", "notify", "yes"]
		cross_sell_ids: DF.JSON | None
		date_on_sale_from: DF.Datetime | None
		date_on_sale_to: DF.Datetime | None
		description: DF.TextEditor | None
		downloadable: DF.Check
		height: DF.Float
		image: DF.AttachImage | None
		images: DF.JSON
		length: DF.Float
		manage_stock: DF.Check
		on_sale: DF.Check
		parent_id: DF.Int
		permalink: DF.SmallText | None
		price: DF.Currency
		regular_price: DF.Currency
		related_ids: DF.JSON | None
		sale_price: DF.Currency
		shipping_class: DF.Data | None
		shipping_required: DF.Check
		shipping_taxable: DF.Check
		short_description: DF.TextEditor | None
		sku: DF.Data | None
		slug: DF.Data | None
		sold_individually: DF.Check
		status: DF.Literal["draft", "pending", "private", "publish", "trash"]
		stock_quantity: DF.Int
		stock_status: DF.Literal["instock", "outofstock", "onbackorder"]
		total_sales: DF.Int
		upsell_ids: DF.JSON | None
		virtual: DF.Check
		weight: DF.Float
		width: DF.Float
		woocommerce_date_created: DF.Datetime
		woocommerce_date_modified: DF.Datetime
		woocommerce_id: DF.Data
		woocommerce_name: DF.Data
		woocommerce_server: DF.Data
	# end: auto-generated types

	doctype = "WooCommerce Product"
	resource = "products"
	field_setter_map = {"woocommerce_name": "name", "woocommerce_id": "id"}  # noqa: RUF012
	field_getter_map = {  # noqa: RUF012
		"image": ["images"],
		"length": ["dimensions"],
		"width": ["dimensions"],
		"height": ["dimensions"],
		"brand": ["attributes"],
	}
	orderby_map = {  # noqa: RUF012
		"price": "price",
		"total_sales": "popularity",
	}
	# The name and type are used to find and name variations, the parent_id to relate them.
	# date_modified_gmt lets the sync compare against ERPNext's timezone.
	mandatory_wc_fields = [  # noqa: RUF012
		"id",
		"name",
		"type",
		"parent_id",
		"date_created",
		"date_modified",
		"date_modified_gmt",
	]
	# The only fields ERPNext writes to WooCommerce. An Item can be linked to several shops, and each
	# shop keeps its own name, description, images, etc., so everything else is left as the shop has it.
	writable_fields = (
		"regular_price",
		"sale_price",
		"date_on_sale_from",
		"date_on_sale_to",
		"manage_stock",
		"stock_quantity",
		"stock_status",
	)

	def db_insert(self, *args, **kwargs):
		pass

	def load_from_db(self):
		return super().load_from_db()

	def after_load_from_db(self, record: dict):
		images = json.loads(record["images"])
		if len(images) > 0:
			record["image"] = images[0]["src"]
		# Indent the JSON fields so they are readable in the form
		for fieldname in ("images", "attributes"):
			if isinstance(record.get(fieldname), str):
				record[fieldname] = json.dumps(json.loads(record[fieldname]), indent=2, ensure_ascii=False)
		record["length"] = record["dimensions"]["length"]
		record["width"] = record["dimensions"]["width"]
		record["height"] = record["dimensions"]["height"]
		return record

	def get_update_endpoint(self, record_id: int) -> str:
		# Variations are only writable through their parent
		if self.parent_id:
			return f"{self.resource}/{self.parent_id}/variations/{record_id}"
		return super().get_update_endpoint(record_id)

	def before_db_update(self, changes: dict) -> dict:
		"""
		Put the changed fields in the format the WooCommerce API expects
		"""
		changes = {key: value for key, value in changes.items() if key in self.writable_fields}

		if self.get("type") == "variable":
			# A variable product's price lives on its variations
			changes.pop("regular_price", None)
			changes.pop("sale_price", None)
		if "regular_price" in changes:
			changes["regular_price"] = str(changes["regular_price"] or "")
		if "sale_price" in changes:
			# An empty string clears a sale, 0 would set the product on sale for free
			sale_price = frappe.utils.flt(changes["sale_price"])
			changes["sale_price"] = str(sale_price) if sale_price > 0 else ""
		if "stock_quantity" in changes:
			changes["stock_quantity"] = frappe.utils.cint(changes["stock_quantity"])

		for date_field in ("date_on_sale_from", "date_on_sale_to"):
			if date_field in changes:
				value = changes[date_field]
				changes[date_field] = (
					value.strftime("%Y-%m-%dT%H:%M:%S") if hasattr(value, "strftime") else value or None
				)

		return super().before_db_update(changes)

	def delete(self, *args, **kwargs):
		pass

	@staticmethod
	def get_list(args) -> list[Document] | None:
		"""
		Get list of products including their variations with intelligent caching.

		This method works with standard Frappe arguments for virtual DocTypes.
		Results are cached to improve performance for subsequent requests, with
		cache invalidation based on request parameters.

		Args:
			args: Arguments passed by Frappe (filters, limit, etc.)

		Returns:
			List: Products with their variations
		"""
		# Validate input arguments
		if not isinstance(args, dict):
			frappe.log_error("WooCommerce Product Error", "Invalid arguments for get_list")
			return []

		# Extract settings early for consistency
		settings = frappe.get_cached_doc("WooCommerce Settings")
		fetch_variations = getattr(settings, "fetch_variations", True)
		variation_batch_size = getattr(settings, "variation_batch_size", 20)
		max_variations = getattr(settings, "max_variations", 100)
		cache_timeout = getattr(settings, "cache_timeout", 300)  # 5 minutes default

		# Skip cache for specific scenarios
		skip_cache = args.get("skip_cache", False)

		# For single product lookups, skip cache to ensure fresh data
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
				cache_key = f"wc_products_{frappe.utils.cstr(frappe.generate_hash(str(args_for_cache), 16))}"

				# Try to get cached data
				cached_data = cache.get_value(cache_key)
				if cached_data:
					cached_products = json.loads(cached_data)

					# If requesting docs and cache contains dicts, convert to docs
					if args.get("as_doc") and cached_products and isinstance(cached_products[0], dict):
						return [frappe.get_doc(product) for product in cached_products]

					return cached_products
			except Exception as e:
				frappe.log_error("WooCommerce Cache Error", f"WooCommerce cache fetch error: {e!s}")

		# If cache miss or error, fetch from API
		try:
			# Fetch products with normal implementation
			products = WooCommerceProduct.get_list_of_records(args)

			if not products:
				return []

			# If variations not needed, cache and return early
			if not fetch_variations:
				# Cache the results before returning (if caching is enabled)
				if cache and cache_key:
					try:
						cache.set_value(
							cache_key,
							json.dumps(products),
							expires_in_sec=cache_timeout,
						)
					except Exception as e:
						frappe.log_error(
							f"WooCommerce Cache ErrorWooCommerce cache set error: {e!s}",
						)
				return products

			# Filter for variable products that need variations fetched
			variable_products = [
				{
					"id": product.get("id"),
					"woocommerce_name": product.get("woocommerce_name"),
				}
				for product in products
				if product.get("type") == "variable"
			]

			all_variations = []

			# Fetch variations in parallel for each variable product (if supported)
			for product in variable_products:
				product_id = product["id"]
				wc_name = product["woocommerce_name"]

				variation_args = args.copy()
				variation_args["endpoint"] = f"products/{product_id}/variations"
				variation_args["metadata"] = {"parent_woocommerce_name": wc_name}
				variation_args["skip_cache"] = True  # Don't cache intermediate results
				# Needed to build the variation name
				variation_args["extra_wc_fields"] = ["attributes"]

				# Use more efficient pagination with a single request if possible
				variation_args["page_length"] = min(variation_batch_size, max_variations)
				variation_args["max_results"] = max_variations
				variation_args["start"] = 0

				try:
					variations = WooCommerceProduct.get_list_of_records(variation_args)
					if variations:
						all_variations.extend(variations)
				except Exception as e:
					frappe.log_error(
						"WooCommerce Variation Error"
						f"Error fetching variations for product {product_id}: {e!s}",
					)

			# Add all variations to the products list
			products.extend(all_variations)

			# Cache the results before returning (if caching is enabled)
			if cache and cache_key:
				try:
					cache.set_value(cache_key, json.dumps(products), expires_in_sec=cache_timeout)
				except Exception as e:
					frappe.log_error(
						f"WooCommerce Cache ErrorWooCommerce cache set error: {e!s}",
					)

			return products

		except Exception as e:
			frappe.log_error(
				"WooCommerce Product Fetch Error",
				f"WooCommerce product fetch error: {e!s}\n{frappe.get_traceback()}",
			)
			return []

	@classmethod
	def during_get_list_of_records(cls, record: dict, args):
		# In the case of variations
		if record.get("parent_id"):
			# Woocommerce product variantions endpoint results doesn't return the type, so set it manually
			record["type"] = "variation"

			if variation_name := cls.get_variation_name(record, args):
				# Override the woocommerce_name field
				record["woocommerce_name"] = variation_name

		return record

	@staticmethod
	def get_variation_name(product: dict, args):
		# If this is a variation, we expect the variation's parent name in the metadata, then we can
		# build an item name in the format of {parent_name}, {attribute 1}, {attribute n}
		if (
			(product["type"] == "variation")
			and (metadata := args.get("metadata"))
			and (attributes := product.get("attributes"))
			and (parent_wc_name := metadata.get("parent_woocommerce_name"))
		):
			attr_values = [attr["option"] for attr in json.loads(attributes)]
			return parent_wc_name + " - " + ", ".join(attr_values)
		return None

	@staticmethod
	def get_count(args) -> int:
		return WooCommerceProduct.get_count_of_records(args)

	@staticmethod
	def get_stats(args):
		pass
