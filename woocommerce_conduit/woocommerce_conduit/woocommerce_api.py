import json
from urllib.parse import urlparse

import frappe
import frappe.utils
from frappe import _
from frappe.model.document import Document
from woocommerce import API

from woocommerce_conduit.exceptions import SyncDisabledError

WC_RESOURCE_DELIMITER = "~"


class WooCommerceAPI(API):
	woocommerce_server_url: str
	woocommerce_server: str


class WooCommerceDocument(Document):
	resource: str
	name: str
	field_setter_map: dict

	def __init__(self, *args, **kwargs):
		self.init_api()
		super().__init__(*args, **kwargs)

	@staticmethod
	def _init_api() -> list[WooCommerceAPI]:
		"""
		Initialise the WooCommerce API connections for all enabled servers

		Returns:
			List of initialized WooCommerceAPI instances

		Raises:
			SyncDisabledError: If no enabled WooCommerce servers are found
		"""
		# Get all WooCommerce servers in a single query and then filter
		wc_servers = [
			server
			for server in frappe.get_all(
				"WooCommerce Server",
				fields=[
					"name",
					"woocommerce_server_url",
					"api_consumer_key",
					"api_consumer_secret",
					"enabled",
				],
				filters={"enabled": 1},
			)
		]

		# Create API instances for enabled servers
		wc_api_list = []
		for server in wc_servers:
			try:
				wc_api = WooCommerceAPI(
					url=server.woocommerce_server_url,
					consumer_key=server.api_consumer_key,
					consumer_secret=server.api_consumer_secret,
					version="wc/v3",
					timeout=40,
				)
				# Add server name for easier identification
				wc_api.woocommerce_server = server.name
				wc_api.woocommerce_server_url = server.woocommerce_server_url
				wc_api_list.append(wc_api)
			except Exception as e:
				frappe.log_error(f"Error initializing WooCommerce API for server {server.name}: {e!s}")

		if not wc_api_list:
			frappe.throw(_("At least one WooCommerce Server should be Enabled"), SyncDisabledError)

		return wc_api_list

	def __getitem__(self, key):
		"""
		Allow for dict-like behaviour when using jsonpath-ng
		"""
		return self.get(key)

	def __setitem__(self, key, value):
		"""
		Allow for dict-like behaviour when using jsonpath-ng
		"""
		self.set(key, value)

	def __contains__(self, key):
		"""
		Allow for dict-like behaviour when using jsonpath-ng
		"""
		fields = [field.fieldname for field in self.meta.fields]
		fields.append("name")
		return key in fields

	def init_api(self):
		"""
		Initialise the WooCommerce API
		"""
		self.wc_api_list = self._init_api()

	def load_from_db(self):
		"""
		Returns a single WooCommerce Record (Form view)

		Raises:
			SyncDisabledError: If synchronization is disabled
			frappe.DoesNotExistError: If the record does not exist
			ConnectionError: If there's a network issue
			ValueError: If the response cannot be parsed as JSON
		"""
		# Parse the server domain and record_id from the Document name
		wc_server_domain, record_id = get_domain_and_id_from_woocommerce_record_name(self.name)

		# Map Frappe query parameters to WooCommerce query parameters
		params = {}

		# Optimize fields selection for specific doctypes
		if self.doctype == "WooCommerce Product":
			params["_fields"] = (
				"name,id,purchasable,virtual,downloadable,status,type,description,short_description,downloads,download_limit,download_expiry,price,regular_price,sale_price,tax_status,tax_class,date_on_sale_from,date_on_sale_to,on_sale,total_sales,sku,manage_stock,sold_individually,stock_quantity,backorders,backorders_allowed,backordered,low_stock_amount,stock_status,weight,dimensions,shipping_required,shipping_taxable,shipping_class,shipping_class_id,upsell_ids,cross_sell_ids,related_ids,slug,permalink,date_created,date_modified,reviews_allowed,average_rating,rating_count,featured,parent_id,catalog_visibility,images,attributes"
			)

		# Select the relevant WooCommerce server
		try:
			self.current_wc_api = next(api for api in self.wc_api_list if wc_server_domain in api.url)
		except StopIteration:
			log_and_raise_error(error_text=f"No WooCommerce server found for domain {wc_server_domain}")

		# Get WooCommerce Record
		try:
			response = self.current_wc_api.get(f"{self.resource}/{record_id}", params=params)
			if response.status_code != 200:
				log_and_raise_error(error_text=f"API returned {response.status_code}", response=response)
			record = response.json()
		except ConnectionError as err:
			log_and_raise_error(
				exception=err,
				error_text=f"Network error when fetching WooCommerce {self.resource} #{record_id}",
			)
		except ValueError as err:
			log_and_raise_error(
				exception=err,
				error_text=f"Invalid JSON response for WooCommerce {self.resource} #{record_id}",
			)
		except Exception as err:
			error_text = f"load_from_db failed (WooCommerce {self.resource} #{record_id})"
			log_and_raise_error(exception=err, error_text=error_text)

		if "id" not in record:
			log_and_raise_error(
				error_text=f"Invalid record format: WooCommerce {self.resource} #{record_id}\nResponse: {record!s}"
			)

		record = self.pre_init_document(record, woocommerce_server_url=self.current_wc_api.url)
		record = self.after_load_from_db(record)

		super(Document, self).__init__(record)

	def after_load_from_db(self, record: dict):
		return record

	@classmethod
	def get_list_of_records(cls, args):
		"""
		Returns List of WooCommerce Records (List view and Report view).

		Fetches data from each WooCommerce server and processes it for Frappe.
		Implements efficient pagination and error handling without redundant caching.

		Args:
			args: Dictionary containing request parameters (filters, pagination, etc.)

		Returns:
			List: WooCommerce records processed for Frappe
		"""
		# Validate required args
		if not isinstance(args, dict) or not args.get("doctype"):
			frappe.log_error("WooCommerce API Error", "Invalid arguments for get_list_of_records")
			return []

		# Initialise the WC API
		try:
			wc_api_list = cls._init_api()
		except SyncDisabledError:
			frappe.msgprint(
				_("WooCommerce synchronization is disabled. Please enable at least one WooCommerce server.")
			)
			return []
		except Exception as e:
			frappe.log_error("WooCommerce API Error", f"Failed to initialize WooCommerce API: {e!s}")
			return []

		cls.doctype = args["doctype"]

		if not wc_api_list:
			return []

		cls.doctype = args["doctype"]

		# Get configuration settings
		wc_records_per_page_limit = 100

		# Map Frappe query parameters to WooCommerce query parameters
		params = {}

		# Optimize fields selection for specific doctypes
		if cls.doctype == "WooCommerce Product":
			params["_fields"] = "name,id,date_created,date_modified,type,sku,status"

		# Handle pagination
		requested = int(args.get("page_length", wc_records_per_page_limit))
		per_page = min(requested, wc_records_per_page_limit)
		offset = int(args.get("offset", 0))
		params["per_page"] = per_page

		# Map Frappe filters to WooCommerce parameters
		if args.get("filters"):
			try:
				updated_params = map_frappe_filters_to_wc_params(args["filters"])
				params.update(updated_params)
			except Exception as e:
				frappe.log_error(f"Error mapping filters: {e!s}", "WooCommerce Filter Error")

		# Initialize required variables
		all_results = []
		total_processed = 0
		max_results = args.get("max_results", 1000)  # Limit maximum records to prevent runaway queries

		# Filter servers if specified
		selected_servers = []
		if args.get("servers"):
			selected_servers = [
				wc_server for wc_server in wc_api_list if wc_server.woocommerce_server in args["servers"]
			]
		else:
			selected_servers = wc_api_list

		if not selected_servers:
			return []

		for wc_server in selected_servers:
			endpoint = args.get("endpoint", cls.resource)
			server_offset = max(0, offset - total_processed)

			if server_offset > 0:
				params["offset"] = server_offset
			else:
				params.pop("offset", None)  # Remove offset if not needed

			try:
				# Fetch records from this server
				response = wc_server.get(endpoint, params=params)

				if response.status_code != 200:
					frappe.log_error(
						f"WooCommerce API error: {response.status_code} - {response.text}",
						"WooCommerce API Error",
					)
					continue

				# Parse the response
				results = response.json()
				if not results:
					continue
			except Exception as err:
				frappe.log_error(
					f"Error fetching WooCommerce records: {err!s}\nEndpoint: {endpoint}\nParams: {params}",
					"WooCommerce API Error",
				)
				continue

			# Get total count from headers if available
			total_records_in_server = 0
			if "x-wp-total" in response.headers:
				total_records_in_server = int(response.headers["x-wp-total"])
			else:
				total_records_in_server = len(results)  # Fallback estimate

			# Process the results
			records_needed = min(requested - len(all_results), max_results - total_processed)

			while results:
				# Process this batch of records
				processed_batch = []
				for record in results:
					try:
						record = cls.pre_init_document(record=record, woocommerce_server_url=wc_server.url)
						record = cls.during_get_list_of_records(record, args)
						processed_batch.append(record)
					except Exception as e:
						frappe.log_error(
							f"Error processing record {record.get('id', 'unknown')}: {e!s}",
							"WooCommerce Record Error",
						)

				records_needed -= len(results)
				all_results.extend(processed_batch)
				total_processed += min(total_records_in_server, len(processed_batch))

				if records_needed <= 0 or len(results) < params["per_page"]:
					break

				try:
					batch_params = params.copy()
					batch_params["per_page"] = min(records_needed, params["per_page"])
					response = wc_server.get(endpoint, params=batch_params)

					if response.status_code != 200:
						break

					# Parse the response
					results = response.json()
				except Exception:
					break

			# Return the records as requested
			if args.get("as_doc"):
				try:
					return [frappe.get_doc(record) for record in all_results]
				except Exception as e:
					frappe.log_error("WooCommerce Format Error", f"Error converting to Frappe docs: {e!s}")
					return []
			else:
				return all_results

		return []

	@classmethod
	def during_get_list_of_records(cls, record: dict, args):
		return record

	@classmethod
	def pre_init_document(cls, record: dict, woocommerce_server_url: str):
		"""
		Set values on dictionary that are required for frappe Document initialisation

		Args:
			record: The WooCommerce record
			woocommerce_server_url: The URL of the WooCommerce server

		Returns:
			Dict: The processed record
		"""
		record = cls._map_field_names(record)
		record = cls._set_metadata(record)
		record = cls._set_server_info(record, woocommerce_server_url)
		record = cls._set_document_identity(record)
		record = cls._serialize_complex_fields(record)

		return record

	@classmethod
	def _map_field_names(cls, record: dict) -> dict:
		"""Map WooCommerce field names to Frappe field names"""
		if cls.field_setter_map:
			for new_key, old_key in cls.field_setter_map.items():
				record[new_key] = record.get(old_key, None)
		return record

	@classmethod
	def _set_metadata(cls, record: dict) -> dict:
		"""Set metadata fields on the record"""
		if "date_modified" in record:
			record["modified"] = record["date_modified"]
			record["woocommerce_date_created"] = record["date_created"]
			record["woocommerce_date_modified"] = record["date_modified"]
		return record

	@classmethod
	def _set_server_info(cls, record: dict, woocommerce_server_url: str) -> dict:
		"""Set server information on the record"""
		server_domain = urlparse(woocommerce_server_url).netloc
		record["woocommerce_server"] = server_domain
		return record

	@classmethod
	def _set_document_identity(cls, record: dict) -> dict:
		"""Set document identity fields"""
		record["name"] = generate_woocommerce_record_name_from_domain_and_id(
			domain=record["woocommerce_server"], resource_id=record["id"]
		)
		record["doctype"] = cls.doctype
		return record

	@classmethod
	def _serialize_complex_fields(cls, record: dict) -> dict:
		"""Serialize complex fields (dict, list) to JSON strings"""
		return cls.serialize_attributes_of_type_dict_or_list(record)

	def to_dict(self):
		"""
		Convert this Document to a dict
		"""
		doc_dict = {field.fieldname: self.get(field.fieldname) for field in self.meta.fields}
		doc_dict["name"] = self.name  # name field is not in meta.fields
		return doc_dict

	@classmethod
	def serialize_attributes_of_type_dict_or_list(cls, obj):
		"""
		Serializes the dictionary and list attributes of a given object into JSON format.

		This function iterates over the fields of the input object that are expected to be in JSON format,
		and if the field is present in the object, it transforms the field's value into a JSON-formatted string.
		"""
		json_fields = cls.get_json_fields()
		for field in json_fields:
			if field.fieldname in obj:
				obj[field.fieldname] = json.dumps(obj[field.fieldname])
		return obj

	@classmethod
	def deserialize_attributes_of_type_dict_or_list(cls, obj):
		"""
		Deserializes the dictionary and list attributes of a given object from JSON format.

		This function iterates over the fields of the input object that are expected to be in JSON format,
		and if the field is present in the object, it transforms the field's value from a JSON-formatted string.
		"""
		json_fields = cls.get_json_fields()
		for field in json_fields:
			if obj.get(field.fieldname):
				obj[field.fieldname] = json.loads(obj[field.fieldname])
		return obj

	@classmethod
	def get_json_fields(cls):
		"""
		Returns a list of fields that have been defined with type "JSON"
		"""
		fields = frappe.db.get_all(
			"DocField",
			{"parent": cls.doctype, "fieldtype": "JSON"},
			["name", "fieldname", "fieldtype"],
		)

		return fields

	# use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
	# nosemgrep
	@classmethod
	def get_count_of_records(cls, args) -> int:
		"""
		Returns count of WooCommerce Records across all enabled servers

		Args:
			args: Dictionary containing request parameters (filters, etc.)

		Returns:
			int: Total count of records

		Raises:
			SyncDisabledError: If synchronization is disabled
		"""
		# Initialize the WC API
		try:
			wc_api_list = cls._init_api()
		except SyncDisabledError:
			frappe.msgprint(
				_("WooCommerce synchronization is disabled. Please enable at least one WooCommerce server.")
			)
			return 0

		total_count = 0

		# Get counts from each server
		for wc_server in wc_api_list:
			try:
				response = wc_server.get(cls.resource, params={"per_page": 1, "_fields": "name"})

				if response.status_code != 200:
					frappe.log_error(
						f"WooCommerce API error: {response.status_code} - {response.text}",
						"WooCommerce API Error",
					)
					continue

				if "x-wp-total" in response.headers:
					total_count += int(response.headers["x-wp-total"])

			except Exception as err:
				frappe.log_error(
					f"Error getting count from {wc_server.url}: {err!s}", "WooCommerce Count Error"
				)
		return total_count


def generate_woocommerce_record_name_from_domain_and_id(
	domain: str, resource_id: str | int, delimiter: str = WC_RESOURCE_DELIMITER
) -> str:
	"""
	Generate a name for a woocommerce resource, based on domain and resource_id.

	Args:
		domain: WooCommerce server domain
		resource_id: WooCommerce resource ID
		delimiter: Character used to separate domain and ID

	Returns:
		str: Combined name e.g. "site1.example.com~11"
	"""
	return f"{domain}{delimiter}{resource_id}"


def log_and_raise_error(exception=None, error_text=None, response=None):
	"""
	Create an "Error Log" and raise error

	Args:
		exception: Original exception that occurred
		error_text: Custom error message
		response: API response object containing error details

	Raises:
		Original exception or a new frappe.throw exception
	"""
	# Build detailed error message
	error_parts = []

	if exception:
		error_parts.append(frappe.get_traceback())

	if error_text:
		error_parts.append(error_text)

	if response is not None:
		error_parts.append(
			f"Response Code: {response.status_code}\n"
			f"Response Text: {response.text}\n"
			f"Request URL: {response.request.url}\n"
			f"Request Body: {response.request.body}"
		)

	error_message = "\n".join(error_parts)

	# Log the error
	log = frappe.log_error("WooCommerce Error", error_message)
	log_link = frappe.utils.get_link_to_form("Error Log", log.name)  # type: ignore

	# Throw error
	frappe.throw(
		msg=_("Something went wrong while connecting to WooCommerce. See Error Log {0}").format(log_link),
		title=_("WooCommerce Error"),
	)

	# Re-raise original exception if provided
	if exception:
		raise exception


def get_domain_and_id_from_woocommerce_record_name(
	name: str, delimiter: str = WC_RESOURCE_DELIMITER
) -> tuple[str, int]:
	"""
	Get domain and record_id from woocommerce_record name

	Args:
		name: Combined resource name (e.g. "site1.example.com~11")
		delimiter: Character that separates domain and ID

	Returns:
		Tuple[str, int]: Domain and record ID
	"""
	parts = name.split(delimiter, 1)
	domain, record_id_str = parts
	return domain, int(record_id_str)


def map_frappe_filters_to_wc_params(filters):
	"""
	Maps Frappe filters to WooCommerce API parameters

	This handles the standard filters that Frappe will pass to get_list
	"""
	params = {}
	standard_mappings = {
		"date_modified": {">": "modified_after", "<": "modified_before"},
		"date_created": {">": "after", "<": "before"},
		# Add more mappings as needed
	}

	for filter in filters:
		doctype, field, operator, value = filter

		# Handle standard mappings
		if field in standard_mappings and operator in standard_mappings[field]:
			params[standard_mappings[field][operator]] = value
			continue

		# Handle specific cases
		if field in ["name", "woocommerce_name"] and operator == "like":
			params["search"] = value.strip("%")
		elif field == "woocommerce_id" and operator == "=":
			params["include"] = [value]
		else:
			frappe.throw(f"Unsupported operator '{operator}' for field '{field}'")

	return params
