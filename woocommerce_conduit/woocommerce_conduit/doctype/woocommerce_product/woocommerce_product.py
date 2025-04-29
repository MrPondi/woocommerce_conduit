# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

import json

import frappe
import frappe.utils
from frappe.model.document import Document

from woocommerce_conduit.woocommerce_conduit.woocommerce_api import WooCommerceDocument


class WooCommerceProduct(WooCommerceDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attributes: DF.JSON
		average_rating: DF.Rating
		backordered: DF.Check
		backorders: DF.Literal["no", "notify", "yes"]
		backorders_allowed: DF.Check
		brand: DF.Data | None
		catalog_visibility: DF.Literal["visible", "catalog", "search", "hidden"]
		cross_sell_ids: DF.JSON | None
		date_on_sale_from: DF.Datetime | None
		date_on_sale_to: DF.Datetime | None
		description: DF.TextEditor | None
		download_expiry: DF.Int
		download_limit: DF.Int
		downloadable: DF.Check
		downloads: DF.JSON | None
		featured: DF.Check
		height: DF.Int
		image: DF.AttachImage | None
		images: DF.JSON
		length: DF.Int
		low_stock_amount: DF.Int
		manage_stock: DF.Check
		on_sale: DF.Check
		parent_id: DF.Int
		permalink: DF.SmallText | None
		price: DF.Currency
		purchasable: DF.Check
		rating_count: DF.Int
		regular_price: DF.Currency
		related_ids: DF.JSON | None
		reviews_allowed: DF.Check
		sale_price: DF.Currency
		shipping_class: DF.Data | None
		shipping_class_id: DF.Data | None
		shipping_required: DF.Check
		shipping_taxable: DF.Check
		short_description: DF.TextEditor | None
		sku: DF.Data | None
		slug: DF.Data | None
		sold_individually: DF.Check
		status: DF.Literal["draft", "pending", "private", "publish", "trash"]
		stock_quantity: DF.Data | None
		stock_status: DF.Literal["instock", "outofstock", "onbackorder"]
		tax_class: DF.Data | None
		tax_status: DF.Literal["taxable", "shipping", "none"]
		total_sales: DF.Int
		type: DF.Literal["simple", "grouped", "external", "variable", "variation", "pw-gift-card"]
		upsell_ids: DF.JSON | None
		virtual: DF.Check
		weight: DF.Float
		width: DF.Int
		woocommerce_date_created: DF.Datetime
		woocommerce_date_modified: DF.Datetime
		woocommerce_id: DF.Data
		woocommerce_name: DF.Data
		woocommerce_server: DF.Data
	# end: auto-generated types

	doctype = "WooCommerce Product"
	resource = "products"
	field_setter_map = {"woocommerce_name": "name", "woocommerce_id": "id"}  # noqa: RUF012

	def db_insert(self, *args, **kwargs):
		pass

	def load_from_db(self):
		return super().load_from_db()

	def after_load_from_db(self, product: dict):
		images = json.loads(product["images"])
		if len(images) > 0:
			product["image"] = images[0]["src"]
		product["length"] = product["dimensions"]["length"]
		product["width"] = product["dimensions"]["width"]
		product["height"] = product["dimensions"]["height"]
		product["average_rating"] = round(float(product["average_rating"]) * 0.2, 1)
		attributes = json.loads(product["attributes"])
		for attribute in attributes:
			if attribute["slug"] == "pa_producent":
				product["brand"] = attribute["options"][0]
				break
		return product

	def db_update(self):
		pass

	def delete(self):
		return super().delete()

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
				cache_key = f"wc_products_{frappe.utils.cstr(frappe.generate_hash(str(args_for_cache), 16))}"

				# Try to get cached data
				cached_data = frappe.cache().get_value(cache_key)
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
				if not skip_cache:
					try:
						frappe.cache().set_value(
							cache_key,
							products,
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
			if not skip_cache:
				try:
					frappe.cache().set_value(cache_key, json.dumps(products), expires_in_sec=cache_timeout)
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
	def during_get_list_of_records(cls, product: dict, args):
		# In the case of variations
		if product.get("parent_id"):
			# Woocommerce product variantions endpoint results doesn't return the type, so set it manually
			product["type"] = "variation"

			if variation_name := cls.get_variation_name(product, args):
				# Override the woocommerce_name field
				product["woocommerce_name"] = variation_name

		return product

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
	def get_count(*args) -> int:
		return WooCommerceProduct.get_count_of_records(args)

	@staticmethod
	def get_stats(*args):
		pass
