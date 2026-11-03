# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

from urllib.parse import urlparse, urlunparse

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, now

from woocommerce_conduit.tasks.field_map import (
	REFUSED_ITEM_FIELDS,
	get_item_map_fields,
	get_order_item_map_fields,
	get_product_fields,
	get_transforms,
	validate_map_row,
)
from woocommerce_conduit.tasks.taxes import get_vat_rows, normalise_tax_class
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTION_DELIVER,
	ACTION_HOLD,
	ACTIONS,
	PAYMENT_OWNED_STATUSES,
	SOLE_ACTIONS,
	get_default_status_rows,
	normalise_status,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_shipping_rule.woocommerce_server_shipping_rule import (
	get_shipping_method_key,
)
from woocommerce_conduit.woocommerce.woocommerce_api import WooCommerceAPI, clear_api_clients


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


def validate_status_actions(row) -> None:
	"""
	The actions that a row of the Sales Order Status Map ticks go together: Cancel, Close and Skip are ticked
	alone, and Hold not with Deliver. The form unticks the others in the same way.
	"""

	def label(action: str) -> str:
		return frappe.bold(_(row.meta.get_label(action)))

	actions = row.get_actions()
	sole = [action for action in ACTIONS if action in actions & SOLE_ACTIONS]
	if sole and len(actions) > 1:
		frappe.throw(_("Row {0}: {1} can't be ticked with other actions").format(row.idx, label(sole[0])))
	if {ACTION_HOLD, ACTION_DELIVER} <= actions:
		frappe.throw(
			_(
				"Row {0}: ERPNext doesn't deliver an order on hold, so {1} and {2} can't both be ticked"
			).format(row.idx, label(ACTION_HOLD), label(ACTION_DELIVER))
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
		from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_item_field.woocommerce_server_order_item_field import (
			WooCommerceServerOrderItemField,
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
		fee_account: DF.Link | None
		freight_and_forwarding_account: DF.Link
		item_field_map: DF.Table[WooCommerceServerItemField]
		item_group: DF.Link
		last_sync_time: DF.Datetime | None
		log_all_requests: DF.Check
		name_by: DF.Literal["WooCommerce ID", "Product SKU"]
		name_sales_orders_by_number: DF.Check
		order_item_field_map: DF.Table[WooCommerceServerOrderItemField]
		order_sync_paused_since: DF.Datetime | None
		pause_order_sync: DF.Check
		payment_method_map: DF.Table[WooCommerceServerPaymentMethod]
		price_list: DF.Link | None
		price_list_delay_per_item: DF.Float
		push_zero_stock_for_disabled_items: DF.Check
		sales_order_prefix: DF.Data | None
		sales_order_status_map: DF.Table[WooCommerceServerOrderStatus]
		sales_price_list: DF.Link | None
		sales_taxes_and_charges_template: DF.Link
		secret: DF.SmallText | None
		shipping_rule_map: DF.Table[WooCommerceServerShippingRule]
		submit_sales_invoice: DF.Check
		subtract_reserved_stock: DF.Check
		sync_in_sales_uom: DF.Check
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
		if not self.payment_method_map and not frappe.in_test:
			self.add_missing_payment_methods()
		if not self.tax_class_map and not frappe.in_test:
			self.add_missing_tax_classes()
		if not self.shipping_rule_map and not frappe.in_test:
			self.add_missing_shipping_methods()

	def validate(self):
		self.validate_woocommerce_url()
		self.track_order_sync_pause()
		if self.enabled and self.credentials_need_testing():
			self.test_api_credentials()
		self.validate_so_status_map()
		self.validate_payment_method_map()
		self.validate_shipping_rule_map()
		self.validate_tax_class_map()
		self.validate_tax_templates()
		self.validate_item_map()
		self.validate_order_item_map()
		self.validate_sales_order_prefix()

		if not self.secret:
			self.secret = frappe.generate_hash()

	def onload(self):
		# The Item fields the Item Field Map may not write, which the form leaves out of its choices, the Sales
		# Order Item fields the Order Line Fields map may write, and the transforms the installed apps register
		self.set_onload("refused_item_fields", REFUSED_ITEM_FIELDS)
		self.set_onload("order_item_fields", sorted(get_order_item_map_fields()))
		self.set_onload("field_transforms", sorted(get_transforms()))

	def on_update(self):
		# The clients built earlier in this request may have the old URL or credentials
		clear_api_clients()

	def on_trash(self):
		clear_api_clients()

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
		Validate the Sales Order Status Map: one row per WooCommerce status, actions that go together, one
		status to push per ERPNext status, and nothing pushed over a status that the payment gateway sets
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
			validate_status_actions(row)

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

	def fetch_list(self, endpoint: str) -> tuple[list[dict], str | None]:
		"""
		The records of a list endpoint of the shop, and why they couldn't be read. The error is returned
		rather than raised, as the callers can go on with what they know without it.
		"""
		try:
			response = self.get_api().get(endpoint)
			if response.status_code != 200:
				return [], _("WooCommerce returned {0}").format(response.status_code)
			records = response.json()
			if not isinstance(records, list):
				return [], _("WooCommerce returned no list")
			return records, None
		except Exception as e:
			frappe.log_error("WooCommerce Error", frappe.get_traceback())
			return [], cstr(e) or e.__class__.__name__

	def fetch_names(self, endpoint: str, id_key: str, name_key: str) -> tuple[dict[str, str], str | None]:
		"""
		The id and name of every record of a list endpoint of the shop, and why it couldn't be read
		"""
		records, error = self.fetch_list(endpoint)
		return {cstr(row.get(id_key)): cstr(row.get(name_key)) for row in records}, error

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
		live_statuses = self.fetch_order_statuses()[0] if not frappe.in_test else {}
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

	def validate_sales_order_prefix(self):
		"""
		A prefix that names Sales Orders by order number apart from those of the other shops, whose order
		numbers repeat, and that Frappe takes in a name
		"""
		if not self.name_sales_orders_by_number:
			return
		self.sales_order_prefix = cstr(self.sales_order_prefix).strip()
		if "<" in self.sales_order_prefix or ">" in self.sales_order_prefix:
			frappe.throw(_("The Sales Order Prefix can't contain < or >"))

		for other in frappe.get_all(
			"WooCommerce Server",
			filters={"name": ["!=", self.name or ""], "name_sales_orders_by_number": 1},
			fields=["name", "sales_order_prefix"],
		):
			if cstr(other.sales_order_prefix).strip() == self.sales_order_prefix:
				frappe.throw(
					_(
						"WooCommerce Server {0} names its Sales Orders with the same prefix. Order numbers repeat across shops, so each needs its own."
					).format(frappe.bold(other.name))
				)

	def validate_shipping_rule_map(self):
		"""
		One row per shipping method, and a Shipping Rule that a Sales Order of the server's company can take:
		ERPNext refuses a rule that isn't for selling, and books one of another company on its accounts. It
		puts the rule's charge on the last Actual row of the rule's account, so a rule on the account the
		order's fees are booked on would charge the shipping in place of a fee.
		"""
		fee_account = self.fee_account or self.freight_and_forwarding_account
		seen = set()
		for row in self.shipping_rule_map:
			row.wc_shipping_method_id = cstr(row.wc_shipping_method_id).strip()
			if row.wc_shipping_method_id in seen:
				frappe.throw(
					_("Row {0}: Shipping method {1} is in the Shipping Method Map more than once").format(
						row.idx, frappe.bold(row.wc_shipping_method_id)
					)
				)
			seen.add(row.wc_shipping_method_id)

			rule = row.shipping_rule and frappe.get_cached_value(
				"Shipping Rule",
				row.shipping_rule,
				["shipping_rule_type", "company", "disabled", "account"],
				as_dict=True,
			)
			if not rule:
				continue
			if rule.shipping_rule_type != "Selling":
				frappe.throw(
					_("Row {0}: Shipping Rule {1} isn't for selling, so a Sales Order can't take it").format(
						row.idx, frappe.bold(row.shipping_rule)
					)
				)
			if self.company and rule.company != self.company:
				frappe.throw(
					_("Row {0}: Shipping Rule {1} is for company {2}, not {3}").format(
						row.idx,
						frappe.bold(row.shipping_rule),
						frappe.bold(rule.company),
						frappe.bold(self.company),
					)
				)
			if rule.disabled:
				frappe.throw(
					_("Row {0}: Shipping Rule {1} is disabled").format(
						row.idx, frappe.bold(row.shipping_rule)
					)
				)
			if fee_account and rule.account == fee_account:
				frappe.throw(
					_(
						"Row {0}: Shipping Rule {1} books on {2}, as the order's fees are, so ERPNext would charge the shipping in place of a fee. Set a Fee Account, or give the rule another account."
					).format(row.idx, frappe.bold(row.shipping_rule), frappe.bold(rule.account))
				)

	def get_shipping_rule(self, shipping_line: dict) -> str | None:
		"""
		The Shipping Rule for an order shipped with this line's method: the rule of the method's row, else the
		one of the row of its kind of method (a bare method id), else None. A row without a rule doesn't count.
		"""
		rules = {
			row.wc_shipping_method_id: row.shipping_rule
			for row in self.shipping_rule_map
			if row.shipping_rule
		}
		method_id = cstr(shipping_line.get("method_id")).strip()
		key = get_shipping_method_key(method_id, shipping_line.get("instance_id"))
		return rules.get(key) or rules.get(method_id)

	def fetch_shipping_methods(self) -> tuple[dict[str, tuple[str, str]], str | None]:
		"""
		The methods of every shipping zone of the shop by `method_id:instance_id`, each with the zone's name
		and the title customers see. The disabled ones are included, as older orders can have been shipped
		with them. Nothing when a zone can't be read, as the others' methods would show its own as gone.
		"""
		zones, error = self.fetch_names("shipping/zones", "id", "name")
		methods = {}
		for zone_id, zone_name in zones.items():
			zone_methods, error = self.fetch_list(f"shipping/zones/{zone_id}/methods")
			if error:
				return {}, error
			for method in zone_methods:
				if key := get_shipping_method_key(method.get("method_id"), method.get("instance_id")):
					methods[key] = (zone_name, cstr(method.get("title")) or cstr(method.get("method_title")))
		return methods, error

	def get_shipping_method_rows(self, live_methods: dict[str, tuple[str, str]]) -> list[dict]:
		existing = {cstr(row.wc_shipping_method_id).strip() for row in self.shipping_rule_map}
		return [
			{
				"wc_shipping_method_id": method,
				"wc_shipping_method_title": title or method,
				"wc_shipping_zone": zone,
			}
			for method, (zone, title) in live_methods.items()
			if method not in existing
		]

	def get_gone_shipping_methods(self, live_methods: dict[str, tuple[str, str]]) -> list[str]:
		"""
		The rows of methods of a zone that the shop no longer has, each as its method and title. A bare
		method id is for every method of its kind, which no zone has to have, so it is never gone.
		"""
		gone = []
		for row in self.shipping_rule_map:
			method = cstr(row.wc_shipping_method_id).strip()
			if ":" in method and method not in live_methods:
				gone.append(
					f"{method} ({row.wc_shipping_method_title})" if row.wc_shipping_method_title else method
				)
		return gone

	def add_missing_shipping_methods(self) -> list[dict]:
		"""
		Add a row, without a Shipping Rule, for each method of the shop's shipping zones the map lacks
		"""
		rows = self.get_shipping_method_rows(self.fetch_shipping_methods()[0])
		for row in rows:
			self.append("shipping_rule_map", row)
		return rows

	@frappe.whitelist()
	def get_missing_shipping_method_rows(self) -> dict:
		"""
		The rows the Shipping Method Map lacks, for the form to add, the rows of methods the shop no longer
		has, and why the shop couldn't be read if it couldn't
		"""
		live_methods, error = self.fetch_shipping_methods()
		return {
			"rows": self.get_shipping_method_rows(live_methods),
			"gone": [] if error else self.get_gone_shipping_methods(live_methods),
			"error": error,
		}

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
		for template in sorted(name for name in templates if name):
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
		Each row of the Item Field Map needs an Item field the map may write, and a JSONPath that starts at a
		field of WooCommerce Product: it runs on the product as the doctype has it, so a path into any other
		field finds nothing.
		"""
		# The variant sync owns the attributes, and the image sync, when it is on, the Item's image
		refused_sources = ["attributes", "images"] if self.enable_image_sync else ["attributes"]
		target_fields, source_fields = get_item_map_fields(), get_product_fields()
		for row in self.item_field_map:
			validate_map_row(
				row,
				_("Item Field Map"),
				"Item",
				target_fields,
				source_doctype="WooCommerce Product",
				source_fields=source_fields,
				refused_sources=refused_sources,
			)

	def validate_order_item_map(self):
		"""
		Each row of the Order Line Fields map needs a Sales Order Item field it may write. Its path isn't checked
		against the line's fields, as plugins add their own to it.
		"""
		target_fields = get_order_item_map_fields()
		for row in self.order_item_field_map:
			validate_map_row(row, _("Order Line Fields"), "Sales Order Item", target_fields)
