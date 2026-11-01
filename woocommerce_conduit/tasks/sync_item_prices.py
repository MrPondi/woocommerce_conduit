import time
from collections import defaultdict
from datetime import date

import frappe
from erpnext.stock.get_item_details import get_item_prices_for_stock_uom
from frappe import _, _dict, qb
from frappe.query_builder import Criterion
from frappe.query_builder.functions import IfNull
from frappe.utils import create_batch, get_datetime, getdate, today

from woocommerce_conduit.tasks.sync import enqueue_after_commit_unless_queued, record_sync_time
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

# The Items one job of the daily push takes: as many as one WooCommerce batch request takes
ITEMS_PER_JOB = 100


def _format_sale_date(date_value) -> str | None:
	"""
	Convert an ERPNext Date value (string or date object) to the ISO 8601 datetime string the
	WooCommerce REST API expects, or None to clear the field.
	"""
	if not date_value or not (value := get_datetime(date_value)):
		return None
	return value.strftime("%Y-%m-%dT%H:%M:%S")


def pick_sale(rows: list, on_date: date):
	"""
	The sale of a product to show in the shop, of those that haven't ended: the running one that ERPNext
	would charge (the one that started last), else the next one to start, which WooCommerce schedules
	"""
	running = [row for row in rows if not row.valid_from or getdate(row.valid_from) <= on_date]
	if running:
		return max(running, key=lambda row: getdate(row.valid_from or "2000-01-01"))
	return min(rows, key=lambda row: getdate(row.valid_from))


def item_wide_price_conditions(ip) -> list:
	"""
	Conditions restricting an Item Price query to rows that apply to the whole item.
	"""
	return [
		IfNull(ip.batch_no, "") == "",
		IfNull(ip.customer, "") == "",
		IfNull(ip.supplier, "") == "",
	]


def price_sync_is_enabled() -> bool:
	"""
	Whether any WooCommerce Server has its price sync switched on
	"""
	return bool(frappe.db.exists("WooCommerce Server", {"enabled": 1, "enable_price_list_sync": 1}))


def get_price_sync_servers() -> list[WooCommerceServer]:
	"""
	The enabled WooCommerce Servers that have their price sync switched on
	"""
	return [
		frappe.get_cached_doc("WooCommerce Server", name)  # type: ignore
		for name in frappe.get_all(
			"WooCommerce Server", filters={"enabled": 1, "enable_price_list_sync": 1}, pluck="name"
		)
	]


def update_item_price_for_woocommerce_item_from_hook(doc, method=None):
	"""
	Intended to be triggered by the on_update and on_trash of an Item Price. Pushes the Item's prices once
	the change is committed, so a deleted Item Price is gone by then.
	"""
	if frappe.flags.in_test or doc.flags.created_by_sync or not price_sync_is_enabled():
		return
	# Only the name goes in the job. It runs after the commit, so it reads the saved price.
	enqueue_after_commit_unless_queued(
		"woocommerce_conduit.tasks.sync_item_prices.run_item_price_sync",
		job_id=f"woocommerce_price_sync::{doc.item_code}",
		item_code=doc.item_code,
	)


def run_item_price_sync_in_background():
	"""
	Push the prices of every Item linked to a WooCommerce Product, ITEMS_PER_JOB Items a job
	"""
	if not price_sync_is_enabled():
		return

	item_codes = sorted(
		set(
			frappe.get_all(
				"Item WooCommerce Server", filters={"parenttype": "Item", "enable_sync": 1}, pluck="parent"
			)
		)
	)
	for item_codes_of_job in create_batch(item_codes, ITEMS_PER_JOB):
		frappe.enqueue(run_item_price_sync, queue="long", timeout=1800, item_codes=list(item_codes_of_job))


@frappe.whitelist(methods=["POST"])
def sync_item_price_to_woocommerce(item_code: str):
	"""
	Push one Item's prices to WooCommerce, from the Item form
	"""
	frappe.has_permission("Item", "write", doc=item_code, throw=True)
	if not price_sync_is_enabled():
		frappe.throw(_("Price sync is not enabled on any WooCommerce Server"))
	run_item_price_sync(item_code=item_code)


def run_item_price_sync(item_code: str | None = None, item_codes: list[str] | None = None):
	sync = SynchroniseItemPrice(item_codes=[item_code] if item_code else item_codes)
	sync.run()
	return True


class SynchroniseItemPrice:
	"""
	Class for managing synchronisation of ERPNext Items with WooCommerce Products
	"""

	item_codes: list[str] | None
	item_price_list: list
	sale_price_map: dict

	def __init__(
		self,
		servers: list[WooCommerceServer | _dict] | None = None,
		item_codes: list[str] | None = None,
	) -> None:
		self.servers = servers or get_price_sync_servers()
		# All the Items when there are none
		self.item_codes = item_codes
		self.wc_server = None
		self.item_price_list = []
		self.sale_price_map = {}

	def run(self) -> None:
		"""
		Run synchornisation
		"""
		for server in self.servers:
			self.wc_server = server
			self.get_erpnext_item_prices()
			self.get_erpnext_sale_prices()
			self.sync_items_with_woocommerce_products()

	def get_erpnext_item_prices(self) -> None:
		"""
		The Items linked to products of the server, each with its price in the server's Price List: the Item
		Price that ERPNext would charge today (valid today, in the stock UOM, for anyone, the newest)
		"""
		self.item_price_list = []
		server = self.wc_server
		if not (server and server.enabled and server.enable_price_list_sync and server.price_list):
			return

		connections = self.get_connections()
		prices = get_item_prices_for_stock_uom(
			{"price_list": server.price_list, "transaction_date": today()},
			list(dict.fromkeys(connection.item_code for connection in connections)),
		)
		self.item_price_list = [
			_dict(connection, price_list_rate=prices[connection.item_code].price_list_rate)
			for connection in connections
			if connection.item_code in prices
		]

	def get_connections(self) -> list:
		"""
		The Items whose prices go to products of the server, with the ids of the products
		"""
		iwc = qb.DocType("Item WooCommerce Server")
		item = qb.DocType("Item")
		conditions = [
			iwc.parenttype == "Item",
			iwc.woocommerce_server == self.wc_server.name,
			iwc.enable_sync == 1,
			iwc.woocommerce_id.isnotnull(),
		]
		# Both price queries have to agree on this. A disabled Item that got a regular price but no sale
		# would have its sale cleared as though the sale had been withdrawn.
		if not self.wc_server.sync_prices_for_disabled_items:
			conditions.append(item.disabled == 0)
		if self.item_codes:
			conditions.append(iwc.parent.isin(self.item_codes))

		return (  # pyright: ignore[reportCallIssue]
			qb.from_(iwc)
			.inner_join(item)
			.on(item.name == iwc.parent)
			.select(iwc.parent.as_("item_code"), iwc.woocommerce_server, iwc.woocommerce_id)
			.where(Criterion.all(conditions))
			.run(as_dict=True)
		)

	def get_erpnext_sale_prices(self) -> None:
		"""
		Build a lookup map of woocommerce_id -> the sale (Item Price) of the server's Sales Price List to show,
		see pick_sale
		"""
		self.sale_price_map = {}
		server = self.wc_server
		if not (
			server
			and server.enabled
			and server.enable_price_list_sync
			and server.enable_sales_price_list_sync
			and server.sales_price_list
		):
			return

		ip = qb.DocType("Item Price")
		iwc = qb.DocType("Item WooCommerce Server")
		item = qb.DocType("Item")
		on_date = getdate(today())

		and_conditions = [
			ip.price_list == server.sales_price_list,
			iwc.woocommerce_server == server.name,
			# Same as the regular price query, see get_connections
			iwc.enable_sync == 1,
			iwc.woocommerce_id.isnotnull(),
			*item_wide_price_conditions(ip),
			# In the stock UOM, like the regular price
			(IfNull(ip.uom, "") == "") | (ip.uom == item.stock_uom),
			# A sale that has ended doesn't count
			IfNull(ip.valid_upto, "2500-12-31") >= on_date,
		]
		if not server.sync_prices_for_disabled_items:
			and_conditions.append(item.disabled == 0)
		if self.item_codes:
			and_conditions.append(ip.item_code.isin(self.item_codes))

		results = (  # pyright: ignore[reportCallIssue]
			qb.from_(ip)
			.inner_join(iwc)
			.on(iwc.parent == ip.item_code)
			.inner_join(item)
			.on(item.name == ip.item_code)
			.select(
				ip.name, ip.item_code, ip.price_list_rate, ip.valid_from, ip.valid_upto, iwc.woocommerce_id
			)
			.where(Criterion.all(and_conditions))
			.run(as_dict=True)
		)

		sales = defaultdict(list)
		for row in results:
			sales[row.woocommerce_id].append(row)
		self.sale_price_map = {
			woocommerce_id: pick_sale(rows, on_date) for woocommerce_id, rows in sales.items()
		}

	def sync_items_with_woocommerce_products(self) -> None:
		"""
		Write the prices to the WooCommerce Products whose prices differ. Only what differs is written, as
		WooCommerce dates every save as a change.
		"""
		if not self.wc_server:
			return
		pushed = False
		for item_price in self.item_price_list:
			wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
				domain=item_price.woocommerce_server, resource_id=item_price.woocommerce_id
			)
			wc_product: WooCommerceProduct = frappe.get_doc(
				{"doctype": "WooCommerce Product", "name": wc_product_name}
			)  # type: ignore

			try:
				wc_product.load_from_db()
				changes = wc_product.get_changes(self.get_price_values(item_price))
				# A variable product's price lives on its variations, so there can be nothing to write
				if changes and wc_product.write(changes):
					pushed = True
					# Space out the writes of a full sweep, so the shop isn't flooded
					if (delay := frappe.utils.flt(self.wc_server.price_list_delay_per_item)) > 0 and not (
						frappe.flags.in_test
					):
						time.sleep(delay)
			except Exception:
				error_message = f"{frappe.get_traceback()}\n\n Product Data: \n{wc_product.as_dict()!s}"
				frappe.log_error("WooCommerce Error: Price List Sync", error_message)

		if pushed:
			record_sync_time(self.wc_server.name)

	def get_price_values(self, item_price) -> dict:
		"""
		The price fields of the WooCommerce Product of an Item Price: its regular price, and the sale of the
		server's Sales Price List when the server syncs it (none clears the sale in WooCommerce)
		"""
		values = {"regular_price": item_price.price_list_rate}
		if self.wc_server and self.wc_server.enable_sales_price_list_sync and self.wc_server.sales_price_list:
			sale = self.sale_price_map.get(item_price.woocommerce_id)
			values.update(
				sale_price=frappe.utils.flt(sale.price_list_rate) if sale else 0,
				date_on_sale_from=_format_sale_date(sale.valid_from) if sale else None,
				date_on_sale_to=_format_sale_date(sale.valid_upto) if sale else None,
			)
		return values
