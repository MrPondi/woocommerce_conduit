import frappe
from frappe.tests import IntegrationTestCase

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


class TestTickOrderStatusActions(IntegrationTestCase):
	def test_the_ticks_are_checks_of_the_status_map(self):
		fields = {
			df.fieldname: df.fieldtype for df in frappe.get_meta("WooCommerce Server Order Status").fields
		}
		for ticks in TICKS_OF_CHOICES.values():
			for fieldname in ticks:
				self.assertEqual(fields.get(fieldname), "Check", fieldname)

	def test_a_core_status_ticks_what_it_did_before(self):
		rows = {row["woocommerce_status"]: row for row in get_default_status_rows()}
		for status, choice in OLD_DEFAULTS.items():
			ticked = {action for action in ACTIONS if rows[status].get(action)}
			self.assertEqual(ticked, set(TICKS_OF_CHOICES[choice]), status)

	def test_every_choice_ticks_actions_that_go_together(self):
		server = frappe.get_doc(
			{
				"doctype": "WooCommerce Server",
				"sales_order_status_map": [
					{"woocommerce_status": f"status-{idx}", **dict.fromkeys(ticks, 1)}
					for idx, ticks in enumerate(TICKS_OF_CHOICES.values())
				],
			}
		)
		server.validate_so_status_map()
