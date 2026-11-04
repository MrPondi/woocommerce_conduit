from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.patches.v1 import tick_order_status_actions
from woocommerce_conduit.patches.v1.tick_order_status_actions import TICKS_OF_CHOICES
from woocommerce_conduit.woocommerce.doctype.woocommerce_server_order_status.woocommerce_server_order_status import (
	ACTIONS,
	get_default_status_rows,
)

# What the core statuses did before the patch
OLD_DEFAULTS = {
	"checkout-draft": "Skip",
	"pending": "Submit SO + Invoice",
	"failed": "Cancel SO",
	"on-hold": "Submit SO + Hold",
	"processing": "Submit SO + Invoice",
	"completed": "Complete SO",
	"cancelled": "Cancel SO",
	"refunded": "Close SO",
	"trash": "Cancel SO",
}
# Invoice and Deliver left the map, as the sync makes no invoices or deliveries, and completed's Submit went
# with them: an order first seen completed is for a WooCommerce Order Import
GONE_ACTIONS = {"action_invoice", "action_deliver"}


class TestTickOrderStatusActions(IntegrationTestCase):
	def test_the_ticks_that_are_left_are_checks_of_the_status_map(self):
		fields = {
			df.fieldname: df.fieldtype for df in frappe.get_meta("WooCommerce Server Order Status").fields
		}
		for ticks in TICKS_OF_CHOICES.values():
			for fieldname in set(ticks) - GONE_ACTIONS:
				self.assertEqual(fields.get(fieldname), "Check", fieldname)
		for fieldname in GONE_ACTIONS:
			self.assertNotIn(fieldname, fields)

	def test_a_core_status_ticks_what_it_did_before_but_for_what_left_the_map(self):
		rows = {row["woocommerce_status"]: row for row in get_default_status_rows()}
		for status, choice in OLD_DEFAULTS.items():
			ticked = {action for action in ACTIONS if rows[status].get(action)}
			expected = set(TICKS_OF_CHOICES[choice]) - GONE_ACTIONS
			if status == "completed":
				expected.discard("action_submit")
			self.assertEqual(ticked, expected, status)

	def test_every_choice_ticks_actions_that_go_together(self):
		server = frappe.get_doc(
			{
				"doctype": "WooCommerce Server",
				"sales_order_status_map": [
					{"woocommerce_status": f"status-{idx}", **dict.fromkeys(set(ticks) - GONE_ACTIONS, 1)}
					for idx, ticks in enumerate(TICKS_OF_CHOICES.values())
				],
			}
		)
		server.validate_so_status_map()

	def test_ticks_whose_column_is_gone_are_left_out(self):
		# A site that migrates over both changes never gets the columns of Invoice and Deliver
		columns = {
			"erpnext_action",
			"action_submit",
			"action_hold",
			"action_cancel",
			"action_close",
			"action_skip",
		}
		update = MagicMock()
		query = update.return_value.where.return_value
		query.set.return_value = query
		with (
			patch.object(frappe.db, "has_column", side_effect=lambda doctype, column: column in columns),
			patch.object(frappe.qb, "update", update),
		):
			tick_order_status_actions.execute()

		ticked = {call.args[0].name for call in query.set.call_args_list}
		self.assertEqual(ticked, columns - {"erpnext_action"})
		# Every choice still ran, Complete SO with Submit alone
		self.assertEqual(query.run.call_count, len(TICKS_OF_CHOICES))
