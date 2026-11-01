"""
ERPNext works out the VAT with the server's Sales Taxes and Charges Template, at the rates of each Item's Item
Tax Template. When the template's VAT is included in the rate, the rates stay the shop's gross prices and
ERPNext takes the VAT out of them.
"""

import frappe
from erpnext.stock.get_item_details import NOT_APPLICABLE_TAX, get_item_tax_map
from frappe.utils import cstr, flt

# WooCommerce leaves the class of a product empty when it is the standard one
STANDARD_TAX_CLASS = "standard"
# A variation whose class is "parent" has that of its product
PARENT_TAX_CLASS = "parent"


def normalise_tax_class(tax_class: str | None) -> str:
	return cstr(tax_class).strip() or STANDARD_TAX_CLASS


def get_vat_rows(wc_server) -> list:
	"""
	The rows of the server's Sales Taxes and Charges Template that tax the lines
	"""
	if not wc_server.get("sales_taxes_and_charges_template"):
		return []
	template = frappe.get_cached_doc(
		"Sales Taxes and Charges Template", wc_server.sales_taxes_and_charges_template
	)
	return [row for row in template.taxes if row.charge_type == "On Net Total"]


def prices_include_tax(wc_server) -> bool:
	"""
	Whether the rates include the VAT: when "Is this Tax included in Basic Rate?" is ticked on the template
	"""
	rows = get_vat_rows(wc_server)
	return bool(rows and rows[0].included_in_print_rate)


def get_vat_rates(wc_server, item_tax_template: str | None) -> list[tuple[str, float]]:
	"""
	The account and rate of each VAT row on a line with this Item Tax Template, as ERPNext charges them: the
	Item Tax Template's rate of an account, else the row's own rate
	"""
	rows = get_vat_rows(wc_server)
	item_tax_map = (
		get_item_tax_map(
			doc={"company": wc_server.company, "taxes": []}, tax_template=item_tax_template, as_json=False
		)
		if item_tax_template
		else {}
	)
	rates = []
	for row in rows:
		rate = item_tax_map.get(row.account_head, row.rate)
		rates.append((row.account_head, 0.0 if rate == NOT_APPLICABLE_TAX else flt(rate)))
	return rates


def to_net(gross, rate) -> float:
	return flt(flt(gross) / (1 + flt(rate) / 100), 2)


def split_vat(gross, vat_rates: list[tuple[str, float]]) -> tuple[float, list[tuple[str, float]]]:
	"""
	A gross amount as its net amount and the VAT on each account, which add up to it exactly
	"""
	net = to_net(gross, sum(rate for _account, rate in vat_rates))
	vat = [(account, flt(net * rate / 100, 2)) for account, rate in vat_rates if rate]
	if vat:
		# The rounding goes to the last row, so nothing is lost
		account, _amount = vat[-1]
		vat[-1] = (account, flt(flt(gross) - net - sum(amount for _account, amount in vat[:-1]), 2))
	return net, vat
