import json
from dataclasses import dataclass
from datetime import datetime

import frappe
from erpnext.stock.doctype.item.item import Item
from frappe import ValidationError, _, _dict
from frappe.query_builder import Criterion
from frappe.utils import get_datetime, now
from jsonpath_ng.ext import parse

from woocommerce_conduit.exceptions import SyncDisabledError
from woocommerce_conduit.tasks.sync import SynchroniseWooCommerce
from woocommerce_conduit.woocommerce_conduit.doctype.item_woocommerce_server.item_woocommerce_server import (
	ItemWooCommerceServer,
)
from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_settings.woocommerce_settings import (
	WooCommerceSettings,
)
from woocommerce_conduit.woocommerce_conduit.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


class SyncedItem(Item):
	woocommerce_servers: list[ItemWooCommerceServer]


def run_item_sync_from_hook(doc, method):
	"""
	Intended to be triggered by a Document Controller hook from Item
	"""
	if doc.doctype == "Item" and not doc.flags.get("created_by_sync", None) and doc.woocommerce_servers:
		frappe.msgprint(
			_("Background sync to WooCommerce triggered for {0} {1}").format(frappe.bold(doc.name), method),
			indicator="blue",
			alert=True,
		)
		frappe.enqueue(clear_sync_hash_and_run_item_sync, item_code=doc.name)


def sync_woocommerce_products_modified_since(date_time_from=None):
	"""
	Get list of WooCommerce products modified since date_time_from
	"""
	settings: WooCommerceSettings = frappe.get_cached_doc("WooCommerce Settings")  # type: ignore

	if not date_time_from:
		date_time_from = getattr(settings, "wc_last_sync_date_items", None)

	wc_products = get_list_of_wc_products(date_time_from=date_time_from)

	for wc_product in wc_products:
		try:
			run_item_sync(woocommerce_product=wc_product, enqueue=True)
		# Skip items with errors, as these exceptions will be logged
		except Exception as e:
			frappe.log_error("Item sync error", f"There was an exception when syncing an item: {e!s}")
			break

	settings.reload()
	settings.wc_last_sync_date_items = now()
	settings.flags.ignore_mandatory = True
	settings.save()


@frappe.whitelist()
def run_item_sync(
	item_code: str | None = None,
	item: SyncedItem | None = None,
	woocommerce_product_name: str | None = None,
	woocommerce_product: WooCommerceProduct | None = None,
	enqueue: bool = False,
) -> tuple[SyncedItem | None, WooCommerceProduct | None]:
	"""
	Synchronize an ERPNext Item with its corresponding WooCommerce Product.

	This function serves as a central dispatcher for item synchronization operations.
	It can be initiated from either the ERPNext side (using item_code/item) or
	the WooCommerce side (using woocommerce_product_name/woocommerce_product).

	Parameters:
	    item_code: ERPNext Item Code to synchronize
	    item: ERPNext Item document to synchronize
	    woocommerce_product_name: Name of WooCommerce Product to synchronize
	    woocommerce_product: WooCommerce Product document to synchronize
	    enqueue: Whether to process synchronization in background

	Returns:
	    tuple: (ERPNext Item, WooCommerce Product) after synchronization

	Raises:
	    ValueError: If no valid input parameters are provided
	    ValidationError: If no WooCommerce Servers are defined for the item
	"""
	# Initialize sync object reference
	sync = None
	erpnext_item = item  # type: ignore
	wc_product = woocommerce_product

	# Validate inputs - at least one parameter should be provided
	if not any([item_code, erpnext_item, woocommerce_product_name, wc_product]):
		raise ValueError(
			"At least one of item_code, item, woocommerce_product_name, or woocommerce_product parameters is required"
		)

	# Case 1: Sync initiated from WooCommerce side
	if woocommerce_product_name or wc_product:
		# Check if we need to load the product from the database
		load_full_product = False

		# If only name provided, we need to load the product
		if not wc_product and woocommerce_product_name:
			load_full_product = True
			lookup_name = woocommerce_product_name
		# If product provided but might be a partial/list view product
		elif wc_product:
			lookup_name = wc_product.get("name")
			# Check for essential fields that would only exist in a full product
			# List view products typically only have name, id, date_created, date_modified, type, sku, status
			# Full products have many more fields like description, price, stock_quantity, etc.
			required_full_fields = ["description", "price", "regular_price", "stock_status"]

			# If any of these fields are missing or None, we need to reload
			if isinstance(wc_product, dict):
				load_full_product = any(not wc_product.get(field) for field in required_full_fields)
			else:
				load_full_product = any(
					not getattr(wc_product, field, None) for field in required_full_fields
				)

		if load_full_product:
			try:
				# Load the full product with all fields from the database
				full_wc_product: WooCommerceProduct = frappe.get_doc(
					{"doctype": "WooCommerce Product", "name": lookup_name}
				)  # type: ignore
				full_wc_product.load_from_db()

				# Validate WooCommerce product has required fields
				if not full_wc_product.woocommerce_server or not full_wc_product.woocommerce_id:
					raise ValueError(
						f"WooCommerce Product {full_wc_product.name} is missing required fields: "
						f"server={full_wc_product.woocommerce_server}, id={full_wc_product.woocommerce_id}"
					)

				wc_product = full_wc_product

			except frappe.DoesNotExistError:
				frappe.throw(_(f"WooCommerce Product {lookup_name} not found"))

		# Initialize synchronization
		sync = SynchroniseItem(woocommerce_product=wc_product)

		# Execute sync now or in background
		if enqueue:
			frappe.enqueue(sync.run, queue="long", timeout=300, job_name=f"Sync WC Product {wc_product.name}")  # type: ignore
		else:
			sync.run()

	# Case 2: Sync initiated from ERPNext side
	elif item_code or erpnext_item:
		# Load item if only the code was provided
		if not erpnext_item and item_code:
			try:
				erpnext_item: SyncedItem = frappe.get_doc("Item", item_code)  # type: ignore
			except frappe.DoesNotExistError:
				frappe.throw(_(f"Item {item_code} not found"))

		# Validate item has WooCommerce servers configured
		if not erpnext_item.woocommerce_servers:
			frappe.throw(_(f"No WooCommerce Servers defined for Item {erpnext_item.name or item_code}"))

		# Sync with each linked WooCommerce server
		for idx, wc_server in enumerate(erpnext_item.woocommerce_servers):
			# Validate server entry has required fields
			if not wc_server.woocommerce_server:
				frappe.msgprint(
					_(f"WooCommerce Server not specified for entry #{idx + 1} in Item {erpnext_item.name}"),
					indicator="red",
					alert=True,
				)
				continue

			# Initialize synchronization for this server
			sync = SynchroniseItem(
				item=ERPNextItemToSync(item=erpnext_item, item_woocommerce_server_idx=wc_server.idx)
			)

			# Execute sync now or in background
			if enqueue:
				frappe.enqueue(
					sync.run,
					queue="long",
					timeout=300,
					job_name=f"Sync Item {erpnext_item.name} with {wc_server.woocommerce_server}",
				)
			else:
				sync.run()

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


class SynchroniseItem(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of WooCommerce Product with ERPNext Item
	"""

	def __init__(
		self,
		servers: list[WooCommerceServer | _dict] | None = None,
		item: ERPNextItemToSync | None = None,
		woocommerce_product: WooCommerceProduct | None = None,
	) -> None:
		super().__init__(servers)
		self.item = item
		self.woocommerce_product = woocommerce_product  # type: ignore
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

			wc_products = get_list_of_wc_products(item=self.item)
			if not wc_products:
				raise ValueError(
					f"No WooCommerce Product found with ID {self.item.item_woocommerce_server.woocommerce_id} on {self.item.item_woocommerce_server.woocommerce_server}"
				)
			self.woocommerce_product: WooCommerceProduct = frappe.get_doc(
				{"doctype": "WooCommerce Product", "name": wc_products[0]["name"]}  # type: ignore
			)
			self.woocommerce_product.load_from_db()

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

		iws: ItemWooCommerceServer = frappe.qb.DocType("Item WooCommerce Server")  # type: ignore
		itm: Item = frappe.qb.DocType("Item")  # type: ignore

		and_conditions = [
			iws.woocommerce_server == self.woocommerce_product.woocommerce_server,
			iws.woocommerce_id == self.woocommerce_product.woocommerce_id,
		]

		item_codes = (
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
		Syncronise Item between ERPNext and WooCommerce
		"""
		if self.item and not self.woocommerce_product:
			# create missing item in WooCommerce
			pass
		elif self.woocommerce_product and not self.item:
			# create missing item in ERPNext
			self.create_item()
		elif self.item and self.woocommerce_product and self.item.item_woocommerce_server.enable_sync:
			# both exist, check sync hash
			if (
				self.woocommerce_product.woocommerce_date_modified
				!= self.item.item_woocommerce_server.woocommerce_last_sync_hash
			):
				if get_datetime(self.woocommerce_product.woocommerce_date_modified) > get_datetime(
					self.item.item.modified
				):  # type: ignore
					self.update_item()
				if get_datetime(self.woocommerce_product.woocommerce_date_modified) < get_datetime(
					self.item.item.modified
				):  # type: ignore
					self.update_woocommerce_product()

	def update_item(self):
		"""
		Update the ERPNext Item with fields from it's corresponding WooCommerce Product
		"""
		if not self.woocommerce_product or not self.item:
			return

		item_dirty = False
		if self.item.item.item_name != self.woocommerce_product.woocommerce_name:
			self.item.item.item_name = self.woocommerce_product.woocommerce_name
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

		if item_dirty or fields_updated:
			self.item.item.flags.created_by_sync = True
			self.item.item.save()

		self.set_sync_hash()

	def update_woocommerce_product(self):
		"""
		Update the WooCommerce Product with fields from it's corresponding ERPNext Item
		"""
		if not self.woocommerce_product or not self.item:
			return

		wc_product_dirty = False

		# Update properties
		if self.item.item.item_name and self.woocommerce_product.woocommerce_name != self.item.item.item_name:
			self.woocommerce_product.woocommerce_name = self.item.item.item_name
			wc_product_dirty = True

		product_fields_changed = self.set_product_fields()
		if product_fields_changed:
			wc_product_dirty = True

		if wc_product_dirty:
			self.woocommerce_product.save()

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

		# Set mapped fields from WooCommerce
		modified, item = self.set_item_fields(item=item)

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

		# Update sync hash
		self.set_sync_hash()

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
			# Check if parent exists and set variant_of
			parent_item = self._get_or_create_parent_item()
			if parent_item:
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
		parent_item, parent_wc_product = run_item_sync(woocommerce_product_name=woocommerce_product_name)
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
		item.item_name = self.woocommerce_product.woocommerce_name

		# Link to WooCommerce server
		row = item.append("woocommerce_servers")
		row.woocommerce_id = self.woocommerce_product.woocommerce_id
		row.woocommerce_server = wc_server.name

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

			# Update attribute values if needed
			if self._should_update_attribute_values(item_attribute, options):
				self._update_attribute_values(item_attribute, options)

			# Save the attribute
			item_attribute.flags.ignore_mandatory = True
			if not item_attribute.name:
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

	def _should_update_attribute_values(self, item_attribute, options):
		"""Helper method to determine if attribute values need updating"""
		return len(item_attribute.item_attribute_values) == 0 or set(options) != set(
			[val.attribute_value for val in item_attribute.item_attribute_values]
		)

	def _update_attribute_values(self, item_attribute, options):
		"""Helper method to update attribute values"""
		item_attribute.item_attribute_values = []
		for option in options:
			row = item_attribute.append("item_attribute_values")
			row.attribute_value = option
			row.abbr = option.replace(" ", "")

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

				# Update ERPNext item field
				setattr(item, erpnext_field_name, wc_field_value)
				item_modified = True

			except Exception as e:
				frappe.log_error(
					f"Error mapping field {field_map.woocommerce_field_name} to {erpnext_field_name}: {e!s}",
					"WooCommerce Field Mapping Error",
				)

		return item_modified, item

	def set_product_fields(self) -> bool:
		"""
		Synchronize values from ERPNext item fields to WooCommerce product fields
		based on field mappings configured in WooCommerce Server.

		Returns:
			tuple: (was_modified, updated_woocommerce_product)
		"""
		if not self.woocommerce_product or not self.item:
			return False

		# Get WooCommerce server config
		wc_server: WooCommerceServer = frappe.get_cached_doc(
			"WooCommerce Server", self.woocommerce_product.woocommerce_server
		)  # type: ignore

		# Exit early if no field mappings exist
		if not wc_server.item_field_map:
			return False

		# Deserialize WooCommerce product attributes for JSONPath operations
		wc_product_data = self.woocommerce_product.deserialize_attributes_of_type_dict_or_list(
			self.woocommerce_product
		)

		wc_product_modified = False

		# Process each field mapping
		for field_map in wc_server.item_field_map:
			# Parse ERPNext field name
			erpnext_field_parts = field_map.erpnext_field_name.split(" | ")
			erpnext_field_name = erpnext_field_parts[0]

			# Get item field value
			try:
				erpnext_field_value = getattr(self.item.item, erpnext_field_name)

				# Find target field in WooCommerce product using JSONPath
				jsonpath_expr = parse(field_map.woocommerce_field_name)
				matches = jsonpath_expr.find(wc_product_data)

				if not matches:
					if self.woocommerce_product.name:
						# Strict check for existing products - field should exist
						raise ValueError(
							_("Field <code>{0}</code> not found in WooCommerce Product {1}").format(
								field_map.woocommerce_field_name, self.woocommerce_product.name
							)
						)
					else:
						# For new products, the field might not exist yet
						continue

				wc_field_value = matches[0].value

				# Update WooCommerce field if values differ
				if erpnext_field_value != wc_field_value:
					jsonpath_expr.update(wc_product_data, erpnext_field_value)
					wc_product_modified = True

			except AttributeError as e:
				frappe.log_error(
					f"ERPNext field {erpnext_field_name} not found on item {self.item.item.name}: {e!s}",
					"WooCommerce Field Mapping Error",
				)
			except Exception as e:
				frappe.log_error(
					f"Error mapping field {erpnext_field_name} to {field_map.woocommerce_field_name}: {e!s}",
					"WooCommerce Field Mapping Error",
				)

		# Re-serialize if modified
		if wc_product_modified:
			self.woocommerce_product = self.woocommerce_product.serialize_attributes_of_type_dict_or_list(
				wc_product_data
			)

		return wc_product_modified

	def set_sync_hash(self):
		"""
		Set the last sync hash value using db.set_value, as it does not call the ORM triggers
		and it does not update the modified timestamp (by using the update_modified parameter)
		"""
		if not self.woocommerce_product or not self.item:
			return
		frappe.db.set_value(
			"Item WooCommerce Server",
			self.item.item_woocommerce_server.name,
			"woocommerce_last_sync_hash",
			self.woocommerce_product.woocommerce_date_modified,
			update_modified=False,
		)


def get_list_of_wc_products(
	item: ERPNextItemToSync | None = None, date_time_from: datetime | None = None
) -> list[WooCommerceProduct]:
	"""
	Fetches a list of WooCommerce Products within a specified date range or linked with an Item.

	This function efficiently retrieves WooCommerce products using the built-in pagination
	and caching mechanisms of the WooCommerceProduct class.

	Args:
		item: Optional ERPNext item to sync with WooCommerce
		date_time_from: Optional datetime to filter products modified after this time

	Returns:
		List of WooCommerceProduct documents
	"""
	# Build filters
	filters = []
	servers = None

	if date_time_from:
		filters.append(["WooCommerce Product", "date_modified", ">", date_time_from])

	if item:
		if not hasattr(item, "item_woocommerce_server") or not item.item_woocommerce_server:
			frappe.log_error(
				"WooCommerce Sync Error", f"Item {item.item.name} has no WooCommerce server configuration"
			)
			return []

		filters.append(["WooCommerce Product", "id", "=", item.item_woocommerce_server.woocommerce_id])
		servers = [item.item_woocommerce_server.woocommerce_server]

	try:
		# Leverage the WooCommerceProduct.get_list method which already has caching
		woocommerce_product: WooCommerceProduct = frappe.get_doc({"doctype": "WooCommerce Product"})  # type: ignore

		# Use a single call with appropriate arguments
		wc_products = woocommerce_product.get_list(
			args={
				"doctype": "WooCommerce Product",
				"filters": filters,
				"servers": servers,
				"as_doc": True,
				# Let the API handle pagination efficiently
				# Set a reasonable limit for maximum records
				"page_length": 1 if item else 1000,
			}
		)

		return wc_products or []  # type: ignore

	except Exception as e:
		frappe.log_error(
			"WooCommerce Sync Error", f"Error fetching WooCommerce products: {e!s}\n{frappe.get_traceback()}"
		)
		return []


def clear_sync_hash_and_run_item_sync(item_code: str):
	"""
	Clear the last sync hash value using db.set_value, as it does not call the ORM triggers
	and it does not update the modified timestamp (by using the update_modified parameter)
	"""

	iws: ItemWooCommerceServer = frappe.qb.DocType("Item WooCommerce Server")  # type: ignore

	iwss = (
		frappe.qb.from_(iws).where(iws.enable_sync == 1).where(iws.parent == item_code).select(iws.name)
	).run(as_dict=True)

	for iws in iwss:
		frappe.db.set_value(
			"Item WooCommerce Server",
			iws.name,
			"woocommerce_last_sync_hash",
			None,
			update_modified=False,
		)

	if len(iwss) > 0:
		frappe.enqueue(
			run_item_sync,
			item_code=item_code,
			enqueue=True,
			queue="long",  # Use appropriate queue
			timeout=1500,  # Set appropriate timeout
			job_name=f"Sync Item {item_code}",  # Provide useful job name
			now=frappe.flags.in_test,  # Run immediately in tests
		)
