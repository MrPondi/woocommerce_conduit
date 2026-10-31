# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

from urllib.parse import urlparse, urlunparse

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr
from frappe.utils.caching import redis_cache
from jsonpath_ng.ext import parse

from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	PAYMENT_OWNED_STATUSES,
	get_default_status_rows,
	normalise_status,
)
from woocommerce_conduit.woocommerce.woocommerce_api import WooCommerceAPI


def normalise_server_url(url: str | None) -> str | None:
	"""
	The shop's URL as scheme and host, `https` when none is given. None when there is no host in it.
	"""
	parsed_url = urlparse(cstr(url).strip())
	if not parsed_url.netloc and not parsed_url.scheme:
		parsed_url = urlparse(f"https://{cstr(url).strip()}")
	if not parsed_url.netloc:
		return None

	url_scheme = parsed_url.scheme if parsed_url.scheme in ("http", "https") else "https"
	return urlunparse((url_scheme, parsed_url.netloc, "", "", "", ""))


def get_mode_of_payment_account(mode_of_payment: str, company: str) -> str | None:
	"""
	The account a payment with this Mode of Payment is booked on for the company
	"""
	return frappe.db.get_value(
		"Mode of Payment Account", {"parent": mode_of_payment, "company": company}, "default_account"
	)


class WooCommerceServer(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from woocommerce_conduit.woocommerce.doctype.woocommerce_server_item_field.woocommerce_server_item_field import (
			WooCommerceServerItemField,
		)
		from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
			WooCommerceServerOrderStatus,
		)
		from woocommerce_conduit.woocommerce.doctype.woocommerce_server_shipping_rule.woocommerce_server_shipping_rule import (
			WooCommerceServerShippingRule,
		)
		from woocommerce_conduit.woocommerce.doctype.woocommerce_server_warehouse.woocommerce_server_warehouse import (
			WooCommerceServerWarehouse,
		)

		api_consumer_key: DF.Password
		api_consumer_secret: DF.Password
		company: DF.Link
		creation_user: DF.Link
		delivery_after_days: DF.Int
		enable_image_sync: DF.Check
		enable_price_list_sync: DF.Check
		enable_sales_price_list_sync: DF.Check
		enable_stock_level_synchronisation: DF.Check
		enabled: DF.Check
		enabled_order_status: DF.Check
		enabled_payments_sync: DF.Check
		enabled_shipping_methods: DF.Check
		freight_and_forwarding_account: DF.Link
		item_field_map: DF.Table[WooCommerceServerItemField]
		item_group: DF.Link
		last_sync_time: DF.Datetime | None
		name_by: DF.Literal["WooCommerce ID", "Product SKU"]
		payment_method_bank_account_mapping: DF.JSON
		payment_method_gl_account_mapping: DF.JSON
		price_list: DF.Link | None
		price_list_delay_per_item: DF.Float
		push_zero_stock_for_disabled_items: DF.Check
		sales_order_status_map: DF.Table[WooCommerceServerOrderStatus]
		sales_price_list: DF.Link | None
		sales_taxes_and_charges_template: DF.Link | None
		secret: DF.SmallText | None
		shipping_rule_map: DF.Table[WooCommerceServerShippingRule]
		subtract_reserved_stock: DF.Check
		sync_prices_for_disabled_items: DF.Check
		sync_so_items_to_wc: DF.Check
		tax_account: DF.Link | None
		uom: DF.Link
		use_actual_tax_type: DF.Check
		warehouse: DF.Link
		warehouses: DF.TableMultiSelect[WooCommerceServerWarehouse]
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

	def before_insert(self):
		# A new server starts with what the core statuses mean, which the user can then change
		if not self.sales_order_status_map:
			self.add_default_order_statuses()
		if not self.payment_method_map and not frappe.flags.in_test:
			self.add_missing_payment_methods()

	def validate(self):
		self.validate_woocommerce_url()
		if self.enabled and self.credentials_need_testing():
			self.test_api_credentials()
		self.validate_so_status_map()
		self.validate_payment_method_map()
		self.validate_item_map()

		if not self.secret:
			self.secret = frappe.generate_hash()

	def credentials_need_testing(self) -> bool:
		"""
		Only call the WooCommerce API when something it depends on has changed
		"""
		return any(
			self.has_value_changed(field)
			for field in (
				"enabled",
				"woocommerce_server_url",
				"api_consumer_key",
				"api_consumer_secret",
			)
		)

	def _credential(self, fieldname: str) -> str:
		"""
		A Password field holds the plaintext only while it is being typed, otherwise the form sends it
		masked and the stored value has to be read back
		"""
		value = cstr(self.get(fieldname))
		if value and set(value) != {"*"}:
			return value
		return cstr(self.get_password(fieldname, raise_exception=False))

	def get_api(self) -> WooCommerceAPI:
		"""
		A client for this server as the form has it, so also before it is saved
		"""
		return WooCommerceAPI(
			url=normalise_server_url(self.woocommerce_server_url) or "",
			consumer_key=self._credential("api_consumer_key"),
			consumer_secret=self._credential("api_consumer_secret"),
			version="wc/v3",
		)

	def test_api_credentials(self):
		res = self.get_api().get("system_status", params={"_fields": "environment"})

		if res.status_code != 200:
			frappe.throw(_("WooCommerce API credentials are not valid"))

	def validate_woocommerce_url(self):
		url = normalise_server_url(self.woocommerce_server_url)
		if not url:
			frappe.throw(_("Please enter a valid WooCommerce Server URL"))
		self.woocommerce_server_url = url

	def validate_so_status_map(self):
		"""
		Validate the Sales Order Status Map: one row per WooCommerce status, one status to push per ERPNext
		status, and nothing pushed over a status that the payment gateway sets
		"""
		seen_statuses, seen_pushes = set(), set()
		for row in self.sales_order_status_map:
			row.woocommerce_status = normalise_status(row.woocommerce_status)
			if not row.woocommerce_status:
				frappe.throw(_("Row {0}: WooCommerce Status is required").format(row.idx))

			if row.woocommerce_status in seen_statuses:
				frappe.throw(
					_(
						"Row {0}: WooCommerce Status {1} is in the Sales Order Status Map more than once"
					).format(row.idx, frappe.bold(row.woocommerce_status))
				)
			seen_statuses.add(row.woocommerce_status)

			if not row.push_from_erpnext_status:
				continue
			if row.push_from_erpnext_status in seen_pushes:
				frappe.throw(
					_("Row {0}: ERPNext status {1} is pushed as more than one WooCommerce Status").format(
						row.idx, frappe.bold(row.push_from_erpnext_status)
					)
				)
			seen_pushes.add(row.push_from_erpnext_status)

			if row.woocommerce_status in PAYMENT_OWNED_STATUSES:
				frappe.throw(
					_(
						"Row {0}: {1} is set by the payment gateway of the shop, ERPNext can't push a Sales Order status as that"
					).format(row.idx, frappe.bold(row.woocommerce_status))
				)

	def get_order_status_row(self, woocommerce_status: str | None):
		"""
		The row of the Sales Order Status Map for a WooCommerce status, or None for a status it doesn't know
		"""
		status = normalise_status(woocommerce_status)
		return next((row for row in self.sales_order_status_map if row.woocommerce_status == status), None)

	def get_push_row(self, erpnext_status: str | None):
		"""
		The row that says which WooCommerce status to set when a Sales Order has this status
		"""
		if not erpnext_status:
			return None
		return next(
			(row for row in self.sales_order_status_map if row.push_from_erpnext_status == erpnext_status),
			None,
		)

	def fetch_names(self, endpoint: str, id_key: str, name_key: str) -> tuple[dict[str, str], str | None]:
		"""
		The id and name of every record of a list endpoint of the shop, and why it couldn't be read. The
		error is returned rather than raised, as the callers can go on with what they know without it.
		"""
		try:
			response = self.get_api().get(endpoint)
			if response.status_code != 200:
				return {}, _("WooCommerce returned {0}").format(response.status_code)
			return {cstr(row[id_key]): cstr(row[name_key]) for row in response.json()}, None
		except Exception as e:
			frappe.log_error("WooCommerce Error", frappe.get_traceback())
			return {}, cstr(e) or e.__class__.__name__

	def fetch_order_statuses(self) -> tuple[dict[str, str], str | None]:
		"""
		The slug and name of every order status of the shop, those of plugins included
		"""
		return self.fetch_names("reports/orders/totals", "slug", "name")

	def add_default_order_statuses(self) -> list[dict]:
		"""
		Add a row for each status that the Sales Order Status Map lacks. Rows that are there stay as they are.
		The core statuses are known without the shop, so a shop that can't be reached isn't an error here.
		"""
		live_statuses = self.fetch_order_statuses()[0] if not frappe.flags.in_test else {}
		rows = get_default_status_rows(
			live_statuses, existing=[row.woocommerce_status for row in self.sales_order_status_map]
		)
		for row in rows:
			self.append("sales_order_status_map", row)
		return rows

	@frappe.whitelist()
	def get_missing_order_status_rows(self) -> dict:
		"""
		The rows the Sales Order Status Map lacks, for the form to add, and why the shop's own statuses
		couldn't be read if they couldn't
		"""
		live_statuses, error = self.fetch_order_statuses()
		return {
			"rows": get_default_status_rows(
				live_statuses, existing=[row.woocommerce_status for row in self.sales_order_status_map]
			),
			"error": error,
		}

	def validate_payment_method_map(self):
		"""
		One row per payment method, and a Mode of Payment that a Payment Entry of the server's company can
		be booked on
		"""
		seen = set()
		for row in self.payment_method_map:
			row.woocommerce_payment_method = cstr(row.woocommerce_payment_method).strip()
			if row.woocommerce_payment_method in seen:
				frappe.throw(
					_("Row {0}: Payment method {1} is in the Payment Method Map more than once").format(
						row.idx, frappe.bold(row.woocommerce_payment_method)
					)
				)
			seen.add(row.woocommerce_payment_method)

			if (
				row.mode_of_payment
				and self.company
				and not get_mode_of_payment_account(row.mode_of_payment, self.company)
			):
				frappe.throw(
					_("Row {0}: Mode of Payment {1} has no default account for company {2}").format(
						row.idx, frappe.bold(row.mode_of_payment), frappe.bold(self.company)
					)
				)

	def get_payment_method_row(self, payment_method: str | None):
		"""
		The row of the Payment Method Map for a WooCommerce payment method, or None
		"""
		if not payment_method:
			return None
		return next(
			(row for row in self.payment_method_map if row.woocommerce_payment_method == payment_method),
			None,
		)

	def fetch_payment_methods(self) -> tuple[dict[str, str], str | None]:
		"""
		The id and title of every payment gateway of the shop, the disabled ones included, as older orders
		can have been paid with them
		"""
		return self.fetch_names("payment_gateways", "id", "title")

	def get_payment_method_rows(self, live_methods: dict[str, str]) -> list[dict]:
		existing = {row.woocommerce_payment_method for row in self.payment_method_map}
		return [
			{"woocommerce_payment_method": method, "woocommerce_payment_method_title": title or method}
			for method, title in live_methods.items()
			if method and method not in existing
		]

	def add_missing_payment_methods(self) -> list[dict]:
		"""
		Add a row, without a Mode of Payment, for each payment method of the shop the map lacks
		"""
		rows = self.get_payment_method_rows(self.fetch_payment_methods()[0])
		for row in rows:
			self.append("payment_method_map", row)
		return rows

	@frappe.whitelist()
	def get_missing_payment_method_rows(self) -> dict:
		"""
		The rows the Payment Method Map lacks, for the form to add, and why the shop couldn't be read if
		it couldn't
		"""
		live_methods, error = self.fetch_payment_methods()
		return {"rows": self.get_payment_method_rows(live_methods), "error": error}

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
			filters=[
				["fieldtype", "not in", invalid_field_types],
				["parent", "=", "Item"],
			],
		)
		custom_fields = frappe.get_all(
			"Custom Field",
			fields=["label", "name", "fieldname"],
			filters=[["fieldtype", "not in", invalid_field_types], ["dt", "=", "Item"]],
		)
		return docfields + custom_fields
