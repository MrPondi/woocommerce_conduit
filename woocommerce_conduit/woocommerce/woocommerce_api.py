import json
import re
import traceback
from typing import NoReturn, Self
from urllib.parse import urlparse

import frappe
import frappe.utils
from frappe import _
from frappe.deferred_insert import deferred_insert
from frappe.model import no_value_fields, numeric_fieldtypes
from frappe.model.document import Document
from frappe.types.filter import Filters
from frappe.utils import add_days, cint, cstr, get_datetime, getdate
from frappe.utils.caching import request_cache
from frappe.utils.password import get_decrypted_password
from woocommerce import API

from woocommerce_conduit.exceptions import SyncDisabledError

WC_RESOURCE_DELIMITER = "~"
# The most of a response that an Integration Request keeps
LOGGED_RESPONSE_LENGTH = 64 * 1024
# The most records WooCommerce returns for one request
WC_PAGE_LIMIT = 100


class WooCommerceAPI(API):
	"""
	The REST API client of one WooCommerce Server. Its failed requests, and all of them when the server has
	Log All Requests ticked, are kept as Integration Requests.
	"""

	def __init__(
		self,
		url: str,
		consumer_key: str,
		consumer_secret: str,
		woocommerce_server: str | None = None,
		log_all_requests: bool = False,
		**kwargs,
	):
		kwargs.setdefault("version", "wc/v3")
		kwargs.setdefault("timeout", 40)
		super().__init__(url=url, consumer_key=consumer_key, consumer_secret=consumer_secret, **kwargs)
		self.woocommerce_server = woocommerce_server
		self.woocommerce_server_url = url
		self.log_all_requests = bool(log_all_requests)

	def _API__request(self, method, endpoint, data, params=None, **kwargs):
		try:
			response = super()._API__request(method, endpoint, data, params, **kwargs)  # type: ignore
		except Exception:
			self.log_request(method, endpoint, params, data, error=traceback.format_exc())
			raise
		if self.log_all_requests or not response.ok:
			self.log_request(method, endpoint, params, data, response=response)
		return response

	def log_request(self, method: str, endpoint: str, params, data, response=None, error: str | None = None):
		"""
		Keep a request as an Integration Request, which Log Settings clears after a while. The insert is
		deferred, so it survives a sync that is rolled back and costs no background job. Frappe writes the
		deferred inserts every 15 minutes.
		"""
		if frappe.in_test:
			return
		failed = bool(error) or not response.ok
		if failed and not error:
			error = f"{response.status_code} {response.reason or ''}".strip()
		elapsed = f" ({response.elapsed.total_seconds():.2f} s)" if response is not None else ""
		deferred_insert(
			"Integration Request",
			[
				{
					"integration_request_service": "WooCommerce",
					"request_description": f"{method} {endpoint}{elapsed}"[:140],
					# Not the URL of the request, which carries the OAuth signature on a shop without https
					"url": f"{self.url.rstrip('/')}/wp-json/{self.version}/{endpoint}",
					"data": frappe.as_json({"params": params, "data": data}),
					"output": response.text[:LOGGED_RESPONSE_LENGTH] if response is not None else None,
					"error": error,
					"status": "Failed" if failed else "Completed",
					"reference_doctype": "WooCommerce Server" if self.woocommerce_server else None,
					"reference_docname": self.woocommerce_server,
				}
			],
		)


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


def value_changed(df, value, old_value) -> bool:
	"""
	Whether a field's value changed. Numbers are compared by value and empty values are alike, so "10" and
	10.0, or None and "", are no change.
	"""
	if df.fieldtype in numeric_fieldtypes:
		return frappe.utils.flt(value) != frappe.utils.flt(old_value)
	return not (value == old_value or (value or None) == (old_value or None))


@request_cache
def get_api_clients() -> list[WooCommerceAPI]:
	"""
	A client for each enabled WooCommerce Server. Built once per request or background job, as each one has
	the server's passwords decrypted.

	Raises:
		SyncDisabledError: If no enabled WooCommerce servers are found
	"""
	wc_servers = frappe.get_all(
		"WooCommerce Server",
		fields=["name", "woocommerce_server_url", "log_all_requests"],
		filters={"enabled": 1},
	)

	wc_api_list = []
	for server in wc_servers:
		try:
			wc_api_list.append(
				WooCommerceAPI(
					url=server.woocommerce_server_url,
					consumer_key=get_decrypted_password(
						"WooCommerce Server", server.name, "api_consumer_key", raise_exception=False
					),
					consumer_secret=get_decrypted_password(
						"WooCommerce Server", server.name, "api_consumer_secret", raise_exception=False
					),
					woocommerce_server=server.name,
					log_all_requests=server.log_all_requests,
				)
			)
		except Exception as e:
			frappe.log_error(
				"WooCommerce Error", f"Error initializing WooCommerce API for server {server.name}: {e!s}"
			)

	if not wc_api_list:
		frappe.throw(_("At least one WooCommerce Server should be Enabled"), SyncDisabledError)

	return wc_api_list


def clear_api_clients():
	"""
	Forget the clients built in this request, as a WooCommerce Server was changed
	"""
	if cache := getattr(frappe.local, "request_cache", None):
		cache.pop(get_api_clients.__wrapped__, None)


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
		The WooCommerce API clients of all enabled servers, see get_api_clients
		"""
		return get_api_clients()

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
			frappe.DoesNotExistError: If WooCommerce doesn't have the record
			frappe.ValidationError: If WooCommerce can't be reached or answers with an error, which is logged
		"""
		# Parse the server domain and record_id from the Document name
		wc_server_domain, record_id = get_domain_and_id_from_woocommerce_record_name(str(self.name))

		# Request the fields of the DocType
		params = {"_fields": self.get_wc_fields()}

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
		except Exception as err:
			log_and_raise_error(
				exception=err, error_text=f"load_from_db failed (WooCommerce {self.resource} #{record_id})"
			)
		# Not an error of the sync: the record was deleted, or never existed. The request is logged already.
		if response.status_code == 404:
			frappe.throw(_("{0} {1} not found").format(_(self.doctype), self.name), frappe.DoesNotExistError)
		if response.status_code != 200:
			log_and_raise_error(error_text=f"API returned {response.status_code}", response=response)
		try:
			record = response.json()
		except ValueError as err:
			log_and_raise_error(
				exception=err,
				error_text=f"Invalid JSON response for WooCommerce {self.resource} #{record_id}",
			)

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
		if changes := self.get_changed_values():
			self.write(changes)

	def write(self, changes: dict) -> dict:
		"""
		Write changed Frappe fields to WooCommerce, shaped by the subclass's before_db_update, without the
		reload a save does. For the pushes, which compare with the record they just loaded (get_changes):
		WooCommerce dates every save as a change, even one that changes nothing.

		Returns:
			The request body that was sent, empty when before_db_update left nothing to write
		"""
		changes = self.before_db_update(changes)
		if not changes:
			return {}

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
		return changes

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

		return {
			df.fieldname: values.get(df.fieldname)
			for df in self.meta.fields
			if df.fieldtype not in no_value_fields
			and df.fieldname in values
			and value_changed(df, values.get(df.fieldname), before_values.get(df.fieldname))
		}

	def get_changes(self, values: dict) -> dict:
		"""
		The values that differ from what the record has now, compared as get_changed_values compares them
		"""
		return {
			fieldname: value
			for fieldname, value in values.items()
			if not (df := self.meta.get_field(fieldname)) or value_changed(df, value, self.get(fieldname))
		}

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
	def get_servers_to_read(cls, args) -> set[str] | None:
		"""
		The servers that `servers` and the filters of a list or count leave, or None for all of them
		"""
		servers = set(args.get("servers") or []) or None
		if (from_filters := get_servers_from_filters(args.get("filters"), cls.doctype)) is not None:
			servers = from_filters if servers is None else servers & from_filters
		return servers

	@classmethod
	def get_wc_orderby(cls, fieldname: str) -> str | None:
		"""
		The WooCommerce `orderby` for a Frappe fieldname, or None if WooCommerce can't order by it
		"""
		return {**COMMON_ORDERBY_MAP, **cls.orderby_map}.get(fieldname)

	@classmethod
	def get_list_of_records(cls, args):
		"""
		WooCommerce records for the list and report views, and for the syncs.

		The servers are read one after the other, as if their records were one list: a server whose records
		all lie before the requested offset (by its `x-wp-total`) only moves the offset on, and no server is
		asked once the page is full. Each server orders its records, and the page is ordered again as a whole.

		Besides Frappe's arguments, args can have:
			page_length: how many records to return, fetched `per_page` (at most 100) a request
			servers: only read these WooCommerce Servers
			endpoint: another endpoint than the resource, for example the variations of a product
			extra_wc_fields: WooCommerce fields to request on top of those of the requested Frappe fields
			strict: raise when a server fails, instead of logging it and going on without it
		"""
		if not isinstance(args, dict):
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

		# Only request the fields that Frappe asked for
		params = {"_fields": cls.get_wc_fields(args.get("fields"), args.get("extra_wc_fields"))}

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
			params.update(map_frappe_filters_to_wc_params(args["filters"], cls.doctype))

		servers = cls.get_servers_to_read(args)
		page_length = cint(args.get("page_length")) or WC_PAGE_LIMIT
		per_page = min(cint(args.get("per_page")) or page_length, WC_PAGE_LIMIT)
		endpoint = args.get("endpoint") or cls.resource

		records = []
		# How many records of the servers still to be read lie before the page
		offset = cint(args.get("start"))
		for wc_api in wc_api_list:
			if len(records) >= page_length:
				break
			if servers is not None and wc_api.woocommerce_server not in servers:
				continue
			server_records, total = cls._get_records_of_server(
				wc_api, endpoint, params, offset, per_page, page_length - len(records), strict, args
			)
			records.extend(server_records)
			# The next server's records come after all of this one's
			if total is not None:
				offset = max(0, offset - total)
			elif server_records:
				offset = 0

		# Every server was ordered on its own, so order the page again
		return sort_records(records, order_by)

	@classmethod
	def _get_records_of_server(
		cls, wc_api, endpoint: str, params: dict, offset: int, per_page: int, wanted: int, strict: bool, args
	) -> tuple[list[dict], int | None]:
		"""
		Up to `wanted` records of one server from `offset` on, and how many records the server has in all
		(None when it didn't say)
		"""
		records, total, received = [], None, 0
		while received < wanted:
			size = min(per_page, wanted - received)
			page_params = {**params, "per_page": size}
			if offset + received:
				page_params["offset"] = offset + received
			response = cls._get_page(wc_api, endpoint, page_params, strict)
			if response is None:
				break
			if total is None and "x-wp-total" in response.headers:
				total = cint(response.headers["x-wp-total"])

			page = response.json() or []
			received += len(page)
			for record in page:
				try:
					record = cls.pre_init_document(
						record=record, woocommerce_server_url=wc_api.woocommerce_server_url
					)
					records.append(cls.during_get_list_of_records(record, args))
				except Exception as e:
					frappe.log_error(
						"WooCommerce Record Error",
						f"Error processing record {record.get('id', 'unknown')}: {e!s}",
					)
			if len(page) < size:
				break
		return records, total

	@staticmethod
	def _get_page(wc_api, endpoint: str, params: dict, strict: bool):
		"""
		One page of a list endpoint, or None when the server failed (which raises instead with `strict`)
		"""
		try:
			response = wc_api.get(endpoint, params=params)
		except Exception as err:
			frappe.log_error(
				"WooCommerce API Error",
				f"Error fetching WooCommerce records: {err!s}\nEndpoint: {endpoint}\nParams: {params}",
			)
			if strict:
				raise
			return None
		if response.status_code != 200:
			frappe.log_error(
				"WooCommerce API Error", f"WooCommerce API error: {response.status_code} - {response.text}"
			)
			if strict:
				raise ConnectionError(f"WooCommerce API returned {response.status_code} for {endpoint}")
			return None
		return response

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
		The JSON fields of the doctype, from its cached meta: this runs for every record of a list
		"""
		return frappe.get_meta(cls.doctype).get("fields", {"fieldtype": "JSON"})

	@classmethod
	def get_count_of_records(cls, args) -> int:
		"""
		Returns count of WooCommerce Records across the enabled servers, with the filters of the list

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

		args = args if isinstance(args, dict) else {}
		params = {"per_page": 1, "_fields": "id"}
		if args.get("filters"):
			params.update(map_frappe_filters_to_wc_params(args["filters"], cls.doctype))
		servers = cls.get_servers_to_read(args)

		total_count = 0

		# Get counts from each server
		for wc_server in wc_api_list:
			if servers is not None and wc_server.woocommerce_server not in servers:
				continue
			try:
				response = wc_server.get(cls.resource, params=params)

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


# Frappe fieldname -> the WooCommerce parameters for after and before a date
DATE_FILTER_PARAMS = {
	"date_created": ("after", "before"),
	"woocommerce_date_created": ("after", "before"),
	"creation": ("after", "before"),
	"date_modified": ("modified_after", "modified_before"),
	"woocommerce_date_modified": ("modified_after", "modified_before"),
	"modified": ("modified_after", "modified_before"),
}


def normalise_filters(filters, doctype: str | None = None) -> list:
	"""
	The filters as Frappe's FilterTuples (doctype, fieldname, operator, value), whichever form they come in:
	a dict, lists of three or four, or Filters
	"""
	if not filters:
		return []
	if isinstance(filters, str):
		filters = json.loads(filters)
	if isinstance(filters, Filters):
		return list(filters)
	return list(Filters(filters, doctype=doctype) if doctype else Filters(filters))


def as_values(value) -> list:
	return list(value) if isinstance(value, list | tuple | set) else [value]


def to_wc_datetime(value) -> str:
	"""
	A date as WooCommerce takes it in a filter: ISO 8601, without microseconds
	"""
	return get_datetime(value).strftime("%Y-%m-%dT%H:%M:%S")


def get_record_id(name) -> str:
	"""
	The WooCommerce id in a record name (`<server>~<id>`), or the value itself when it is an id already
	"""
	name = cstr(name)
	return (
		str(get_domain_and_id_from_woocommerce_record_name(name)[1])
		if WC_RESOURCE_DELIMITER in name
		else name
	)


def get_servers_from_filters(filters, doctype: str | None = None) -> set[str] | None:
	"""
	The servers that the filters on `woocommerce_server`, or on record names (`<server>~<id>`), leave, or None
	when they leave every server. WooCommerce doesn't know these filters: they pick the servers to ask.
	"""
	servers = None
	for f in normalise_filters(filters, doctype):
		operator = f.operator.lower()
		if operator not in ("=", "in"):
			continue
		if f.fieldname == "woocommerce_server":
			values = {cstr(value) for value in as_values(f.value)}
		elif f.fieldname == "name":
			values = {
				get_domain_and_id_from_woocommerce_record_name(cstr(value))[0]
				for value in as_values(f.value)
				if WC_RESOURCE_DELIMITER in cstr(value)
			}
			if not values:
				continue
		else:
			continue
		servers = values if servers is None else servers & values
	return servers


def map_frappe_filters_to_wc_params(filters, doctype: str | None = None):
	"""
	Maps Frappe filters to WooCommerce API parameters. An unsupported filter is an error: dropping it would
	return unrelated records. A filter on the server picks the servers instead, see get_servers_from_filters.
	"""
	params = {}
	for f in normalise_filters(filters, doctype):
		field, operator, value = f.fieldname, f.operator.lower(), f.value

		if field == "woocommerce_server" and operator in ("=", "in"):
			continue
		if field in DATE_FILTER_PARAMS and operator in (">", "<"):
			after, before = DATE_FILTER_PARAMS[field]
			params[after if operator == ">" else before] = to_wc_datetime(value)
		elif field in DATE_FILTER_PARAMS and operator == "between":
			# Whole days, the last one included, as Frappe's date range filter means them
			if value:
				after, before = DATE_FILTER_PARAMS[field]
				start, end = value
				params[after] = to_wc_datetime(getdate(start))
				params[before] = to_wc_datetime(add_days(getdate(end), 1))
		elif field in ("name", "woocommerce_name") and operator == "like":
			params["search"] = cstr(value).strip("%")
		elif field in ("name", "id", "woocommerce_id") and operator == "=":
			params["include"] = [get_record_id(value) if field == "name" else value]
		elif field in ("name", "id", "woocommerce_id") and operator == "in":
			params["include"] = ",".join(
				get_record_id(item) if field == "name" else str(item) for item in as_values(value)
			)
		elif field == "status" and operator == "=":
			params["status"] = [value]
		elif field == "status" and operator == "in":
			params["status"] = ",".join(as_values(value))
		else:
			frappe.throw(f"Unsupported operator '{f.operator}' for field '{field}'")

	return params
