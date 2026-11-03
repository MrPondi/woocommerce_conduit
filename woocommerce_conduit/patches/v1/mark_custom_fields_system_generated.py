import frappe

from woocommerce_conduit.custom_fields import get_custom_fields


def execute():
	"""
	The app makes its custom fields from custom_fields.py now, not from fixtures. Fields made that way are system
	generated: Customize Form saves changes to them as Property Setters, which an update of the field leaves
	alone, and doesn't delete them, also not when it resets a doctype. The fields the fixtures made are marked
	the same.
	"""
	for doctype, fields in get_custom_fields().items():
		frappe.db.set_value(
			"Custom Field",
			{"dt": doctype, "fieldname": ("in", [df["fieldname"] for df in fields])},
			"is_system_generated",
			1,
			update_modified=False,
		)
		frappe.clear_cache(doctype=doctype)
