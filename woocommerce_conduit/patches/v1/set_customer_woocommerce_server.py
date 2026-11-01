from collections import defaultdict

import frappe
from frappe.query_builder.functions import IfNull, Min
from frappe.utils.fixtures import sync_fixtures


def execute():
	"""
	The sync looks a Customer up by its shop and identifier now, as a guest's order id and a username repeat
	across shops. A Customer it made gets the shop of its oldest Sales Order, and on a site with one shop the
	ones without a Sales Order get that shop too. The old lookup put orders of several shops on one Customer,
	which only a person can split, so those are listed.
	"""
	# Fixtures are synced after the patches, and the field is one of them
	if not frappe.db.has_column("Customer", "woocommerce_server"):
		sync_fixtures("woocommerce_conduit")

	shops = get_shops_of_customers()
	for name, servers in shops.items():
		frappe.db.set_value("Customer", name, "woocommerce_server", servers[0], update_modified=False)

	if len(all_servers := frappe.get_all("WooCommerce Server", pluck="name")) == 1:
		customer = frappe.qb.DocType("Customer")
		(
			frappe.qb.update(customer)
			.set(customer.woocommerce_server, all_servers[0])
			.where((customer.woocommerce_identifier != "") & (IfNull(customer.woocommerce_server, "") == ""))
		).run()

	if merged := sorted(name for name, servers in shops.items() if len(servers) > 1):
		print(
			"These Customers have Sales Orders of more than one WooCommerce shop. They stay with the shop of "
			f"their first order, and the orders of the other shops are best moved by hand: {', '.join(merged)}"
		)


def get_shops_of_customers() -> dict[str, list[str]]:
	"""
	The shops of the Sales Orders of each Customer that the sync made and that has no shop yet, the shop of
	its oldest Sales Order first
	"""
	customer = frappe.qb.DocType("Customer")
	sales_order = frappe.qb.DocType("Sales Order")
	first_order = Min(sales_order.creation)
	rows = (
		frappe.qb.from_(sales_order)
		.join(customer)
		.on(customer.name == sales_order.customer)
		.where(
			(customer.woocommerce_identifier != "")
			& (IfNull(customer.woocommerce_server, "") == "")
			& (sales_order.woocommerce_server != "")
		)
		.groupby(sales_order.customer, sales_order.woocommerce_server)
		.select(sales_order.customer, sales_order.woocommerce_server, first_order)
		.orderby(first_order)
	).run()

	shops = defaultdict(list)
	for name, server, _first_order in rows:
		shops[name].append(server)
	return shops
