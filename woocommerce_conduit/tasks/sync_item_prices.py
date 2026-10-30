import time

import frappe
from erpnext.stock.doctype.item_price.item_price import ItemPrice
from frappe import _, _dict, qb
from frappe.query_builder import Criterion
from frappe.query_builder.functions import IfNull
from frappe.utils import get_datetime

from woocommerce_conduit.tasks.sync import SynchroniseWooCommerce, record_sync_time
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


def _format_sale_date(date_value) -> str | None:
	"""
	Convert an ERPNext Date value (string or date object) to the ISO 8601 datetime string the
	WooCommerce REST API expects, or None to clear the field.
	"""
	if not date_value or not (value := get_datetime(date_value)):
		return None
	return value.strftime("%Y-%m-%dT%H:%M:%S")


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


def update_item_price_for_woocommerce_item_from_hook(doc, method=None):
	"""
	Intended to be triggered by a Document Controller hook from Item Price. Pushes the Item's prices once
	the change is committed.
	"""
	if frappe.flags.in_test or doc.flags.created_by_sync or not price_sync_is_enabled():
		return
	# Only the name goes in the job. It runs after the commit, so it reads the saved price.
	frappe.enqueue(
		"woocommerce_conduit.tasks.sync_item_prices.run_item_price_sync",
		enqueue_after_commit=True,
		item_code=doc.item_code,
	)


@frappe.whitelist()
def run_item_price_sync_in_background():
	frappe.enqueue(run_item_price_sync, queue="long", timeout=3600)


@frappe.whitelist(methods=["POST"])
def sync_item_price_to_woocommerce(item_code: str):
	"""
	Push one Item's prices to WooCommerce, from the Item form
	"""
	frappe.has_permission("Item", "write", doc=item_code, throw=True)
	if not price_sync_is_enabled():
		frappe.throw(_("Price sync is not enabled on any WooCommerce Server"))
	run_item_price_sync(item_code=item_code)


@frappe.whitelist()
def run_item_price_sync(item_code: str | None = None, item_price_doc: ItemPrice | None = None):
	sync = SynchroniseItemPrice(item_code=item_code, item_price_doc=item_price_doc)
	sync.run()
	return True


class SynchroniseItemPrice(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of ERPNext Items with WooCommerce Products
	"""

	item_code: str | None
	item_price_list: list
	sale_price_map: dict

	def __init__(
		self,
		servers: list[WooCommerceServer | _dict] | None = None,
		item_code: str | None = None,
		item_price_doc: ItemPrice | None = None,
	) -> None:
		super().__init__(servers)
		self.item_code = item_code
		self.item_price_doc = item_price_doc
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
		Get list of ERPNext Item Prices to synchronise,
		"""
		if not self.wc_server:
			return
		self.item_price_list = []
		if self.wc_server.enabled and self.wc_server.enable_price_list_sync and self.wc_server.price_list:
			ip = qb.DocType("Item Price")
			iwc = qb.DocType("Item WooCommerce Server")
			item = qb.DocType("Item")
			and_conditions = []
			and_conditions.append(ip.price_list == self.wc_server.price_list)
			and_conditions.append(iwc.woocommerce_server == self.wc_server.name)
			and_conditions.append(iwc.enable_sync == 1)
			if not self.wc_server.sync_prices_for_disabled_items:
				and_conditions.append(item.disabled == 0)
			and_conditions.append(iwc.woocommerce_id.isnotnull())
			and_conditions.extend(item_wide_price_conditions(ip))
			if self.item_code:
				and_conditions.append(ip.item_code == self.item_code)

			self.item_price_list = (  # pyright: ignore[reportCallIssue]
				qb.from_(ip)
				.inner_join(iwc)
				.on(iwc.parent == ip.item_code)
				.inner_join(item)
				.on(item.name == ip.item_code)
				.select(ip.name, ip.item_code, ip.price_list_rate, iwc.woocommerce_server, iwc.woocommerce_id)
				.where(Criterion.all(and_conditions))
				.run(as_dict=True)
			)

	def get_erpnext_sale_prices(self) -> None:
		"""
		Build a lookup map of woocommerce_id -> sale Item Price record using the
		server's configured Sales Price List.
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

		and_conditions = [
			ip.price_list == server.sales_price_list,
			iwc.woocommerce_server == server.name,
			# Same as the regular price query, see below
			iwc.enable_sync == 1,
			iwc.woocommerce_id.isnotnull(),
			*item_wide_price_conditions(ip),
		]
		# Both queries have to agree on this. A disabled Item that reached the regular price list but
		# not this map would have its sale price cleared as though the sale had been withdrawn.
		if not server.sync_prices_for_disabled_items:
			and_conditions.append(item.disabled == 0)
		if self.item_code:
			and_conditions.append(ip.item_code == self.item_code)

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

		for row in results:
			self.sale_price_map[row.woocommerce_id] = row

	def sync_items_with_woocommerce_products(self) -> None:
		"""
		Synchronise Item Prices with WooCommerce Products
		"""
		if not self.wc_server:
			return
		pushed = False
		for item_price in self.item_price_list:
			# Get the WooCommerce Product doc
			wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
				domain=item_price.woocommerce_server, resource_id=item_price.woocommerce_id
			)
			wc_product: WooCommerceProduct = frappe.get_doc(
				{"doctype": "WooCommerce Product", "name": wc_product_name}
			)  # type: ignore

			try:
				wc_product.load_from_db()
				wc_product_dirty = False

				# If self.item_price_doc is set, set the price_list_rate accordingly, else use the price_list_rate from the price list
				price_list_rate = (
					self.item_price_doc.price_list_rate
					if self.item_price_doc and self.item_price_doc.price_list == self.wc_server.price_list
					else item_price.price_list_rate
				)
				# Handle blank string for regular_price
				if not wc_product.regular_price:
					wc_product.regular_price = 0
				# When the price is set, the WooCommerce API returns a string value, when the price is not set, it returns a float value of 0.0
				wc_product_regular_price = (
					float(wc_product.regular_price)
					if isinstance(wc_product.regular_price, str)
					else wc_product.regular_price
				)
				if wc_product_regular_price != price_list_rate:
					wc_product.regular_price = price_list_rate
					wc_product_dirty = True

				if self.wc_server.enable_sales_price_list_sync and self.wc_server.sales_price_list:
					wc_product_dirty |= self._apply_sale_price(wc_product, item_price.woocommerce_id)

				if wc_product_dirty:
					# The callers check permissions; whoever saved the Item Price may not have write
					# access to WooCommerce Products
					wc_product.flags.ignore_permissions = True
					wc_product.save()
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

	def _apply_sale_price(self, wc_product: WooCommerceProduct, woocommerce_id: str) -> bool:
		"""
		Set or clear sale_price, date_on_sale_from and date_on_sale_to on the WooCommerce Product
		based on the sale_price_map entry for this product.

		Returns True if any field was changed, False otherwise.
		"""
		if not self.wc_server:
			return False
		sale_price_record = self.sale_price_map.get(woocommerce_id)

		if sale_price_record:
			if self.item_price_doc and self.item_price_doc.price_list == self.wc_server.sales_price_list:
				new_sale_price = float(self.item_price_doc.price_list_rate or 0)
				new_valid_from = self.item_price_doc.valid_from
				new_valid_upto = self.item_price_doc.valid_upto
			else:
				new_sale_price = float(sale_price_record.price_list_rate or 0)
				new_valid_from = sale_price_record.valid_from
				new_valid_upto = sale_price_record.valid_upto
		else:
			new_sale_price = 0
			new_valid_from = None
			new_valid_upto = None

		dirty = False
		if float(wc_product.sale_price or 0) != new_sale_price:
			wc_product.sale_price = new_sale_price
			dirty = True

		new_from_str = _format_sale_date(new_valid_from)
		new_to_str = _format_sale_date(new_valid_upto)

		if wc_product.date_on_sale_from != new_from_str:
			wc_product.date_on_sale_from = new_from_str
			dirty = True

		if wc_product.date_on_sale_to != new_to_str:
			wc_product.date_on_sale_to = new_to_str
			dirty = True

		return dirty
