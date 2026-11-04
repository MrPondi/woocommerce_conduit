import frappe

STATUS_ROW = "WooCommerce Server Order Status"
# The Order Status Map ticked these for completed by default. Invoice and Deliver are gone from the map, but
# their columns are still in the table. The fieldnames are written out, so the patch stays as it is when the
# app's own constants change.
OLD_DEFAULT = ("action_submit", "action_invoice", "action_deliver")
OTHER_ACTIONS = ("action_hold", "action_cancel", "action_close", "action_skip")


def execute():
	"""
	An order that ERPNext first meets completed is for a WooCommerce Order Import now, which books its payment,
	so completed makes no Sales Orders any more. A completed row still at the old default loses Submit. A row a
	person changed stays as it is. A site that never had the Invoice and Deliver columns has no old default.
	"""
	if not all(frappe.db.has_column(STATUS_ROW, column) for column in (*OLD_DEFAULT, *OTHER_ACTIONS)):
		return
	get_untick_query().run()


def get_untick_query():
	table = frappe.qb.DocType(STATUS_ROW)
	query = frappe.qb.update(table).set(table.action_submit, 0).where(table.woocommerce_status == "completed")
	for fieldname in OLD_DEFAULT:
		query = query.where(table[fieldname] == 1)
	for fieldname in OTHER_ACTIONS:
		query = query.where(table[fieldname] == 0)
	return query
