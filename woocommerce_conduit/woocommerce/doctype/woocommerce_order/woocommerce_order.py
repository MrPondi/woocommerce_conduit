# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.utils import cstr, flt

from woocommerce_conduit.woocommerce.woocommerce_api import WooCommerceDocument

# WooCommerce nests the addresses as `billing` and `shipping`, the DocType has them as
# `billing_<key>` and `shipping_<key>` fields
ADDRESS_PREFIXES = ("billing", "shipping")
# Prefixed fields that are not part of the nested address
NON_ADDRESS_FIELDS = {"shipping_total", "shipping_tax", "shipping_lines"}
# Indented in the form, so they can be read
JSON_FIELDS = ("line_items", "shipping_lines", "fee_lines", "tax_lines", "coupon_lines", "refunds")


def get_address_data(order, prefix: str) -> dict:
	"""The `billing` or `shipping` dict as WooCommerce has it, from the flattened fields of the order"""
	data = order if isinstance(order, dict) else order.as_dict()
	return {
		key.removeprefix(f"{prefix}_"): value or ""
		for key, value in data.items()
		if key.startswith(f"{prefix}_") and key not in NON_ADDRESS_FIELDS
	}


class WooCommerceOrder(WooCommerceDocument):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		billing_address_1: DF.Data | None
		billing_address_2: DF.Data | None
		billing_city: DF.Data | None
		billing_company: DF.Data | None
		billing_country: DF.Data | None
		billing_email: DF.Data | None
		billing_first_name: DF.Data | None
		billing_last_name: DF.Data | None
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
		date_completed: DF.Datetime | None
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
		sales_order: DF.Link | None
		shipping_address_1: DF.Data | None
		shipping_address_2: DF.Data | None
		shipping_city: DF.Data | None
		shipping_company: DF.Data | None
		shipping_country: DF.Data | None
		shipping_first_name: DF.Data | None
		shipping_last_name: DF.Data | None
		shipping_lines: DF.JSON
		shipping_phone: DF.Data | None
		shipping_postcode: DF.Data | None
		shipping_state: DF.Data | None
		shipping_tax: DF.Currency
		shipping_total: DF.Currency
		status: DF.Autocomplete
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
	field_setter_map = {"woocommerce_id": "id"}  # noqa: RUF012
	# The only fields ERPNext writes to WooCommerce
	writable_fields = ("status",)
	# Worked out here, not asked of WooCommerce
	field_getter_map = {"sales_order": []}  # noqa: RUF012
	orderby_map = {"modified": "modified", "woocommerce_date_modified": "modified"}  # noqa: RUF012

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
		for fieldname in JSON_FIELDS:
			if isinstance(record.get(fieldname), str):
				record[fieldname] = json.dumps(json.loads(record[fieldname]), indent=2, ensure_ascii=False)
		return record

	def onload(self):
		"""
		What the order is in ERPNext: its Sales Order, and the Item each line is linked to
		"""
		# Imported here, as the sync imports this module
		from woocommerce_conduit.tasks.order_lines import find_item_code, get_line_items, get_product_id

		self.sales_order = frappe.db.get_value(
			"Sales Order",
			{"woocommerce_server": self.woocommerce_server, "woocommerce_id": cstr(self.woocommerce_id)},
			"name",
			order_by="docstatus asc, creation desc",
		)
		product_ids = {get_product_id(line) for line in get_line_items(self)}
		self.set_onload(
			"item_codes",
			{product_id: find_item_code(self.woocommerce_server, product_id) for product_id in product_ids},
		)

	def on_update(self):
		# The status was changed in the form and written to WooCommerce, the Sales Order follows. A status
		# pushed from the Sales Order is there already.
		if self.has_value_changed("status") and not self.flags.created_by_sync:
			from woocommerce_conduit.tasks.sync_sales_orders import enqueue_sales_order_sync

			enqueue_sales_order_sync(woocommerce_order_name=self.name)

	def before_db_update(self, changes: dict) -> dict:
		"""
		Only the status is written to WooCommerce, the rest of an order is the shop's
		"""
		return super().before_db_update(
			{key: value for key, value in changes.items() if key in self.writable_fields}
		)

	@staticmethod
	def get_list(args) -> list[dict]:
		"""
		The orders of all enabled servers, for the list and report views
		"""
		return WooCommerceOrder.get_list_of_records(args)

	@staticmethod
	def get_count(args) -> int:
		return WooCommerceOrder.get_count_of_records(args)


@frappe.whitelist()
def get_status_options(woocommerce_server: str) -> list[dict]:
	"""
	The statuses of a server's Order Status Map, as suggestions for the status of an order
	"""
	server = frappe.get_cached_doc("WooCommerce Server", woocommerce_server)
	server.check_permission("read")
	return [
		{"value": row.woocommerce_status, "label": row.woocommerce_status_label or row.woocommerce_status}
		for row in server.sales_order_status_map
	]
