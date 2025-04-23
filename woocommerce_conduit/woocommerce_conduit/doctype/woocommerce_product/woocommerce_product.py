# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

import json

import frappe
import frappe.utils

from woocommerce_conduit.woocommerce_conduit.woocommerce_api import WooCommerceDocument


class WooCommerceProduct(WooCommerceDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		average_rating: DF.Float
		backordered: DF.Check
		backorders: DF.Literal["no", "notify", "yes"]
		backorders_allowed: DF.Check
		brand: DF.Data | None
		catalog_visibility: DF.Literal["visible", "catalog", "search", "hidden"]
		cross_sell_ids: DF.JSON | None
		date_created: DF.Datetime | None
		date_modified: DF.Datetime | None
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
		woocommerce_id: DF.Data | None
		woocommerce_name: DF.Data | None
		woocommerce_server: DF.Data | None
	# end: auto-generated types

	doctype = "WooCommerce Product"
	resource = "products"
	field_setter_map = {"woocommerce_name": "name", "woocommerce_id": "id"}  # noqa: RUF012

	def db_insert(self, *args, **kwargs):
		pass

	def db_update(self):
		pass

	def after_load_from_db(self, product: dict):
		product["image"] = product["images"][0]["src"]
		product["length"] = product["dimensions"]["length"]
		product["width"] = product["dimensions"]["width"]
		product["height"] = product["dimensions"]["height"]
		return product

	@staticmethod
	def get_list(args):
		"""
		Get list of products including their variations with caching

		This method works with standard Frappe arguments for virtual DocTypes.
		Results are cached to improve performance for subsequent requests.

		Args:
			args: Arguments passed by Frappe (filters, limit, etc.)

		Returns:
			List: Products with their variations
		"""
		# Generate a cache key based on args, excluding non-serializable items
		args_for_cache = args.copy()

		# Remove non-serializable items from cache key generation
		if "metadata" in args_for_cache:
			args_for_cache["metadata"] = {
				k: v
				for k, v in args_for_cache["metadata"].items()
				if isinstance(v, str | int | float | bool | list | dict)
			}

		# Create a deterministic representation for cache key
		cache_key = f"wc_products_{frappe.utils.cstr(frappe.generate_hash(args_for_cache, 16))}"

		# Try to get cached data
		try:
			cached_data = frappe.cache().get_value(cache_key)
			if cached_data:
				return json.loads(cached_data)
		except Exception as e:
			frappe.log_error(f"WooCommerce cache fetch error: {e!s}", "WooCommerce Cache Error")

		# If cache miss or error, fetch from API
		try:
			# Fetch products with normal implementation
			products = WooCommerceProduct.get_list_of_records(args)

			if not products:
				return []

			# Extract config from WooCommerce Settings
			settings = frappe.get_cached_doc("WooCommerce Settings")
			fetch_variations = getattr(settings, "fetch_variations", True)
			variation_batch_size = getattr(settings, "variation_batch_size", 20)
			max_variations = getattr(settings, "max_variations", 100)
			cache_timeout = getattr(settings, "cache_timeout", 300)  # 5 minutes default

			if not fetch_variations:
				# Cache the results before returning
				try:
					frappe.cache().set_value(cache_key, json.dumps(products), expires_in_sec=cache_timeout)
				except Exception as e:
					frappe.log_error(f"WooCommerce cache set error: {e!s}", "WooCommerce Cache Error")
				return products

			# Filter for variable products that need variations fetched
			variable_products = [
				{"id": product.get("id"), "woocommerce_name": product.get("woocommerce_name")}
				for product in products
				if product.get("type") == "variable"
			]

			all_variations = []

			# Fetch variations for each variable product
			for product in variable_products:
				product_id = product["id"]
				wc_name = product["woocommerce_name"]

				variation_args = args.copy()
				variation_args["endpoint"] = f"products/{product_id}/variations"
				variation_args["metadata"] = {"parent_woocommerce_name": wc_name}

				# Use standard pagination parameters that Frappe would use
				# But control the total number fetched
				variations_fetched = 0
				offset = 0

				while variations_fetched < max_variations:
					batch_args = variation_args.copy()
					batch_args["page_length"] = variation_batch_size
					batch_args["start"] = offset

					variations = WooCommerceProduct.get_list_of_records(batch_args)
					if not variations:
						break

					all_variations.extend(variations)
					variations_fetched += len(variations)

					if len(variations) < variation_batch_size:
						break

					offset += variation_batch_size

			# Add all variations to the products list
			products.extend(all_variations)

			# Cache the results before returning
			try:
				frappe.cache().set_value(cache_key, json.dumps(products), expires_in_sec=cache_timeout)
			except Exception as e:
				frappe.log_error(f"WooCommerce cache set error: {e!s}", "WooCommerce Cache Error")

			return products

		except Exception as e:
			frappe.log_error(
				f"WooCommerce product fetch error: {e!s}\n{frappe.get_traceback()}",
				"WooCommerce Product Fetch Error",
			)
			return []

	@classmethod
	def during_get_list_of_records(cls, product: dict, args):
		# In the case of variations
		if "parent_id" in product:
			# Woocommerce product variantions endpoint results doesn't return the type, so set it manually
			product["type"] = "variation"

			if variation_name := cls.get_variation_name(product, args):
				# Set the name in args, for use by set_title()
				args["metadata"]["woocommerce_name"] = variation_name

				# Override the woocommerce_name field
				product = cls.override_woocommerce_name(product, variation_name)

		return product

	@staticmethod
	def override_woocommerce_name(product: dict, name: str):
		product["woocommerce_name"] = name
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
