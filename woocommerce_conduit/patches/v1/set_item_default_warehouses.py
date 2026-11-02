import frappe

from woocommerce_conduit.tasks.sync_items import set_default_warehouse


def execute():
	"""
	The sync gives the Items it makes or links the default warehouse of the shop's company now, as ERPNext 16
	gives a new Item none. This does the same for the Items linked to a shop before, in the company of each
	of their shops, unless they have a default warehouse there already.
	"""
	with_warehouse = {
		(row.parent, row.company)
		for row in frappe.get_all(
			"Item Default",
			filters={"parenttype": "Item", "default_warehouse": ["is", "set"]},
			fields=["parent", "company"],
		)
	}
	for item_code, company in get_linked_items():
		if (item_code, company) not in with_warehouse:
			set_default_warehouse(item_code, company)


def get_linked_items() -> list[tuple[str, str]]:
	"""
	The Items linked to a shop, each with the company of each of its shops
	"""
	connection = frappe.qb.DocType("Item WooCommerce Server")
	server = frappe.qb.DocType("WooCommerce Server")
	return (  # pyright: ignore[reportCallIssue]
		frappe.qb.from_(connection)
		.join(server)
		.on(server.name == connection.woocommerce_server)
		.where(connection.parenttype == "Item")
		.select(connection.parent, server.company)
		.distinct()
	).run()
