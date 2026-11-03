from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from woocommerce_conduit.custom_fields import get_custom_fields


def after_install():
	create_custom_fields(get_custom_fields())


def after_migrate():
	# A field changed in custom_fields.py is updated on every site. Only the fields that differ are saved.
	create_custom_fields(get_custom_fields())
