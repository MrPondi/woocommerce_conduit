import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks import item_parity

SERVER = "one.example.com"


def order(status, *lines, created="2026-10-30T10:00:00"):
	return {"status": status, "line_items": json.dumps(list(lines)), "woocommerce_date_created": created}


def line(product_id, qty=1, variation_id=0, sku="", name="Product"):
	return {
		"product_id": product_id,
		"variation_id": variation_id,
		"quantity": qty,
		"sku": sku,
		"name": name,
	}


class TestUnlinkedOrderProducts(IntegrationTestCase):
	def check(self, orders, linked=(), suggestions=None):
		suggestions = suggestions or {}
		server = MagicMock()
		with (
			patch.object(frappe, "get_doc", return_value=server),
			patch.object(item_parity, "get_recent_orders", return_value=orders) as get_recent_orders,
			patch.object(
				item_parity, "find_item_code", side_effect=lambda s, pid: "ITEM" if pid in linked else None
			),
			patch.object(item_parity, "find_item_by_sku", side_effect=lambda s, sku: suggestions.get(sku)),
		):
			rows = item_parity.get_unlinked_order_products(SERVER, 50)
		server.check_permission.assert_called_once_with("read")
		get_recent_orders.assert_called_once_with(SERVER, 50)
		return rows

	def test_linked_and_deleted_products_are_left_out(self):
		rows = self.check([order("processing", line(1), line(2), line(0))], linked={"1"})
		self.assertEqual([row["product_id"] for row in rows], ["2"])
		self.assertEqual(rows[0]["woocommerce_product_name"], f"{SERVER}~2")

	def test_variations_are_their_own_products(self):
		rows = self.check([order("processing", line(5, variation_id=51), line(5, variation_id=52))])
		self.assertEqual({row["product_id"] for row in rows}, {"51", "52"})

	def test_most_ordered_first_and_drafts_left_out(self):
		rows = self.check(
			[
				order("completed", line(1, qty=1), line(2, qty=3), line(2, qty=1)),
				order("processing", line(2, qty=1)),
				order("checkout-draft", line(1, qty=9), line(1, qty=9)),
			]
		)
		self.assertEqual(
			[(row["product_id"], row["orders"], row["qty"]) for row in rows], [("2", 2, 5), ("1", 1, 1)]
		)

	def test_item_with_the_sku_is_suggested(self):
		rows = self.check([order("processing", line(1, sku="MOWER-1"))], suggestions={"MOWER-1": "M1"})
		self.assertEqual(rows[0]["suggested_item"], "M1")


class TestLinkingProducts(IntegrationTestCase):
	def test_product_is_linked_to_the_item(self):
		item = MagicMock()
		with (
			patch.object(item_parity, "find_item_code", return_value=None),
			patch.object(frappe, "get_doc", return_value=item),
		):
			item_parity.link_product_to_item(SERVER, "51", "MOWER-1")

		item.check_permission.assert_called_once_with("write")
		item.append.assert_called_once_with(
			"woocommerce_servers",
			{"woocommerce_server": SERVER, "woocommerce_id": "51", "enable_sync": 1},
		)
		item.save.assert_called_once()

	def test_product_linked_already_is_not_linked_twice(self):
		with (
			patch.object(item_parity, "find_item_code", return_value="OTHER"),
			self.assertRaises(frappe.ValidationError),
		):
			item_parity.link_product_to_item(SERVER, "51", "MOWER-1")
