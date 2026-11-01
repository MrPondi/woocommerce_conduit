# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import patch

from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks import taxes
from woocommerce_conduit.tasks.taxes import (
	get_vat_rates,
	normalise_tax_class,
	prices_include_tax,
	split_vat,
	to_net,
)

SERVER = _dict(company="Shop", sales_taxes_and_charges_template="VAT")
ROWS = [_dict(account_head="VAT 23%", rate=23.0), _dict(account_head="VAT 8%", rate=8.0)]


class TestAmounts(IntegrationTestCase):
	def test_net(self):
		self.assertEqual(to_net(260, 23), 211.38)
		self.assertEqual(to_net(50, 0), 50)

	def test_split_vat_adds_up(self):
		self.assertEqual(split_vat(20, [("VAT 23%", 23.0)]), (16.26, [("VAT 23%", 3.74)]))
		net, vat = split_vat(10, [("VAT A", 5.0), ("VAT B", 3.0), ("VAT C", 0.0)])
		self.assertEqual(net, 9.26)
		self.assertEqual([account for account, _amount in vat], ["VAT A", "VAT B"])
		self.assertAlmostEqual(net + sum(amount for _account, amount in vat), 10)
		self.assertEqual(split_vat(20, []), (20, []))

	def test_standard_class_is_named(self):
		self.assertEqual(normalise_tax_class(""), "standard")
		self.assertEqual(normalise_tax_class(" zerowa-stawka "), "zerowa-stawka")


class TestTemplate(IntegrationTestCase):
	def test_prices_include_tax_when_the_template_says_so(self):
		for rows, included in (
			([_dict(account_head="VAT 23%", included_in_print_rate=1)], True),
			([_dict(account_head="VAT 23%", included_in_print_rate=0)], False),
			([], False),
		):
			with patch.object(taxes, "get_vat_rows", return_value=rows):
				self.assertEqual(prices_include_tax(SERVER), included)

	def rates(self, item_tax_map, template="Reduced"):
		with (
			patch.object(taxes, "get_vat_rows", return_value=ROWS),
			patch.object(taxes, "get_item_tax_map", return_value=item_tax_map),
		):
			return get_vat_rates(SERVER, template)

	def test_template_rates_without_an_item_tax_template(self):
		self.assertEqual(self.rates({}, template=None), [("VAT 23%", 23.0), ("VAT 8%", 8.0)])

	def test_item_tax_template_sets_the_rates(self):
		self.assertEqual(self.rates({"VAT 23%": 0, "VAT 8%": 8}), [("VAT 23%", 0.0), ("VAT 8%", 8.0)])

	def test_an_account_the_item_tax_template_leaves_out_keeps_the_row_rate(self):
		self.assertEqual(self.rates({"VAT 23%": 23}), [("VAT 23%", 23.0), ("VAT 8%", 8.0)])

	def test_not_applicable_is_no_vat(self):
		self.assertEqual(
			self.rates({"VAT 23%": taxes.NOT_APPLICABLE_TAX, "VAT 8%": 0}),
			[("VAT 23%", 0.0), ("VAT 8%", 0.0)],
		)
