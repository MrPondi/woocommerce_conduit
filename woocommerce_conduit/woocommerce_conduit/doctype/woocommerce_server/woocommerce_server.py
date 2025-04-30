# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

from urllib.parse import urlparse, urlunparse

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.caching import redis_cache
from jsonpath_ng.ext import parse

from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_order.woocommerce_order import (
	WC_ORDER_STATUS_MAPPING,
)
from woocommerce_conduit.woocommerce_conduit.woocommerce_api import WooCommerceAPI


class WooCommerceServer(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_server_item_field.woocommerce_server_item_field import (
			WooCommerceServerItemField,
		)
		from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
			WooCommerceServerOrderStatus,
		)
		from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_server_shipping_rule.woocommerce_server_shipping_rule import (
			WooCommerceServerShippingRule,
		)

		api_consumer_key: DF.Data
		api_consumer_secret: DF.Data
		company: DF.Link
		creation_user: DF.Link
		delivery_after_days: DF.Int
		enable_image_sync: DF.Check
		enabled: DF.Check
		enabled_order_status: DF.Check
		enabled_payments_sync: DF.Check
		enabled_price_list: DF.Check
		enabled_shipping_methods: DF.Check
		enabled_sync: DF.Check
		freight_and_forwarding_account: DF.Link
		item_field_map: DF.Table[WooCommerceServerItemField]
		item_group: DF.Link
		last_sync_time: DF.Datetime | None
		name_by: DF.Literal["WooCommerce ID", "Product SKU"]
		payment_method_bank_account_mapping: DF.JSON
		payment_method_gl_account_mapping: DF.JSON
		price_list: DF.Link
		sales_order_status_map: DF.Table[WooCommerceServerOrderStatus]
		sales_taxes_and_charges_template: DF.Link | None
		shipping_rule_map: DF.Table[WooCommerceServerShippingRule]
		submit_sales_orders: DF.Check
		sync_so_items_to_wc: DF.Check
		tax_account: DF.Link | None
		uom: DF.Link
		use_actual_tax_type: DF.Check
		warehouse: DF.Link
		woocommerce_server_url: DF.Data

	# end: auto-generated types
	def autoname(self):
		"""
		Set name from woocommerce_server_url field
		"""
		parsed_url = urlparse(self.woocommerce_server_url)

		if not parsed_url.netloc and not parsed_url.scheme:
			parsed_url = urlparse(f"https://{self.woocommerce_server_url}")

		if parsed_url.netloc:
			self.name = parsed_url.netloc

	def validate(self):
		self.validate_woocommerce_url()
		self.test_api_credentials()
		self.validate_so_status_map()
		self.validate_item_map()

	def test_api_credentials(self):
		wcapi = WooCommerceAPI(
			url=self.woocommerce_server_url,
			consumer_key=self.api_consumer_key,
			consumer_secret=self.api_consumer_secret,
			version="wc/v3",
		)
		res = wcapi.get("system_status", params={"_fields": "environment"})

		if res.status_code != 200:
			frappe.throw(_("WooCommerce API credentials are not valid"))

	def validate_woocommerce_url(self):
		parsed_url = urlparse(self.woocommerce_server_url)

		if not parsed_url.netloc:
			if not parsed_url.scheme:
				parsed_url = urlparse(f"https://{self.woocommerce_server_url}")

			if not parsed_url.netloc:
				frappe.throw(_("Please enter a valid WooCommerce Server URL"))

		url_scheme = parsed_url.scheme if parsed_url.scheme in ["http", "https"] else "https"
		self.woocommerce_server_url = urlunparse((url_scheme, parsed_url.netloc, "", "", "", ""))

	def validate_so_status_map(self):
		"""
		Validate Sales Order Status Map to have unique mappings
		"""
		erpnext_so_statuses = [map.erpnext_sales_order_status for map in self.sales_order_status_map]
		if len(erpnext_so_statuses) != len(set(erpnext_so_statuses)):
			frappe.throw(_("Duplicate ERPNext Sales Order Statuses found in Sales Order Status Map"))

		wc_so_statuses = [map.woocommerce_sales_order_status for map in self.sales_order_status_map]
		if len(wc_so_statuses) != len(set(wc_so_statuses)):
			frappe.throw(_("Duplicate WooCommerce Sales Order Statuses found in Sales Order Status Map"))

	def validate_item_map(self):
		"""
		Validate Item Map to have valid JSONPath expressions
		"""
		disallowed_fields = ["attributes"]

		# If the built-in image sync is enabled, disallow the image field in the item field map to avoid unexpected behavior
		if self.enable_image_sync:
			disallowed_fields.append("images")

		if self.item_field_map:
			for map in self.item_field_map:
				jsonpath_expr = map.woocommerce_field_name
				try:
					parse(jsonpath_expr)
				except Exception as e:
					frappe.throw(
						_("Invalid JSONPath syntax in Item Field Map Row {0}:<br><br><pre>{1}</pre>").format(
							map.idx, e
						)
					)

				for field in disallowed_fields:
					if field in jsonpath_expr:
						frappe.throw(_("Field '{0}' is not allowed in JSONPath expression").format(field))

	@frappe.whitelist()
	@redis_cache(ttl=600)
	def get_item_docfields(self):
		"""
		Get a list of DocFields for the Item Doctype
		"""
		invalid_field_types = [
			"Column Break",
			"Fold",
			"Heading",
			"Read Only",
			"Section Break",
			"Tab Break",
			"Table",
			"Table MultiSelect",
		]
		docfields = frappe.get_all(
			"DocField",
			fields=["label", "name", "fieldname"],
			filters=[["fieldtype", "not in", invalid_field_types], ["parent", "=", "Item"]],
		)
		custom_fields = frappe.get_all(
			"Custom Field",
			fields=["label", "name", "fieldname"],
			filters=[["fieldtype", "not in", invalid_field_types], ["dt", "=", "Item"]],
		)
		return docfields + custom_fields

	@frappe.whitelist()
	@redis_cache(ttl=86400)
	def get_woocommerce_order_status_list(self) -> list[str]:
		"""
		Retrieve list of WooCommerce Order Statuses
		"""
		return [key for key in WC_ORDER_STATUS_MAPPING.keys()]
