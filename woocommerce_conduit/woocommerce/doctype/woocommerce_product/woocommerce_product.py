# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

import json

import frappe
import frappe.utils

from woocommerce_conduit.woocommerce.woocommerce_api import WooCommerceDocument


class WooCommerceProduct(WooCommerceDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attributes: DF.JSON
		backorders: DF.Literal["no", "notify", "yes"]
		brands: DF.JSON | None
		categories: DF.JSON | None
		cross_sell_ids: DF.JSON | None
		date_on_sale_from: DF.Datetime | None
		date_on_sale_to: DF.Datetime | None
		description: DF.TextEditor | None
		downloadable: DF.Check
		global_unique_id: DF.Data | None
		height: DF.Float
		image: DF.AttachImage | None
		images: DF.JSON
		length: DF.Float
		manage_stock: DF.Check
		meta_data: DF.JSON | None
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
		tags: DF.JSON | None
		tax_class: DF.Data | None
		tax_status: DF.Data | None
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
		"backorders",
	)

	@classmethod
	def pre_init_document(cls, record: dict, woocommerce_server_url: str):
		# A variation has one `image` where a product has a list of `images`
		if "images" not in record and "image" in record:
			image = record.pop("image")
			record["images"] = [image] if image else []
		# The first image is the picture of the product, also in the list view
		if images := record.get("images"):
			record["image"] = images[0].get("src")
		return super().pre_init_document(record, woocommerce_server_url)

	def after_load_from_db(self, record: dict):
		# Indent the JSON fields so they are readable in the form
		for fieldname in ("images", "attributes", "categories", "tags", "brands", "meta_data"):
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

	def get_batch_endpoint(self) -> str:
		if self.parent_id:
			return f"{self.resource}/{self.parent_id}/variations/batch"
		return super().get_batch_endpoint()

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

	@staticmethod
	def get_list(args) -> list[dict]:
		"""
		The products of all enabled servers (list and report views), followed by the variations of the
		variable ones when WooCommerce Settings asks for them
		"""
		products = WooCommerceProduct.get_list_of_records(args)
		settings = frappe.get_cached_doc("WooCommerce Settings")
		if not products or not settings.fetch_variations:
			return products

		max_variations = frappe.utils.cint(settings.max_variations) or 100
		variations = []
		for product in products:
			if product.get("type") != "variable":
				continue

			variation_args = {
				"doctype": args.get("doctype"),
				"fields": args.get("fields"),
				# The parent's own server: another shop's product with the same id is another product
				"servers": [product["woocommerce_server"]],
				"endpoint": f"products/{product['id']}/variations",
				"metadata": {"parent_woocommerce_name": product.get("woocommerce_name")},
				# The attributes build the variation name; a variation has one image, not a list
				"extra_wc_fields": ["attributes", "image"],
				"page_length": max_variations,
				"per_page": frappe.utils.cint(settings.variation_batch_size) or 20,
			}

			try:
				variations.extend(WooCommerceProduct.get_list_of_records(variation_args))
			except Exception as e:
				frappe.log_error(
					"WooCommerce Variation Error",
					f"Error fetching variations for product {product['id']}: {e!s}",
				)

		return products + variations

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
