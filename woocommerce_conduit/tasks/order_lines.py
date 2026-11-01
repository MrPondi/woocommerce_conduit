import json
from typing import TYPE_CHECKING

import frappe
from erpnext.controllers.accounts_controller import update_child_qty_rate
from frappe import _
from frappe.utils import cstr, flt

from woocommerce_conduit.tasks.sync_items import run_item_sync
from woocommerce_conduit.tasks.taxes import get_vat_rates, prices_include_tax, split_vat
from woocommerce_conduit.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

if TYPE_CHECKING:
	from woocommerce_conduit.tasks.sync_sales_orders import SyncedOrder
	from woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder
	from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
		WooCommerceServer,
	)

# The Item that stands in for a line whose product was deleted in WooCommerce or can't be found here
PLACEHOLDER_ITEM = "DELETED_WOOCOMMERCE_PRODUCT"


def get_json_lines(wc_order, fieldname: str) -> list[dict]:
	return json.loads(wc_order.get(fieldname) or "[]")


def get_line_items(wc_order) -> list[dict]:
	return get_json_lines(wc_order, "line_items")


def get_product_id(line: dict) -> str:
	"""
	The id of the variation, or of the product when it has none. "0" when it was deleted in WooCommerce.
	"""
	return cstr(line.get("variation_id") or line.get("product_id") or 0)


def get_line_rate(line: dict, tax_inclusive: bool) -> float:
	"""
	The rate of a line after coupons: WooCommerce's `total` is what is left of the `subtotal` after the
	discounts. When the template's VAT is included in the rate, it is what the customer paid, so with any
	tax WooCommerce charged itself.
	"""
	qty = flt(line.get("quantity"))
	if not qty:
		return 0.0
	total = flt(line.get("total"))
	if tax_inclusive:
		total += flt(line.get("total_tax"))
	return total / qty


def rates_match(rate, other_rate) -> bool:
	# ERPNext keeps a rate to the cent, WooCommerce's total / qty can have more decimals
	return abs(flt(rate) - flt(other_rate)) < 0.005


def totals_differ(grand_total, wc_total, lines: list[dict]) -> bool:
	"""
	Whether a Sales Order doesn't add up to its WooCommerce Order. Each rate is rounded to the cent, so the
	difference allowed grows with the quantity.
	"""
	tolerance = 0.01 + 0.005 * sum(flt(line.get("quantity")) for line in lines)
	return abs(flt(grand_total) - flt(wc_total)) > tolerance


def build_item_row(line: dict, item_code: str, item_name: str | None, rate: float, warehouse, delivery_date):
	row = {
		"item_code": item_code,
		"item_name": item_name or line.get("name"),
		"description": item_name or line.get("name"),
		"qty": flt(line.get("quantity")),
		"rate": rate,
		"warehouse": warehouse,
		"woocommerce_id": get_product_id(line),
		"woocommerce_line_item_id": cstr(line.get("id")),
	}
	if delivery_date:
		row["delivery_date"] = delivery_date
	# ERPNext takes a rate of 0 for one that isn't set and puts the price list rate in its place. A line sold
	# for nothing (a 100% coupon, a free gift) keeps it as a full discount. Update Items works it out itself.
	if not flt(rate, 2):
		row["discount_percentage"] = 100
	return row


def actual_charge(amount, account: str | None, description: str) -> dict:
	return {
		"charge_type": "Actual",
		"account_head": account,
		"tax_amount": flt(amount),
		"description": description,
		# ERPNext takes the VAT rate of a line from the last row on each account. Without this the VAT on
		# shipping, on the template's VAT account, would set the rate of the lines to nothing.
		"set_by_item_tax_template": 1,
	}


def build_charges(
	wc_order, wc_server, with_shipping: bool, vat_rates: list[tuple[str, float]], tax_inclusive: bool
) -> list[dict]:
	"""
	The rows of the Sales Taxes and Charges that WooCommerce charged on top of the lines: shipping (unless
	a Shipping Rule works it out) and fees, and the VAT on them. The template taxes the lines, but not
	these. `vat_rates` are the account and rate of each VAT row of the template.

	When the template's VAT is included in the rate, so it is in shipping and fees, and it is taken out of
	them at these rates: ERPNext can't include tax in an Actual charge. Otherwise they are net, and the VAT
	on them is what WooCommerce charged.
	"""
	freight_account = wc_server.freight_and_forwarding_account
	fee_account = wc_server.get("fee_account") or freight_account

	amounts = []
	if with_shipping:
		amounts.append((wc_order.shipping_total, wc_order.shipping_tax, freight_account, _("Shipping")))
	for fee in get_json_lines(wc_order, "fee_lines"):
		amounts.append((fee.get("total"), fee.get("total_tax"), fee_account, fee.get("name") or _("Fee")))

	charges = []
	vat = {}
	for amount, tax, account, description in amounts:
		if tax_inclusive:
			amount, tax_rows = split_vat(flt(amount) + flt(tax), vat_rates)
		else:
			tax_rows = [(vat_rates[0][0] if vat_rates else freight_account, flt(tax))]
		if flt(amount):
			charges.append(actual_charge(amount, account, description))
		for tax_account, tax_amount in tax_rows:
			vat[tax_account] = flt(vat.get(tax_account)) + flt(tax_amount)

	for tax_account, tax_amount in vat.items():
		if flt(tax_amount, 2):
			charges.append(actual_charge(flt(tax_amount, 2), tax_account, _("VAT on shipping and fees")))
	return charges


def find_item_code(woocommerce_server: str, product_id: str) -> str | None:
	"""
	The enabled Item linked to a WooCommerce product. An Item can be linked to several shops.
	"""
	iws = frappe.qb.DocType("Item WooCommerce Server")
	itm = frappe.qb.DocType("Item")
	rows = (  # pyright: ignore[reportCallIssue]
		frappe.qb.from_(iws)
		.join(itm)
		.on(iws.parent == itm.name)
		.where(
			(iws.woocommerce_id == product_id)
			& (iws.woocommerce_server == woocommerce_server)
			& (itm.disabled == 0)
		)
		.select(iws.parent)
		.limit(1)
	).run(as_dict=True)
	return rows[0].parent if rows else None


def get_placeholder_item(wc_server) -> str:
	"""
	The Item for lines whose product is gone, made the first time it is needed
	"""
	if not frappe.db.exists("Item", PLACEHOLDER_ITEM):
		item = frappe.new_doc("Item")
		item.item_code = PLACEHOLDER_ITEM
		item.item_name = "Deleted WooCommerce Product"
		item.description = "Deleted WooCommerce Product"
		item.item_group = wc_server.item_group or "All Item Groups"
		item.stock_uom = wc_server.uom or "Nos"
		item.is_stock_item = 0
		item.is_fixed_asset = 0
		item.opening_stock = 0
		item.flags.created_by_sync = True
		item.insert()
	return PLACEHOLDER_ITEM


class SalesOrderLinesMixin:
	"""
	The lines and charges of the Sales Order of a WooCommerce Order
	"""

	sales_order: SyncedOrder
	woocommerce_order: WooCommerceOrder

	def get_wc_server(self) -> WooCommerceServer:
		return frappe.get_cached_doc("WooCommerce Server", self.woocommerce_order.woocommerce_server)  # type: ignore

	def create_missing_items(self):
		"""
		Pull the products of the lines that aren't linked to an Item yet. A product the shop no longer has
		gets the placeholder Item instead, in build_item_rows.
		"""
		server = self.woocommerce_order.woocommerce_server
		for line in get_line_items(self.woocommerce_order):
			product_id = get_product_id(line)
			# Deleted products have "0"
			if product_id == "0" or find_item_code(server, product_id):
				continue
			try:
				run_item_sync(
					woocommerce_product_name=generate_woocommerce_record_name_from_domain_and_id(
						server, product_id
					)
				)
			except frappe.DoesNotExistError:
				continue

	def build_item_rows(self, wc_server, delivery_date) -> tuple[list[dict], list[str]]:
		"""
		A row for every line of the order, and what to flag about them. A line is never left out, as the
		Sales Order would not add up: one whose product is gone gets the placeholder Item.
		"""
		tax_inclusive = prices_include_tax(wc_server)
		rows, issues = [], []
		for line in get_line_items(self.woocommerce_order):
			product_id = get_product_id(line)
			item_code = find_item_code(wc_server.name, product_id) if product_id != "0" else None
			item_name = frappe.get_cached_value("Item", item_code, "item_name") if item_code else None
			if not item_code:
				item_code = get_placeholder_item(wc_server)
				issues.append(
					_("Line {0} has no Item, it is on the Sales Order as {1}").format(
						line.get("name"), item_code
					)
				)
			rows.append(
				build_item_row(
					line,
					item_code,
					item_name,
					get_line_rate(line, tax_inclusive),
					wc_server.warehouse,
					delivery_date,
				)
			)
		return rows, issues

	def set_items_in_sales_order(self, new_sales_order: SyncedOrder):
		"""
		The lines, taxes and charges of a new Sales Order
		"""
		wc_server = self.get_wc_server()
		if not wc_server.warehouse:
			frappe.throw(_("Please set Warehouse in WooCommerce Server"))
		if not get_line_items(self.woocommerce_order):
			frappe.throw(_("WooCommerce Order {0} has no lines").format(self.woocommerce_order.name))

		# The prices are WooCommerce's, ERPNext's pricing rules must not change them
		new_sales_order.ignore_pricing_rule = 1
		rows, issues = self.build_item_rows(wc_server, new_sales_order.delivery_date)
		for row in rows:
			new_sales_order.append("items", row)

		# Adds the rows of the template, which it only does while there are no taxes
		new_sales_order.taxes_and_charges = wc_server.sales_taxes_and_charges_template
		new_sales_order.set_missing_lead_customer_details()

		# Shipping and fees are taxed at the rate of the default Item Tax Template
		for charge in build_charges(
			self.woocommerce_order,
			wc_server,
			with_shipping=not new_sales_order.shipping_rule,
			vat_rates=get_vat_rates(wc_server, wc_server.default_item_tax_template),
			tax_inclusive=prices_include_tax(wc_server),
		):
			new_sales_order.append("taxes", charge)

		self.line_issues = issues

	def check_totals(self, sales_order: SyncedOrder) -> str | None:
		"""
		What to flag when the Sales Order doesn't add up to the WooCommerce Order
		"""
		if totals_differ(
			sales_order.grand_total, self.woocommerce_order.total, get_line_items(self.woocommerce_order)
		):
			return _("The Sales Order totals {0} while WooCommerce has {1}").format(
				flt(sales_order.grand_total), flt(self.woocommerce_order.total)
			)
		return None

	def update_sales_order_items(self) -> None:
		"""
		Bring the lines of the Sales Order in line with WooCommerce. As long as nothing was delivered or
		invoiced the Sales Order is changed, like Update Items does. After that the lines are left to a
		person, as the Delivery Note or Sales Invoice would have to change too.
		"""
		sales_order = self.sales_order
		wc_server = self.get_wc_server()
		self.create_missing_items()
		wanted, issues = self.build_item_rows(wc_server, sales_order.delivery_date)
		if self.lines_match(wanted):
			return

		keyed = all(row.get("woocommerce_line_item_id") for row in sales_order.items)
		if (
			sales_order.docstatus != 1
			or not keyed
			or flt(sales_order.per_delivered) > 0
			or flt(sales_order.per_billed) > 0
		):
			self.flag_sync_issue(
				_(
					"The lines changed in WooCommerce after the order was delivered or invoiced. Update them by hand."
				)
			)
			return

		existing = {cstr(row.woocommerce_line_item_id): row for row in sales_order.items}
		trans_items = []
		for row in wanted:
			current = existing.get(row["woocommerce_line_item_id"])
			item = {
				"item_code": row["item_code"],
				"qty": row["qty"],
				"rate": row["rate"],
				"delivery_date": row.get("delivery_date") or sales_order.delivery_date,
				"warehouse": row["warehouse"],
			}
			# A line that now has another product is a new row, the old one is removed
			if current and current.item_code == row["item_code"]:
				item.update(
					docname=current.name, uom=current.uom, conversion_factor=current.conversion_factor
				)
			trans_items.append(item)

		update_child_qty_rate("Sales Order", json.dumps(trans_items, default=str), sales_order.name)
		sales_order.reload()
		self.link_new_rows(wanted)

		if issue := (issues and "; ".join(issues)) or self.check_totals(sales_order):
			self.flag_sync_issue(issue)

	def lines_match(self, wanted: list[dict]) -> bool:
		rows = {cstr(row.get("woocommerce_line_item_id")): row for row in self.sales_order.items}
		if set(rows) != {row["woocommerce_line_item_id"] for row in wanted}:
			return False
		return all(
			rows[row["woocommerce_line_item_id"]].item_code == row["item_code"]
			and flt(rows[row["woocommerce_line_item_id"]].qty) == flt(row["qty"])
			and rates_match(rows[row["woocommerce_line_item_id"]].rate, row["rate"])
			for row in wanted
		)

	def link_new_rows(self, wanted: list[dict]) -> None:
		"""
		Update Items doesn't take custom fields, so the rows it added are linked to their line here
		"""
		linked = {cstr(row.woocommerce_line_item_id) for row in self.sales_order.items}
		unlinked = [row for row in self.sales_order.items if not row.woocommerce_line_item_id]
		for line in wanted:
			if line["woocommerce_line_item_id"] in linked:
				continue
			row = next((row for row in unlinked if row.item_code == line["item_code"]), None)
			if not row:
				continue
			unlinked.remove(row)
			frappe.db.set_value(
				"Sales Order Item",
				row.name,
				{
					"woocommerce_id": line["woocommerce_id"],
					"woocommerce_line_item_id": line["woocommerce_line_item_id"],
				},
				update_modified=False,
			)
			row.woocommerce_id = line["woocommerce_id"]
			row.woocommerce_line_item_id = line["woocommerce_line_item_id"]
