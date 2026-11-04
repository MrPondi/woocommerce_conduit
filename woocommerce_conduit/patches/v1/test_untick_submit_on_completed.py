from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.patches.v1 import untick_submit_on_completed
from woocommerce_conduit.patches.v1.untick_submit_on_completed import STATUS_ROW, get_untick_query

SERVER = "untick.example.com"


class TestUntickSubmitOnCompleted(IntegrationTestCase):
	def test_only_completed_rows_at_the_old_default_lose_submit(self):
		sql = get_untick_query().get_sql()

		self.assertIn("SET `action_submit`=0", sql)
		self.assertIn("`woocommerce_status`='completed'", sql)
		for fieldname in ("action_submit", "action_invoice", "action_deliver"):
			self.assertIn(f"`{fieldname}`=1", sql)
		# A row a person ticked more on stays as it is
		for fieldname in ("action_hold", "action_cancel", "action_close", "action_skip"):
			self.assertIn(f"`{fieldname}`=0", sql)

	def test_a_site_without_the_old_columns_is_left_alone(self):
		with (
			patch.object(frappe.db, "has_column", return_value=False),
			patch.object(untick_submit_on_completed, "get_untick_query") as get_query,
		):
			untick_submit_on_completed.execute()
		get_query.assert_not_called()

	def test_the_rows_of_a_site_that_has_them(self):
		if not frappe.db.has_column(STATUS_ROW, "action_invoice"):
			self.skipTest("This site never had the Invoice and Deliver columns")

		frappe.db.savepoint("test_untick_submit")
		self.addCleanup(frappe.db.rollback, save_point="test_untick_submit")
		table = frappe.qb.Table(f"tab{STATUS_ROW}")
		rows = {
			"old-default": ("completed", 1, 1, 1),
			"changed": ("completed", 1, 0, 0),
			"processing": ("processing", 1, 1, 0),
		}
		for name, (status, submit, invoice, deliver) in rows.items():
			frappe.qb.into(table).columns(
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"woocommerce_status",
				"action_submit",
				"action_invoice",
				"action_deliver",
			).insert(
				name, SERVER, "WooCommerce Server", "sales_order_status_map", status, submit, invoice, deliver
			).run()

		untick_submit_on_completed.execute()

		submit = dict(
			frappe.get_all(
				STATUS_ROW, filters={"parent": SERVER}, fields=["name", "action_submit"], as_list=True
			)
		)
		self.assertEqual(submit, {"old-default": 0, "changed": 1, "processing": 1})
