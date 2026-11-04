# The module of the fields, so uninstalling the app removes them
MODULE = "WooCommerce"

# A Customer's form shows its possible duplicates only when there are some (customer_duplicates.py)
HAS_POSSIBLE_DUPLICATES = (
	"eval:doc.__onload && doc.__onload.woocommerce_duplicates && doc.__onload.woocommerce_duplicates.length"
)


def _(text: str) -> str:
	"""Marks a label for translation. It is stored as it is, and Frappe translates it where it shows it."""
	return text


def get_custom_fields() -> dict[str, list[dict]]:
	"""
	The fields the app adds to ERPNext's doctypes, by doctype, each after the field it is inserted after. They
	are made on install and brought up to date after every migrate (install.py).
	"""
	return {
		# The shops an Item is linked to, on a tab of their own
		"Item": [
			{
				"fieldname": "woocommerce_tab",
				"label": _("WooCommerce"),
				"fieldtype": "Tab Break",
				"insert_after": "serial_no_series",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_servers",
				"fieldtype": "Table",
				"options": "Item WooCommerce Server",
				"insert_after": "woocommerce_tab",
				"module": MODULE,
			},
		],
		# At the top of the form, above ERPNext's fields: the Customers that may be the same buyer, then who the
		# Customer is in the shop it was made for
		"Customer": [
			# The possible duplicates, in a section of their own so they take the whole width, shown only when
			# there are some. A field without insert_after goes to the top of the form (Meta.sort_fields). It is
			# the only one: a second would go there too, in an order of its own.
			{
				"fieldname": "woocommerce_duplicates_section",
				"fieldtype": "Section Break",
				"depends_on": HAS_POSSIBLE_DUPLICATES,
				"print_hide": 1,
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_duplicates",
				"label": _("Possible Duplicates"),
				"fieldtype": "HTML",
				"insert_after": "woocommerce_duplicates_section",
				"depends_on": HAS_POSSIBLE_DUPLICATES,
				"print_hide": 1,
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_section",
				"fieldtype": "Section Break",
				"print_hide": 1,
				"insert_after": "woocommerce_duplicates",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_identifier",
				"label": _("Woocommerce Identifier"),
				"fieldtype": "Data",
				"read_only": 1,
				"in_list_view": 1,
				"search_index": 1,
				"print_hide": 1,
				"insert_after": "woocommerce_section",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_column_break",
				"fieldtype": "Column Break",
				"print_hide": 1,
				"insert_after": "woocommerce_identifier",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_server",
				"label": _("WooCommerce Server"),
				"fieldtype": "Link",
				"options": "WooCommerce Server",
				"read_only": 1,
				"in_standard_filter": 1,
				"print_hide": 1,
				"insert_after": "woocommerce_column_break",
				"module": MODULE,
			},
		],
		# The WooCommerce order a Sales Order was made from, and what the sync saw of it
		"Sales Order": [
			{
				"fieldname": "woocommerce_customer_note",
				"label": _("WooCommerce Customer Note"),
				"fieldtype": "Small Text",
				"read_only": 1,
				"allow_on_submit": 1,
				"width": "100",
				"insert_after": "amended_from",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_sync_issue",
				"label": _("WooCommerce Sync Issue"),
				"fieldtype": "Small Text",
				"depends_on": "eval:doc.woocommerce_sync_issue",
				"read_only": 1,
				"allow_on_submit": 1,
				"no_copy": 1,
				"insert_after": "woocommerce_customer_note",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_server",
				"label": _("WooCommerce Server"),
				"fieldtype": "Link",
				"options": "WooCommerce Server",
				"hidden": 1,
				"read_only": 1,
				"print_hide": 1,
				"insert_after": "woocommerce_sync_issue",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_id",
				"label": _("WooCommerce ID"),
				"fieldtype": "Data",
				"hidden": 1,
				"read_only": 1,
				"search_index": 1,
				"print_hide": 1,
				"insert_after": "woocommerce_server",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_status",
				"label": _("WooCommerce Status"),
				"fieldtype": "Data",
				"hidden": 1,
				"read_only": 1,
				"allow_on_submit": 1,
				"in_list_view": 1,
				"print_hide": 1,
				"insert_after": "woocommerce_id",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_payment_method",
				"label": _("WooCommerce Payment Method"),
				"fieldtype": "Data",
				"read_only": 1,
				"allow_on_submit": 1,
				"print_hide": 1,
				"insert_after": "woocommerce_status",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_last_sync_hash",
				"label": _("WooCommerce Last Sync Hash"),
				"fieldtype": "Datetime",
				"hidden": 1,
				"read_only": 1,
				"allow_on_submit": 1,
				# A duplicated order hasn't been synced
				"no_copy": 1,
				"print_hide": 1,
				"insert_after": "woocommerce_payment_method",
				"module": MODULE,
			},
		],
		# The product and the line of the WooCommerce order a row was made from, which only the sync writes
		"Sales Order Item": [
			{
				"fieldname": "woocommerce_id",
				"label": _("WooCommerce Product ID"),
				"fieldtype": "Data",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "item_code",
				"module": MODULE,
			},
			{
				"fieldname": "woocommerce_line_item_id",
				"label": _("WooCommerce Line Item ID"),
				"fieldtype": "Data",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "woocommerce_id",
				"module": MODULE,
			},
		],
	}
