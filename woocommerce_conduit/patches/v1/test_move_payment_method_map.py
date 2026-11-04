from unittest.mock import patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.patches.v1.move_payment_method_map import move_rows, read_old_rows

SERVER = "moved.example.com"
OTHER_SERVER = "unmapped.example.com"


def old_row(parent, method, mode_of_payment=None, bank_account=None):
	return _dict(
		parent=parent,
		woocommerce_payment_method=method,
		woocommerce_payment_method_title=method.upper(),
		mode_of_payment=mode_of_payment,
		bank_account=bank_account,
	)


class TestMovePaymentMethodMap(IntegrationTestCase):
	def setUp(self):
		frappe.db.savepoint("test_move_payment_method_map")
		self.addCleanup(frappe.db.rollback, save_point="test_move_payment_method_map")
		for name in (SERVER, OTHER_SERVER):
			frappe.get_doc(
				{"doctype": "WooCommerce Server", "name": name, "woocommerce_server_url": f"https://{name}"}
			).db_insert()

	def test_a_servers_mapped_rows_move_into_a_pending_import(self):
		rows = [
			old_row(SERVER, "bacs", "Wire Transfer", "PKO"),
			old_row(SERVER, "cod"),
			# Only the shop's gateways, nothing a person mapped
			old_row(OTHER_SERVER, "bacs"),
			# Of a server that is gone
			old_row("gone.example.com", "bacs", "Wire Transfer"),
		]
		made = move_rows(rows)

		self.assertEqual(len(made), 1)
		doc = frappe.get_doc("WooCommerce Order Import", made[0])
		self.assertEqual((doc.woocommerce_server, doc.status), (SERVER, "Pending"))
		self.assertEqual(
			[
				(
					row.woocommerce_payment_method,
					row.woocommerce_payment_method_title,
					row.mode_of_payment,
					row.bank_account,
				)
				for row in doc.payment_method_map
			],
			[("bacs", "BACS", "Wire Transfer", "PKO"), ("cod", "COD", None, None)],
		)

	def test_a_server_that_has_an_import_keeps_it(self):
		self.assertEqual(len(move_rows([old_row(SERVER, "bacs", "Wire Transfer")])), 1)
		self.assertEqual(move_rows([old_row(SERVER, "cod", "Cash")]), [])
		self.assertEqual(frappe.db.count("WooCommerce Order Import", {"woocommerce_server": SERVER}), 1)

	def test_a_site_without_the_old_table_has_nothing_to_move(self):
		with patch.object(frappe.db, "table_exists", return_value=False):
			self.assertEqual(read_old_rows(), [])
