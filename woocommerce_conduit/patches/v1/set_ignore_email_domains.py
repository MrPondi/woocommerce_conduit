import frappe

SETTINGS = "WooCommerce Settings"
FIELD = "ignore_email_domains"


def execute():
	"""
	The email domains the Duplicate Customers report doesn't group Customers by are a field of WooCommerce
	Settings now, no longer a filter of the report. Frappe gives a Single's new field its default only while the
	Single has no values at all, so WooCommerce Settings that were saved get the default here.
	"""
	# Never saved: the default applies when the Settings are loaded. Filled in already: it is kept.
	if not frappe.db.get_singles_dict(SETTINGS) or frappe.db.get_single_value(SETTINGS, FIELD):
		return
	default = frappe.get_meta(SETTINGS).get_field(FIELD).default
	frappe.db.set_single_value(SETTINGS, FIELD, default, update_modified=False)
