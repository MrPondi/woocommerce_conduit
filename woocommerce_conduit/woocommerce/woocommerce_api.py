import json
import re
import traceback
from typing import NoReturn, Self
from urllib.parse import urlparse

import frappe
import frappe.utils
from frappe import _
from frappe.model import no_value_fields
from frappe.model.document import Document
from frappe.utils.password import get_decrypted_password
from woocommerce import API

from woocommerce_conduit.exceptions import SyncDisabledError

WC_RESOURCE_DELIMITER = "~"


class WooCommerceAPI(API):
	"""WooCommerce API with Request Logging."""

	def _API__request(self, method, endpoint, data, params=None, **kwargs):
		"""Override _request method to also create a 'WooCommerce Request Log'"""
		result = None
		try:
			result = super()._API__request(method, endpoint, data, params, **kwargs)  # type: ignore
			if not frappe.flags.in_test:
				frappe.enqueue(
					"woocommerce_conduit.tasks.utils.log_woocommerce_request",
					url=self.url,
					endpoint=endpoint,
					request_method=method,
					params=params,
					data=data,
					res=result,
					traceback="".join(traceback.format_stack(limit=8)),
				)
			return result
		except Exception:
			if not frappe.flags.in_test:
				frappe.enqueue(
					"woocommerce_conduit.tasks.utils.log_woocommerce_request",
					url=self.url,
					endpoint=endpoint,
					request_method=method,
					params=params,
					data=data,
					res=result,
					traceback="".join(traceback.format_stack(limit=8)),
				)
			raise

	woocommerce_server_url: str
	woocommerce_server: str


# Frappe fields that are derived on our side, so there is nothing to request from WooCommerce for them
NON_WOOCOMMERCE_FIELDS = {
	"woocommerce_server",
	"owner",
	"modified_by",
	"docstatus",
	"idx",
	"doctype",
	"_user_tags",
	"_comments",
	"_assign",
	"_liked_by",
}

# Frappe fieldname -> the WooCommerce fields needed to fill it
COMMON_FIELD_GETTER_MAP = {
	"name": ["id"],
	"woocommerce_id": ["id"],
	"woocommerce_name": ["name"],
	"woocommerce_date_created": ["date_created"],
	"woocommerce_date_modified": ["date_modified"],
	"creation": ["date_created"],
	"modified": ["date_modified"],
}


# Frappe fieldname -> `orderby` supported by the WooCommerce REST API
COMMON_ORDERBY_MAP = {
	"name": "id",
	"woocommerce_id": "id",
	"woocommerce_name": "title",
	"slug": "slug",
	"creation": "date",
	"woocommerce_date_created": "date",
}


def parse_order_by(order_by: str | None) -> list[tuple[str, bool]]:
	"""
	Parse a Frappe order_by, for example "`tabWooCommerce Product`.`modified` desc, sku asc",
	into [(fieldname, descending), ...]
	"""
	clauses = []
	for clause in (order_by or "").split(","):
		# The table name in "`tabWooCommerce Product`.`modified`" contains a space, so only look at the end
		match = re.match(r"^(.+?)(?:\s+(asc|desc))?$", clause.strip(), flags=re.IGNORECASE)
		if not match or not (fieldname := parse_frappe_field(match.group(1))):
			continue
		clauses.append((fieldname, (match.group(2) or "").lower() == "desc"))
	return clauses


def sort_records(records: list[dict], order_by: list[tuple[str, bool]]) -> list[dict]:
	"""
	Order records on the fields of a parsed order_by. A stable sort per clause, last clause first, so
	the first clause wins. Fields the records don't carry are left alone.
	"""

	def sort_key(fieldname: str):
		def key(record: dict):
			value = record.get(fieldname)
			if value is None:
				return (1, 0, "")
			try:
				return (0, float(value), "")
			except (TypeError, ValueError):
				return (0, 0, str(value))

		return key

	for fieldname, descending in reversed(order_by):
		if any(fieldname in record for record in records):
			records = sorted(records, key=sort_key(fieldname), reverse=descending)
	return records


def parse_frappe_field(field: str) -> str | None:
	"""
	Extract the fieldname from a field as requested by Frappe, for example
	"`tabWooCommerce Product`.`sku` as code" -> "sku". Returns None for aggregates and other expressions.
	"""
	field = re.split(r"\s+as\s+", field.strip(), flags=re.IGNORECASE)[0]
	if "(" in field:
		return None
	return field.split(".")[-1].strip('`" ') or None


class WooCommerceDocument(Document):
	resource: str
	field_setter_map: dict
	# Fieldnames that don't map one-to-one onto a WooCommerce field
	field_getter_map: dict[str, list[str]] = {}  # noqa: RUF012
	# Always requested, as the document identity and sorting depend on them
	mandatory_wc_fields: list[str] = ["id", "date_created", "date_modified"]  # noqa: RUF012
	# Fieldnames WooCommerce can order by, as the `orderby` it expects
	orderby_map: dict[str, str] = {}  # noqa: RUF012

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
		wc_servers = frappe.get_all(
			"WooCommerce Server",
			fields=["name", "woocommerce_server_url"],
			filters={"enabled": 1},
		)

		# Create API instances for enabled servers
		wc_api_list = []
		for server in wc_servers:
			try:
				wc_api = WooCommerceAPI(
					url=server.woocommerce_server_url,
					consumer_key=get_decrypted_password(
						"WooCommerce Server", server.name, "api_consumer_key", raise_exception=False
					),
					consumer_secret=get_decrypted_password(
						"WooCommerce Server", server.name, "api_consumer_secret", raise_exception=False
					),
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

	def init_api(self):
		"""
		Initialise the WooCommerce API
		"""
		self.wc_api_list = self._init_api()

	def load_from_db(self) -> Self:
		"""
		Returns a single WooCommerce Record (Form view)

		Raises:
			SyncDisabledError: If synchronization is disabled
			frappe.DoesNotExistError: If the record does not exist
			ConnectionError: If there's a network issue
			ValueError: If the response cannot be parsed as JSON
		"""
		# Parse the server domain and record_id from the Document name
		wc_server_domain, record_id = get_domain_and_id_from_woocommerce_record_name(str(self.name))

		# Map Frappe query parameters to WooCommerce query parameters
		params = {}

		# Request the fields of the DocType
		params["_fields"] = self.get_wc_fields()

		# Select the relevant WooCommerce server
		try:
			self.current_wc_api = next(
				api for api in self.wc_api_list if wc_server_domain in api.woocommerce_server_url
			)
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

		record = self.pre_init_document(
			record, woocommerce_server_url=self.current_wc_api.woocommerce_server_url
		)
		record = self.after_load_from_db(record)

		super(Document, self).__init__(record)
		return self

	def after_load_from_db(self, record: dict):
		return record

	def db_update(self):
		"""
		Write the fields that changed since the record was loaded back to WooCommerce. The subclass's
		before_db_update decides which fields may be written at all.

		`Document.save` loads the current WooCommerce record into `_doc_before_save` (and `check_if_latest`
		rejects the save if it was modified in WooCommerce since this copy was loaded), so only the
		difference against that is sent.
		"""
		changes = self.get_changed_values()
		if not changes:
			return

		changes = self.before_db_update(changes)
		if not changes:
			return

		wc_server_domain, record_id = get_domain_and_id_from_woocommerce_record_name(str(self.name))
		try:
			self.current_wc_api = next(
				api for api in self.wc_api_list if wc_server_domain in api.woocommerce_server_url
			)
		except StopIteration:
			log_and_raise_error(error_text=f"No WooCommerce server found for domain {wc_server_domain}")

		endpoint = self.get_update_endpoint(record_id)
		try:
			response = self.current_wc_api.put(endpoint, changes, params={"_fields": "id,date_modified"})
		except Exception as err:
			log_and_raise_error(exception=err, error_text=f"db_update failed ({endpoint})")
		if response.status_code != 200:
			log_and_raise_error(error_text=f"db_update failed ({endpoint})", response=response)

		# Keep the new modified date, so the sync hash written after this save matches WooCommerce
		date_modified = response.json().get("date_modified")
		if date_modified:
			self.woocommerce_date_modified = date_modified
			self.modified = date_modified

	# save() runs these before db_update. The record lives in WooCommerce and isn't stored here, so there are
	# no column lengths to respect and WooCommerce decides which values are valid. Otherwise a name over 140
	# characters or a "future" status would stop a price update. Images in the description must not turn
	# into ERPNext Files either.
	def _validate_length(self):
		pass

	def _validate_selects(self):
		pass

	def _extract_images_from_text_editor(self):
		pass

	def get_changed_values(self) -> dict:
		"""
		The fields whose value differs from `_doc_before_save`, with JSON fields deserialised and numbers
		compared by value, so "10" and 10.0 or an indented and a compact JSON string are not a change
		"""
		before = getattr(self, "_doc_before_save", None)
		before_values = self.deserialize_attributes_of_type_dict_or_list(before.to_dict()) if before else {}
		values = self.deserialize_attributes_of_type_dict_or_list(self.to_dict())

		changes = {}
		for df in self.meta.fields:
			if df.fieldtype in no_value_fields or df.fieldname not in values:
				continue
			value, old_value = values.get(df.fieldname), before_values.get(df.fieldname)
			if df.fieldtype in ("Currency", "Float", "Int", "Percent", "Check"):
				if frappe.utils.flt(value) == frappe.utils.flt(old_value):
					continue
			elif value == old_value or (value or None) == (old_value or None):
				continue
			changes[df.fieldname] = value
		return changes

	def before_db_update(self, changes: dict) -> dict:
		"""
		Convert the changed Frappe fields into the WooCommerce request body
		"""
		return {self.field_setter_map.get(key, key): value for key, value in changes.items()}

	def get_update_endpoint(self, record_id: int) -> str:
		return f"{self.resource}/{record_id}"

	@classmethod
	def get_wc_fields(
		cls, frappe_fields: list[str] | str | None = None, extra: list[str] | None = None
	) -> str:
		"""
		Build the WooCommerce `_fields` parameter for the Frappe fields that were requested

		Args:
			frappe_fields: Fields as passed by Frappe. All fields of the DocType when empty or "*"
			extra: Additional WooCommerce fields to request

		Returns:
			Comma separated WooCommerce field names
		"""
		meta = frappe.get_meta(cls.doctype)
		all_fieldnames = [df.fieldname for df in meta.fields if df.fieldtype not in no_value_fields]

		if isinstance(frappe_fields, str):
			frappe_fields = [frappe_fields]
		requested = [parse_frappe_field(field) for field in frappe_fields or []]
		fieldnames = all_fieldnames if not requested or "*" in requested else requested

		getter_map = {**COMMON_FIELD_GETTER_MAP, **cls.field_getter_map}
		wc_fields = list(cls.mandatory_wc_fields) + list(extra or [])
		for fieldname in fieldnames:
			if not fieldname or fieldname in NON_WOOCOMMERCE_FIELDS:
				continue
			if fieldname in getter_map:
				wc_fields.extend(getter_map[fieldname])
			elif fieldname in all_fieldnames:
				wc_fields.append(fieldname)

		return ",".join(dict.fromkeys(wc_fields))

	@classmethod
	def get_wc_orderby(cls, fieldname: str) -> str | None:
		"""
		The WooCommerce `orderby` for a Frappe fieldname, or None if WooCommerce can't order by it
		"""
		return {**COMMON_ORDERBY_MAP, **cls.orderby_map}.get(fieldname)

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

		# With `strict`, a failing server raises instead of being logged and skipped, so the caller can tell
		# "nothing found" from "nothing fetched"
		strict = bool(args.get("strict"))

		# Initialise the WC API
		try:
			wc_api_list = cls._init_api()
		except SyncDisabledError:
			if strict:
				raise
			frappe.msgprint(
				_("WooCommerce synchronization is disabled. Please enable at least one WooCommerce server.")
			)
			return []
		except Exception as e:
			if strict:
				raise
			frappe.log_error("WooCommerce API Error", f"Failed to initialize WooCommerce API: {e!s}")
			return []

		# Get configuration settings
		wc_records_per_page_limit = 100

		# Map Frappe query parameters to WooCommerce query parameters
		params = {}

		# Only request the fields that Frappe asked for
		params["_fields"] = cls.get_wc_fields(args.get("fields"), args.get("extra_wc_fields"))

		# Handle pagination
		requested = int(args.get("page_length", wc_records_per_page_limit))
		per_page = min(requested, wc_records_per_page_limit)
		offset = int(args.get("start", 0))
		params["per_page"] = per_page

		# Order on the WooCommerce side where it supports the field, so the right page is returned
		order_by = parse_order_by(args.get("order_by"))
		if order_by:
			field, descending = order_by[0]
			if wc_orderby := cls.get_wc_orderby(field):
				params["orderby"] = wc_orderby
				params["order"] = "desc" if descending else "asc"

		# Map Frappe filters to WooCommerce parameters
		if args.get("filters"):
			# An unsupported filter must fail: dropping it would return unrelated records
			params.update(map_frappe_filters_to_wc_params(args["filters"]))

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
					if strict:
						raise ConnectionError(
							f"WooCommerce API returned {response.status_code} for {endpoint}"
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
				if strict:
					raise
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
						record = cls.pre_init_document(
							record=record, woocommerce_server_url=wc_server.woocommerce_server_url
						)
						record = cls.during_get_list_of_records(record, args)
						processed_batch.append(record)
					except Exception as e:
						frappe.log_error(
							f"Error processing record {record.get('id', 'unknown')}: {e!s}",
							"WooCommerce Record Error",
						)

				records_needed -= len(results)
				server_offset += len(results)
				all_results.extend(processed_batch)
				total_processed += min(total_records_in_server, len(processed_batch))

				if records_needed <= 0 or len(results) < params["per_page"]:
					break

				try:
					batch_params = params.copy()
					batch_params["per_page"] = min(records_needed, params["per_page"])
					batch_params["offset"] = server_offset
					response = wc_server.get(endpoint, params=batch_params)

					if response.status_code != 200:
						break

					# Parse the response
					results = response.json()
				except Exception:
					break

		# Every server was ordered on its own, so order the combined list again
		return sort_records(all_results, order_by)

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
		server_domain = parse_domain_from_url(woocommerce_server_url)
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

	# Records are made and removed in WooCommerce, never from here
	def db_insert(self, *args, **kwargs):
		pass

	def delete(self, *args, **kwargs):
		pass

	@staticmethod
	def get_stats(args):
		pass

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
			if isinstance(value := obj.get(field.fieldname), str) and value:
				obj[field.fieldname] = json.loads(value)
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
				response = wc_server.get(cls.resource, params={"per_page": 1, "_fields": "id"})

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
					f"Error getting count from {wc_server.woocommerce_server_url}: {err!s}",
					"WooCommerce Count Error",
				)
		return total_count

	@classmethod
	def get_api_response(cls, server: str, endpoint: str, **kwargs):
		# Initialise the WC API
		try:
			wc_api_list = cls._init_api()
		except SyncDisabledError:
			frappe.msgprint(
				_("WooCommerce synchronization is disabled. Please enable at least one WooCommerce server.")
			)
			return {}

		wcapi = next(api for api in wc_api_list if server in api.woocommerce_server_url)
		return wcapi.get(endpoint, **kwargs).json()


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


def log_and_raise_error(exception=None, error_text=None, response=None) -> NoReturn:
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

	# frappe.throw always raises, this only tells the type checker so
	raise AssertionError("unreachable")


def parse_domain_from_url(url: str):
	domain = urlparse(url).netloc
	if not domain:
		raise ValueError(_("Invalid server URL"))
	return domain


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
		_doctype, field, operator, value = filter

		# Handle standard mappings
		if field in standard_mappings and operator in standard_mappings[field]:
			params[standard_mappings[field][operator]] = value
			continue

		# Handle specific cases
		if field in ["name", "woocommerce_name"] and operator == "like":
			params["search"] = value.strip("%")
		elif field in ("id", "woocommerce_id") and operator == "=":
			params["include"] = [value]
		elif field in ("id", "woocommerce_id") and operator == "in":
			params["include"] = ",".join(str(v) for v in value)
		elif field == "status" and operator == "=":
			params["status"] = [value]
		elif field == "status" and operator == "in":
			params["status"] = ",".join(value)
		else:
			frappe.throw(f"Unsupported operator '{operator}' for field '{field}'")

	return params
