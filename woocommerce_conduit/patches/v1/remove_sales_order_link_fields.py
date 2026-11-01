import frappe


def execute():
	"""
	The sync finds the Sales Invoice and Payment Entry of a Sales Order through ERPNext's own links now
	(Sales Invoice Item.sales_order and Payment Entry Reference), so the Sales Order's fields for them go.
	Fixtures that were removed from the app stay on the site until they are deleted.
	"""
	for fieldname in ("woocommerce_sales_invoice", "woocommerce_payment_entry"):
		frappe.delete_doc("Custom Field", f"Sales Order-{fieldname}", ignore_missing=True, force=True)
