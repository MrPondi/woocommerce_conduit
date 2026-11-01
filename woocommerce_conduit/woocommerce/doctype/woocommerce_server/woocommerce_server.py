# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

from urllib.parse import urlparse, urlunparse

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, now
from frappe.utils.caching import redis_cache
from jsonpath_ng.ext import parse

from woocommerce_conduit.tasks.taxes import get_vat_rows, normalise_tax_class
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
		from woocommerce_conduit.woocommerce.doctype.woocommerce_server_payment_method.woocommerce_server_payment_method import (
			WooCommerceServerPaymentMethod,
		)
		from woocommerce_conduit.woocommerce.doctype.woocommerce_server_shipping_rule.woocommerce_server_shipping_rule import (
			WooCommerceServerShippingRule,
		)
		from woocommerce_conduit.woocommerce.doctype.woocommerce_server_tax_class.woocommerce_server_tax_class import (
			WooCommerceServerTaxClass,
		)
		from woocommerce_conduit.woocommerce.doctype.woocommerce_server_warehouse.woocommerce_server_warehouse import (
			WooCommerceServerWarehouse,
		)

		api_consumer_key: DF.Password
		api_consumer_secret: DF.Password
		company: DF.Link
		create_sales_invoice: DF.Check
		creation_user: DF.Link
		default_item_tax_template: DF.Link | None
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
		log_all_requests: DF.Check
		name_by: DF.Literal["WooCommerce ID", "Product SKU"]
		order_sync_paused_since: DF.Datetime | None
		pause_order_sync: DF.Check
		payment_method_map: DF.Table[WooCommerceServerPaymentMethod]
		price_list: DF.Link | None
		price_list_delay_per_item: DF.Float
		push_zero_stock_for_disabled_items: DF.Check
		sales_order_status_map: DF.Table[WooCommerceServerOrderStatus]
		sales_price_list: DF.Link | None
		sales_taxes_and_charges_template: DF.Link
		secret: DF.SmallText | None
		shipping_rule_map: DF.Table[WooCommerceServerShippingRule]
		submit_sales_invoice: DF.Check
		subtract_reserved_stock: DF.Check
		sync_prices_for_disabled_items: DF.Check
		tax_class_map: DF.Table[WooCommerceServerTaxClass]
		uom: DF.Link
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
		if not self.tax_class_map and not frappe.flags.in_test:
			self.add_missing_tax_classes()

	def validate(self):
		self.validate_woocommerce_url()
		self.track_order_sync_pause()
		if self.enabled and self.credentials_need_testing():
			self.test_api_credentials()
		self.validate_so_status_map()
		self.validate_payment_method_map()
		self.validate_tax_class_map()
		self.validate_tax_templates()
		self.validate_item_map()

		if not self.secret:
			self.secret = frappe.generate_hash()

	def track_order_sync_pause(self):
		"""
		Note when the order sync was paused, and when it is unpaused sync the orders that changed meanwhile
		"""
		if not self.pause_order_sync:
			if not self.is_new() and self.has_value_changed("pause_order_sync"):
				self.sync_orders_changed_while_paused()
			self.order_sync_paused_since = None
		elif self.is_new() or self.has_value_changed("pause_order_sync"):
			self.order_sync_paused_since = now()

	def sync_orders_changed_while_paused(self):
		if not self.order_sync_paused_since:
			# Paused without a date (set in the database or by a fixture), so every order of the shop would be
			# synced
			frappe.msgprint(
				_(
					"It isn't known since when the order sync was paused, so the orders changed meanwhile aren't"
					" synced. Each one is synced when it changes again."
				),
				indicator="orange",
			)
			return
		frappe.enqueue(
			"woocommerce_conduit.tasks.sync_sales_orders.sync_paused_orders",
			queue="long",
			timeout=1500,
			enqueue_after_commit=True,
			woocommerce_server=self.name,
			since=self.order_sync_paused_since,
		)

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
			# An unsaved server can't be linked to from the log yet
			woocommerce_server=None if self.is_new() else self.name,
			log_all_requests=self.log_all_requests,
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

	def validate_tax_class_map(self):
		"""
		One row per tax class. The standard class is stored by its slug, as WooCommerce has it in the list.
		"""
		seen = set()
		for row in self.tax_class_map:
			row.woocommerce_tax_class = normalise_tax_class(row.woocommerce_tax_class)
			if row.woocommerce_tax_class in seen:
				frappe.throw(
					_("Row {0}: Tax class {1} is in the Tax Class Map more than once").format(
						row.idx, frappe.bold(row.woocommerce_tax_class)
					)
				)
			seen.add(row.woocommerce_tax_class)

	def get_item_tax_template(self, tax_class: str | None) -> str | None:
		"""
		The Item Tax Template for the Items of products in a tax class: the one the Tax Class Map has for
		the class, else the default one
		"""
		tax_class = normalise_tax_class(tax_class)
		return (
			next(
				(
					row.item_tax_template
					for row in self.tax_class_map
					if row.woocommerce_tax_class == tax_class and row.item_tax_template
				),
				None,
			)
			or self.default_item_tax_template
		)

	def validate_tax_templates(self):
		"""
		Warn about an Item Tax Template that doesn't set the rate of every VAT row of the Sales Taxes and
		Charges Template: such a row charges its own rate on the Item as well.
		"""
		accounts = {row.account_head for row in get_vat_rows(self)}
		if not accounts:
			return
		templates = {self.default_item_tax_template} | {row.item_tax_template for row in self.tax_class_map}
		for template in sorted(filter(None, templates)):
			covered = {row.tax_type for row in frappe.get_cached_doc("Item Tax Template", template).taxes}
			if missing := accounts - covered:
				frappe.msgprint(
					_(
						"Item Tax Template {0} doesn't set a rate for {1}, so Items with it are also taxed at that row's rate in {2}"
					).format(
						frappe.bold(template),
						", ".join(frappe.bold(account) for account in sorted(missing)),
						frappe.bold(self.sales_taxes_and_charges_template),
					),
					indicator="orange",
					alert=True,
				)

	def fetch_tax_classes(self) -> tuple[dict[str, str], str | None]:
		"""
		The slug and name of every tax class of the shop
		"""
		return self.fetch_names("taxes/classes", "slug", "name")

	def get_tax_class_rows(self, live_classes: dict[str, str]) -> list[dict]:
		existing = {row.woocommerce_tax_class for row in self.tax_class_map}
		return [
			{"woocommerce_tax_class": tax_class, "woocommerce_tax_class_title": title or tax_class}
			for tax_class, title in live_classes.items()
			if tax_class and tax_class not in existing
		]

	def add_missing_tax_classes(self) -> list[dict]:
		"""
		Add a row, without an Item Tax Template, for each tax class of the shop the map lacks
		"""
		rows = self.get_tax_class_rows(self.fetch_tax_classes()[0])
		for row in rows:
			self.append("tax_class_map", row)
		return rows

	@frappe.whitelist()
	def get_missing_tax_class_rows(self) -> dict:
		"""
		The rows the Tax Class Map lacks, for the form to add, and why the shop couldn't be read if it
		couldn't
		"""
		live_classes, error = self.fetch_tax_classes()
		return {"rows": self.get_tax_class_rows(live_classes), "error": error}

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
