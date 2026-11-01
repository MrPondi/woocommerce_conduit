import json
from dataclasses import dataclass
from datetime import datetime

import frappe
from erpnext.stock.doctype.item.item import Item
from frappe import ValidationError, _, _dict
from frappe.query_builder import Criterion
from frappe.utils import convert_utc_to_system_timezone, flt, get_datetime, getdate, now
from jsonpath_ng.ext import parse

from woocommerce_conduit.exceptions import SyncDisabledError
from woocommerce_conduit.tasks.sync import SynchroniseWooCommerce
from woocommerce_conduit.tasks.taxes import PARENT_TAX_CLASS
from woocommerce_conduit.woocommerce.doctype.item_woocommerce_server.item_woocommerce_server import (
	ItemWooCommerceServer,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_settings.woocommerce_settings import (
	WooCommerceSettings,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)

# Item.item_name is a Data field without a length set, so it has the default length
ITEM_NAME_LENGTH = 140


class SyncedItem(Item):
	woocommerce_servers: list[ItemWooCommerceServer]


def validate_primary_connection(doc, method=None):
	"""
	Intended to be triggered by a Document Controller hook from Item. An Item has one primary WooCommerce
	connection, the first one unless another is marked.
	"""
	connections = doc.get("woocommerce_servers") or []
	primaries = [row for row in connections if row.is_primary]
	if len(primaries) > 1:
		frappe.throw(
			_("Only one WooCommerce connection can be Primary, but rows {0} are").format(
				", ".join(str(row.idx) for row in primaries)
			)
		)
	if connections and not primaries:
		connections[0].is_primary = 1


def get_primary_connection(item) -> ItemWooCommerceServer | None:
	"""
	The connection whose WooCommerce Product the Item takes its fields from. Items saved before the Primary
	flag existed have none set, so fall back to the first row, as validate_primary_connection does.
	"""
	connections = item.get("woocommerce_servers") or []
	return next(
		(row for row in connections if row.get("is_primary")), connections[0] if connections else None
	)


def sync_woocommerce_products_modified_since(date_time_from=None):
	"""
	Get list of WooCommerce products modified since date_time_from
	"""
	settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	if not date_time_from:
		date_time_from = getattr(settings, "wc_last_sync_date_items", None)

	# Take the cursor before fetching, so products modified while this runs are picked up next time
	sync_started_at = now()
	wc_products = get_list_of_wc_products(date_time_from=date_time_from)

	for wc_product in wc_products:
		try:
			run_item_sync(woocommerce_product_name=wc_product["name"], enqueue=True)
		# Skip items with errors, as these exceptions will be logged
		except Exception as e:
			frappe.log_error("Item sync error", f"There was an exception when syncing an item: {e!s}")

	settings.reload()
	settings.wc_last_sync_date_items = sync_started_at
	settings.flags.ignore_mandatory = True
	settings.save()


@frappe.whitelist(methods=["POST"])
def sync_woocommerce_product(woocommerce_product_name: str):
	"""
	Sync one WooCommerce Product with its ERPNext Item, from the WooCommerce Product form. A click is an
	explicit request, so an Item that is already linked is updated too.
	"""
	frappe.has_permission("Item", "write", throw=True)
	run_item_sync(woocommerce_product_name=woocommerce_product_name, force_update=True)


@frappe.whitelist(methods=["POST"])
def sync_item_from_woocommerce(item_code: str):
	"""
	Update one ERPNext Item from its WooCommerce Product(s), from the Item form
	"""
	frappe.has_permission("Item", "write", doc=item_code, throw=True)
	run_item_sync(item_code=item_code, force_update=True)


def load_wc_product(woocommerce_product_name: str) -> WooCommerceProduct:
	"""
	Load a WooCommerce Product with all its fields
	"""
	wc_product: WooCommerceProduct = frappe.get_doc(
		{"doctype": "WooCommerce Product", "name": woocommerce_product_name}
	)  # type: ignore
	wc_product.load_from_db()
	return wc_product


def run_item_sync(
	item_code: str | None = None,
	item: SyncedItem | None = None,
	woocommerce_product_name: str | None = None,
	woocommerce_product: WooCommerceProduct | None = None,
	enqueue: bool = False,
	force_update: bool = False,
) -> tuple[SyncedItem | None, WooCommerceProduct | None]:
	"""
	Synchronize an ERPNext Item with its corresponding WooCommerce Product.

	This function serves as a central dispatcher for item synchronization operations.
	It can be initiated from either the ERPNext side (using item_code/item) or
	the WooCommerce side (using woocommerce_product_name/woocommerce_product).

	A product is pulled once: a missing Item is created, an Item that is already linked is left alone
	unless force_update is set (a manual sync). After that only prices and stock change, and they go from
	ERPNext to WooCommerce.

	Parameters:
	    item_code: ERPNext Item Code to synchronize
	    item: ERPNext Item document to synchronize
	    woocommerce_product_name: Name of WooCommerce Product to synchronize
	    woocommerce_product: WooCommerce Product document to synchronize
	    enqueue: Whether to process synchronization in background
	    force_update: Update an Item that already exists from its WooCommerce Product

	Returns:
	    tuple: (ERPNext Item, WooCommerce Product) after synchronization

	Raises:
	    ValueError: If no valid input parameters are provided
	    ValidationError: If no WooCommerce Servers are defined for the item
	"""
	if not any([item_code, item, woocommerce_product_name, woocommerce_product]):
		raise ValueError(
			"At least one of item_code, item, woocommerce_product_name, or woocommerce_product parameters is required"
		)
	if enqueue:
		# Only pass names, documents (and the API clients they hold) don't belong in a job payload.
		# The job loads the product again, which also gives it every field and not just the list's.
		if woocommerce_product_name or woocommerce_product:
			kwargs = {"woocommerce_product_name": woocommerce_product_name or woocommerce_product.name}  # type: ignore
		else:
			kwargs = {"item_code": item_code or item.name}  # type: ignore
		frappe.enqueue(
			run_item_sync,
			queue="long",
			timeout=300,
			now=bool(frappe.flags.in_test),
			force_update=force_update,
			**kwargs,
		)
		return None, None

	sync = None
	erpnext_item = item

	# Case 1: Sync initiated from WooCommerce side
	if woocommerce_product_name or woocommerce_product:
		wc_product = woocommerce_product
		if not wc_product:
			wc_product = load_wc_product(woocommerce_product_name)  # type: ignore

		sync = SynchroniseItem(woocommerce_product=wc_product, force_update=force_update)
		sync.run()

	# Case 2: Sync initiated from ERPNext side
	elif item_code or erpnext_item:
		if not erpnext_item:
			erpnext_item: SyncedItem = frappe.get_doc("Item", item_code)  # type: ignore

		if not erpnext_item.woocommerce_servers:
			frappe.throw(_("No WooCommerce Servers defined for Item {0}").format(erpnext_item.name))

		# Sync with each linked WooCommerce server. run() logs its own errors, so carry on with the other
		# servers and raise afterwards.
		errors = []
		for wc_server in erpnext_item.woocommerce_servers:
			if not wc_server.woocommerce_server:
				frappe.msgprint(
					_("WooCommerce Server not specified for entry #{0} in Item {1}").format(
						wc_server.idx, erpnext_item.name
					),
					indicator="red",
					alert=True,
				)
				continue

			sync = SynchroniseItem(
				item=ERPNextItemToSync(item=erpnext_item, item_woocommerce_server_idx=wc_server.idx),
				force_update=force_update,
			)
			try:
				sync.run()
			except Exception as err:
				errors.append(err)
		if errors:
			raise errors[0]

	# Return the synchronized item and product
	# If multiple servers were synced, returns the last one processed
	if sync:
		return (
			sync.item.item if sync.item else None,
			sync.woocommerce_product if sync.woocommerce_product else None,
		)
	else:
		return None, None


@dataclass
class ERPNextItemToSync:
	"""Class for keeping track of an ERPNext Item and the relevant WooCommerce Server to sync to"""

	item: SyncedItem
	item_woocommerce_server_idx: int

	@property
	def item_woocommerce_server(self):
		return self.item.woocommerce_servers[self.item_woocommerce_server_idx - 1]

	@property
	def is_primary(self) -> bool:
		primary = get_primary_connection(self.item)
		return bool(primary) and primary.idx == self.item_woocommerce_server_idx


class SynchroniseItem(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of WooCommerce Product with ERPNext Item
	"""

	def __init__(
		self,
		servers: list[WooCommerceServer | _dict] | None = None,
		item: ERPNextItemToSync | None = None,
		woocommerce_product: WooCommerceProduct | None = None,
		force_update: bool = False,
	) -> None:
		super().__init__(servers)
		self.item = item
		self.woocommerce_product = woocommerce_product  # type: ignore
		self.force_update = force_update
		self.settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	def run(self):
		"""
		Run synchronisation
		"""
		try:
			self.get_corresponding_item_or_product()
			self.sync_wc_product_with_erpnext_item()
		except Exception as err:
			try:
				woocommerce_product_dict = (
					self.woocommerce_product.as_dict()
					if isinstance(self.woocommerce_product, WooCommerceProduct)
					else self.woocommerce_product
				)
				item_dict = (
					(self.item.item.as_dict() if isinstance(self.item.item, SyncedItem) else self.item.item)
					if self.item
					else None
				)
			except ValidationError:
				woocommerce_product_dict = self.woocommerce_product
				item_dict = self.item.item if self.item else None
			error_message = f"{frappe.get_traceback()}\n\nItem Data: \n{str(item_dict) if self.item else ''}\n\nWC Product Data \n{str(woocommerce_product_dict) if self.woocommerce_product else ''})"
			frappe.log_error("WooCommerce Error", error_message)
			raise err

	def get_corresponding_item_or_product(self):
		"""
		If we have an ERPNext Item, get the corresponding WooCommerce Product
		If we have a WooCommerce Product, get the corresponding ERPNext Item
		"""
		if self.item and not self.woocommerce_product and self.item.item_woocommerce_server.woocommerce_id:
			# Validate that this Item's WooCommerce Server has sync enabled
			wc_server: WooCommerceServer = frappe.get_cached_doc(
				"WooCommerce Server", self.item.item_woocommerce_server.woocommerce_server
			)  # type: ignore
			if not wc_server.enabled:
				raise SyncDisabledError(wc_server)

			# The WooCommerce Server is named after its domain, as the WooCommerce Product names are
			self.woocommerce_product = load_wc_product(
				generate_woocommerce_record_name_from_domain_and_id(
					wc_server.name, self.item.item_woocommerce_server.woocommerce_id
				)
			)

		if self.woocommerce_product and not self.item:
			self.get_erpnext_item()

	def get_erpnext_item(self):
		"""
		Get erpnext item for a WooCommerce Product
		"""
		if not self.woocommerce_product:
			raise ValueError("woocommerce_product required")
		if not all([self.woocommerce_product.woocommerce_server, self.woocommerce_product.woocommerce_id]):
			raise ValueError("Both woocommerce_server and woocommerce_id required")

		iws = frappe.qb.DocType("Item WooCommerce Server")
		itm = frappe.qb.DocType("Item")

		and_conditions = [
			iws.woocommerce_server == self.woocommerce_product.woocommerce_server,
			iws.woocommerce_id == self.woocommerce_product.woocommerce_id,
		]

		item_codes = (  # pyright: ignore[reportCallIssue]
			frappe.qb.from_(iws)
			.join(itm)
			.on(iws.parent == itm.name)
			.where(Criterion.all(and_conditions))
			.select(iws.parent, iws.name)
			.limit(1)
		).run(as_dict=True)

		found_item: SyncedItem | None = frappe.get_doc("Item", item_codes[0].parent) if item_codes else None  # type: ignore
		if found_item:
			self.item = ERPNextItemToSync(
				item=found_item,
				item_woocommerce_server_idx=next(
					server.idx
					for server in found_item.woocommerce_servers
					if server.name == item_codes[0].name
				),
			)

	def sync_wc_product_with_erpnext_item(self):
		"""
		Syncronise an ERPNext Item from its WooCommerce Product
		"""
		if self.woocommerce_product and not self.item:
			# create missing item in ERPNext
			self.create_item()
		elif self.item and self.woocommerce_product and self.force_update:
			# A product is pulled once, when it is created. Later pulls (orders, the product list) leave a
			# linked Item alone; only a manual sync updates it.
			# Check the sync hash. It's a Datetime field, so it reads back as a datetime and WooCommerce's
			# date_modified is a string: compare them as datetimes.
			last_sync_hash = self.item.item_woocommerce_server.woocommerce_last_sync_hash
			if not last_sync_hash or get_datetime(last_sync_hash) != get_datetime(
				self.woocommerce_product.woocommerce_date_modified
			):
				# Only WooCommerce updates the Item. An Item is shared by every shop it is linked to, so
				# its fields are never written back; prices go to WooCommerce through the price sync.
				# A newer Item means it was edited in ERPNext since, which a pull must not undo.
				if self.get_wc_modified_in_system_timezone() > get_datetime(self.item.item.modified):
					self.update_item()

	def get_wc_modified_in_system_timezone(self) -> datetime:
		"""
		WooCommerce's date_modified is in the shop's timezone, and Item.modified in ERPNext's, so compare
		via date_modified_gmt when the product has it
		"""
		if date_modified_gmt := self.woocommerce_product.get("date_modified_gmt"):  # type: ignore
			return convert_utc_to_system_timezone(get_datetime(date_modified_gmt)).replace(tzinfo=None)
		return get_datetime(self.woocommerce_product.woocommerce_date_modified)  # type: ignore

	def update_item(self):
		"""
		Update the ERPNext Item with fields from it's corresponding WooCommerce Product
		"""
		if not self.woocommerce_product or not self.item:
			return

		# Only the primary shop fills in the Item. The others keep their own name, description, etc.,
		# which would otherwise overwrite each other on the shared Item.
		if not self.item.is_primary:
			self.set_sync_hash()
			return

		item_dirty = False
		wc_item_name = to_item_name(self.woocommerce_product.woocommerce_name)
		if wc_item_name and self.item.item.item_name != wc_item_name:
			self.item.item.item_name = wc_item_name
			item_dirty = True

		fields_updated, self.item.item = self.set_item_fields(self.item.item)

		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_product.woocommerce_server
		)  # type: ignore

		if (
			wc_server.enable_image_sync
			and self.woocommerce_product.image
			and self.item.item.image != self.woocommerce_product.image
		):
			self.item.item.image = self.woocommerce_product.image
			item_dirty = True

		item_dirty |= self._set_item_tax_template(self.item.item, wc_server)

		if item_dirty or fields_updated:
			self.item.item.flags.created_by_sync = True
			self.item.item.save()

		self.set_sync_hash()

	def create_item(self):
		"""
		Create a new ERPNext Item from a WooCommerce Product.
		"""
		if not self.woocommerce_product:
			raise ValueError("WooCommerce product is required")

		# Get WooCommerce server config
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_product.woocommerce_server
		)  # type: ignore

		# Create new Item document
		item: SyncedItem = frappe.new_doc("Item")  # type: ignore

		# Set up Item based on product type
		self._handle_product_variants(item)

		# Set core item fields
		self._set_core_item_fields(item, wc_server)
		self._set_item_tax_template(item, wc_server)

		# Set mapped fields from WooCommerce
		_modified, item = self.set_item_fields(item=item)

		# Set item flags and insert
		item.flags.created_by_sync = True
		item.flags.ignore_mandatory = True
		item.insert()

		# Update self reference
		self.item = ERPNextItemToSync(
			item=item,
			item_woocommerce_server_idx=next(
				iws.idx
				for iws in item.woocommerce_servers
				if iws.woocommerce_server == self.woocommerce_product.woocommerce_server
			),
		)

		# Prices are pulled once, with the Item. From then on ERPNext owns them and the price sync pushes them.
		self.create_item_prices(wc_server)

		# Update sync hash
		self.set_sync_hash()

	def create_item_prices(self, wc_server: WooCommerceServer):
		"""
		Create the Item Prices of a new Item from its WooCommerce Product: the regular price in the server's
		Price List and a running or scheduled sale in its Sales Price List
		"""
		product = self.woocommerce_product
		if not product or not self.item or product.type == "variable":
			# A variable product's prices live on its variations
			return

		if wc_server.price_list and (regular_price := flt(product.regular_price)) > 0:
			self._insert_item_price(wc_server.price_list, regular_price)

		if wc_server.sales_price_list and (sale_price := flt(product.sale_price)) > 0:
			self._insert_item_price(
				wc_server.sales_price_list,
				sale_price,
				valid_from=getdate(product.date_on_sale_from) if product.date_on_sale_from else None,
				valid_upto=getdate(product.date_on_sale_to) if product.date_on_sale_to else None,
			)

	def _insert_item_price(self, price_list: str, rate: float, valid_from=None, valid_upto=None):
		item_code = self.item.item.name  # type: ignore
		if frappe.db.exists("Item Price", {"item_code": item_code, "price_list": price_list}):
			return
		item_price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": rate,
				"valid_from": valid_from,
				"valid_upto": valid_upto,
			}
		)
		# The price came from WooCommerce, so don't push it straight back
		item_price.flags.created_by_sync = True
		item_price.insert()

	def _set_item_tax_template(self, item: SyncedItem, wc_server: WooCommerceServer) -> bool:
		"""
		Put the Item Tax Template of the product's tax class in the Item's tax row without a tax category.
		Rows with a tax category are left as they are. Returns whether the Item changed.
		"""
		if not (template := self.get_item_tax_template(item, wc_server)):
			return False
		row = next((row for row in item.get("taxes", []) if not row.tax_category), None)
		if row and row.item_tax_template == template:
			return False
		if not row:
			row = item.append("taxes")
		row.item_tax_template = template
		return True

	def get_item_tax_template(self, item: SyncedItem, wc_server: WooCommerceServer) -> str | None:
		product = self.woocommerce_product
		if not product:
			return None
		# A variation in its product's class has the template of the product's Item
		if product.tax_class == PARENT_TAX_CLASS and item.variant_of:
			parent = frappe.get_cached_doc("Item", item.variant_of)
			return next((row.item_tax_template for row in parent.taxes if not row.tax_category), None)
		return wc_server.get_item_tax_template(product.tax_class)

	def _handle_product_variants(self, item: SyncedItem):
		"""
		Handle variant-related setup for the item based on product type.

		Args:
			item: The ERPNext item being created
		"""
		if not self.woocommerce_product:
			return

		# Handle variants based on product type
		if self.woocommerce_product.type in ["variable", "variation"]:
			self.create_or_update_item_attributes()

			# Add attributes to item
			if self.woocommerce_product.get("attributes"):
				wc_attributes = json.loads(self.woocommerce_product.attributes)
				for wc_attribute in wc_attributes:
					row = item.append("attributes")
					row.attribute = wc_attribute["name"]
					if self.woocommerce_product.type == "variation":
						row.attribute_value = wc_attribute["option"]

		# Set up variant configuration
		if self.woocommerce_product.type == "variable":
			item.has_variants = 1
		elif self.woocommerce_product.type == "variation":
			# Without its parent the variation would become a standalone Item, which a later pull no longer
			# fixes, so fail instead
			parent_item = self._get_or_create_parent_item()
			if not parent_item:
				frappe.throw(
					_("Parent product {0} of WooCommerce variation {1} could not be synced").format(
						self.woocommerce_product.parent_id, self.woocommerce_product.name
					)
				)
			item.variant_of = parent_item.item_code

	def _get_or_create_parent_item(self):
		"""
		Get or create parent item for a variation product.

		Returns:
			The parent item or None
		"""
		if not self.woocommerce_product or not self.woocommerce_product.parent_id:
			return None

		woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
			self.woocommerce_product.woocommerce_server, self.woocommerce_product.parent_id
		)
		parent_item, _parent_wc_product = run_item_sync(woocommerce_product_name=woocommerce_product_name)
		return parent_item

	def _set_core_item_fields(self, item: SyncedItem, wc_server: WooCommerceServer):
		"""
		Set core fields on the item document.

		Args:
			item: The ERPNext item being created
			wc_server: The WooCommerce server configuration
		"""
		if not self.woocommerce_product:
			return

		# Set item code based on server configuration
		item.item_code = (
			self.woocommerce_product.sku
			if wc_server.name_by == "Product SKU" and self.woocommerce_product.sku
			else str(self.woocommerce_product.woocommerce_id)
		)

		# Set basic item properties
		item.stock_uom = wc_server.uom or _("Nos")
		item.item_group = wc_server.item_group
		item.item_name = to_item_name(self.woocommerce_product.woocommerce_name)

		# Link to WooCommerce server
		row = item.append("woocommerce_servers")
		row.woocommerce_id = self.woocommerce_product.woocommerce_id
		row.woocommerce_server = wc_server.name
		row.enable_sync = 1

		# Set image if enabled
		if wc_server.enable_image_sync and self.woocommerce_product.image:
			item.image = self.woocommerce_product.image

	def create_or_update_item_attributes(self):
		"""
		Create or update Item Attributes with better code organization
		"""
		if not self.woocommerce_product or not self.woocommerce_product.attributes:
			return

		wc_attributes = json.loads(self.woocommerce_product.attributes)

		for wc_attribute in wc_attributes:
			attribute_name = wc_attribute["name"]
			item_attribute = self._get_or_create_attribute(attribute_name)

			# Get attribute options based on product type
			options = self._get_attribute_options(wc_attribute)

			# Item Attributes are shared between products, so only add the values that are missing
			if (
				not self._add_missing_attribute_values(item_attribute, options)
				and not item_attribute.is_new()
			):
				continue

			item_attribute.flags.ignore_mandatory = True
			if item_attribute.is_new():
				item_attribute.insert()
			else:
				item_attribute.save()

	def _get_or_create_attribute(self, attribute_name):
		"""Helper method to get or create an attribute"""
		if frappe.db.exists("Item Attribute", attribute_name):
			return frappe.get_doc("Item Attribute", attribute_name)
		else:
			return frappe.get_doc({"doctype": "Item Attribute", "attribute_name": attribute_name})

	def _get_attribute_options(self, wc_attribute):
		"""Helper method to get attribute options"""
		if not self.woocommerce_product:
			return []
		return (
			wc_attribute["options"]
			if self.woocommerce_product.type == "variable"
			else [wc_attribute["option"]]
		)

	def _add_missing_attribute_values(self, item_attribute, options) -> bool:
		"""
		Add the options the attribute doesn't have yet. Values other products use are kept.

		Returns:
			bool: whether any value was added
		"""
		existing_values = {val.attribute_value for val in item_attribute.item_attribute_values}
		existing_abbrs = {(val.abbr or "").lower() for val in item_attribute.item_attribute_values}
		added = False
		for option in options:
			if option in existing_values:
				continue
			# ERPNext requires a unique abbreviation per attribute
			abbr = base_abbr = option.replace(" ", "")
			suffix = 1
			while abbr.lower() in existing_abbrs:
				suffix += 1
				abbr = f"{base_abbr}{suffix}"
			row = item_attribute.append("item_attribute_values")
			row.attribute_value = option
			row.abbr = abbr
			existing_values.add(option)
			existing_abbrs.add(abbr.lower())
			added = True
		return added

	def set_item_fields(self, item: SyncedItem) -> tuple[bool, SyncedItem]:
		"""
		Synchronize values from WooCommerce fields to ERPNext item fields
		based on field mappings configured in WooCommerce Server.

		Args:
			item: The ERPNext item to update

		Returns:
			tuple: (was_modified, updated_item)
		"""
		if not (item and self.woocommerce_product):
			return False, item

		# Get WooCommerce server config
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_product.woocommerce_server
		)  # type: ignore

		# Exit early if no field mappings exist
		if not wc_server.item_field_map:
			return False, item

		# Deserialize product data for JSONPath operations
		wc_product_data = self.woocommerce_product.deserialize_attributes_of_type_dict_or_list(
			self.woocommerce_product.to_dict()
		)

		item_modified = False

		# Process each field mapping
		for field_map in wc_server.item_field_map:
			# Parse ERPNext field name
			erpnext_field_parts = field_map.erpnext_field_name.split(" | ")
			erpnext_field_name = erpnext_field_parts[0]

			# Extract WooCommerce field value using JSONPath
			try:
				jsonpath_expr = parse(field_map.woocommerce_field_name)
				matches = jsonpath_expr.find(wc_product_data)

				if not matches:
					continue

				wc_field_value = matches[0].value

				# Only touch fields that differ, so an unchanged product doesn't save (and re-date) the Item
				if not values_match(item.get(erpnext_field_name), wc_field_value):
					item.set(erpnext_field_name, wc_field_value)
					item_modified = True

			except Exception as e:
				frappe.log_error(
					f"Error mapping field {field_map.woocommerce_field_name} to {erpnext_field_name}: {e!s}",
					"WooCommerce Field Mapping Error",
				)

		return item_modified, item

	def set_sync_hash(self):
		"""
		Set the last sync hash value using db.set_value, as it does not call the ORM triggers
		and it does not update the modified timestamp (by using the update_modified parameter)
		"""
		if not self.woocommerce_product or not self.item:
			return
		iws_name = self.item.item_woocommerce_server.name
		if not iws_name:
			return
		frappe.db.set_value(
			"Item WooCommerce Server",
			iws_name,
			"woocommerce_last_sync_hash",
			self.woocommerce_product.woocommerce_date_modified,
			update_modified=False,
		)


def get_list_of_wc_products(date_time_from: datetime | str | None = None) -> list[dict]:
	"""
	Fetches the WooCommerce Products (and their variations) modified since date_time_from.

	Only the identifying fields are requested, as each product is loaded in full when it is synced.

	Args:
		date_time_from: Optional datetime to filter products modified after this time

	Returns:
		List of WooCommerce Product records, as dicts
	"""
	filters = []
	if date_time_from:
		filters.append(["WooCommerce Product", "date_modified", ">", date_time_from])

	try:
		wc_products = WooCommerceProduct.get_list(
			args={
				"doctype": "WooCommerce Product",
				"filters": filters,
				"fields": ["name"],
				# This is a sync, so it needs the current list rather than a cached one
				"skip_cache": True,
				"page_length": 1000,
			}
		)

		return wc_products or []  # type: ignore

	except Exception as e:
		frappe.log_error(
			"WooCommerce Sync Error", f"Error fetching WooCommerce products: {e!s}\n{frappe.get_traceback()}"
		)
		return []


def to_item_name(wc_name: str | None) -> str | None:
	"""
	A WooCommerce product name as an Item name, which is a Data field and so capped in length
	"""
	return wc_name[:ITEM_NAME_LENGTH] if wc_name else wc_name


def values_match(erpnext_value, wc_value) -> bool:
	"""
	Compare an ERPNext and a WooCommerce value. WooCommerce sends numbers as strings ("10.00") and empty
	values as "", while ERPNext has 10.0 and None, which are the same value.
	"""
	if erpnext_value == wc_value or (erpnext_value in (None, "") and wc_value in (None, "")):
		return True
	if isinstance(erpnext_value, int | float) or isinstance(wc_value, int | float):
		try:
			return float(erpnext_value or 0) == float(wc_value or 0)
		except (TypeError, ValueError):
			return False
	return False
