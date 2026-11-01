app_name = "woocommerce_conduit"
app_title = "WooCommerce Conduit"
app_publisher = "Karol Parzonka"
app_description = "Connect your woocomemrce site to frappe"
app_email = "me@pondi.dev"
app_license = "gpl-3.0"
app_logo_url = "/assets/woocommerce_conduit/images/woocommerce-logo.svg"
app_home = "/desk/woocommerce-server"

required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
add_to_apps_screen = [
	{
		"name": app_name,
		"logo": "/assets/woocommerce_conduit/images/woocommerce-logo.svg",
		"title": "WooCommerce",
		"route": app_home,
	}
]

doctype_js = {"Item": "public/js/item.js", "Sales Order": "public/js/sales_order.js"}

# Document Events
# ---------------

doc_events = {
	"Sales Order": {
		"on_submit": [
			"woocommerce_conduit.tasks.sync_sales_orders.run_sales_order_sync_from_hook",
			"woocommerce_conduit.tasks.stock_update.update_stock_levels_for_woocommerce_item",
		],
		"on_cancel": "woocommerce_conduit.tasks.stock_update.update_stock_levels_for_woocommerce_item",
		"on_change": "woocommerce_conduit.tasks.order_status.enqueue_status_push_from_hook",
	},
	"Item": {
		"validate": "woocommerce_conduit.tasks.sync_items.validate_primary_connection",
	},
	"Item Price": {
		"on_update": "woocommerce_conduit.tasks.sync_item_prices.update_item_price_for_woocommerce_item_from_hook",
	},
	# Every stock movement and its reversal, whichever document makes it
	"Stock Ledger Entry": {
		"on_submit": "woocommerce_conduit.tasks.stock_update.update_stock_levels_from_ledger",
	},
}

# Extension Hooks (for other apps)
# --------------------------------
# Adjust the stock values pushed to WooCommerce, e.g. to take backorders when a supplier has stock.
# Declare in the other app's hooks.py:
#
# woocommerce_stock_values = ["supplier_app.stock.adjust_woocommerce_stock"]
#
# def adjust_woocommerce_stock(item, wc_server, bins, stock_values) -> dict | None:
# 	if stock_values["stock_quantity"] <= 0 and supplier_has_stock(item.name):
# 		return {**stock_values, "backorders": "notify"}
#
# Return the values to push, or None to keep them. Only WooCommerce Product writable_fields are written.
# Call woocommerce_conduit.tasks.stock_update.enqueue_stock_update(item_code) to push again when the
# data the method reads changes.

# Scheduled Tasks
# ---------------

scheduler_events = {
	# Prices and stock are pushed when they change; these are the safety net
	"daily_long": [
		"woocommerce_conduit.tasks.sync_item_prices.run_item_price_sync_in_background",
		"woocommerce_conduit.tasks.stock_update.update_stock_levels_for_all_enabled_items_in_background",
	],
	# Products are pulled once, when an order needs them (or from the Sync buttons), not on a schedule
	"hourly_long": [
		"woocommerce_conduit.tasks.sync_sales_orders.sync_woocommerce_orders_modified_since",
	],
}

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Fixtures
# --------

fixtures = [
	{
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				(
					"Item-woocommerce_tab",
					"Item-woocommerce_servers",
					"Customer-woocommerce_identifier",
					"Sales Order-woocommerce_id",
					"Sales Order-woocommerce_server",
					"Sales Order-woocommerce_status",
					"Sales Order-woocommerce_payment_method",
					"Sales Order-woocommerce_last_sync_hash",
					"Sales Order-woocommerce_customer_note",
					"Sales Order-woocommerce_sync_issue",
					"Sales Order Item-woocommerce_id",
					"Sales Order Item-woocommerce_line_item_id",
				),
			]
		],
	}
]
