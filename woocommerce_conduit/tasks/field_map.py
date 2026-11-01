"""
The field maps of a WooCommerce Server. A row copies a value of a WooCommerce record, found with a JSONPath,
into a field of an ERPNext document: the Item Field Map from a product into its Item, the Order Line Fields
map from an order line into its Sales Order Item. A row can have the
value changed first by a transform, a method another app registers under the `woocommerce_field_transforms`
hook (see hooks.py).
"""

import json
from collections.abc import Container
from functools import lru_cache

import frappe
from frappe import _
from frappe.model import no_value_fields, numeric_fieldtypes
from frappe.utils import cint, cstr, escape_html
from frappe.utils.html_utils import sanitize_html
from jsonpath_ng.ext import parse
from jsonpath_ng.jsonpath import Child, Fields, Root

from woocommerce_conduit.tasks.sync import unescape_text

# Fields whose value is HTML
HTML_FIELDTYPES = ("Text Editor", "HTML Editor")

# Item fields the Item Field Map may not write. `item_code` and the variant fields are the sync's own,
# `standard_rate` makes an Item Price the price push sends straight back, `opening_stock` a Stock Entry, and
# `stock_uom` or `is_stock_item` can't change once the Item has transactions.
REFUSED_ITEM_FIELDS = (
	"item_code",
	"variant_of",
	"has_variants",
	"stock_uom",
	"is_stock_item",
	"opening_stock",
	"standard_rate",
)

# The standard Sales Order Item fields the Order Line Fields map may write, besides the custom ones. The others
# change what the line costs, how it is taxed or how it is delivered, which the sync and ERPNext work out.
ORDER_ITEM_MAP_FIELDS = ("item_name", "description", "additional_notes")
# The custom fields of Sales Order Item that the sync sets itself
SYNC_ORDER_ITEM_FIELDS = ("woocommerce_id", "woocommerce_line_item_id")

# Kept in frappe.flags, so a request or job logs each problem of a map once rather than once per record
LOGGED_PROBLEMS_FLAG = "woocommerce_logged_map_problems"

# Other apps register their transforms under this hook, as {"Name": "dotted.path.to.method"}
TRANSFORMS_HOOK = "woocommerce_field_transforms"


@lru_cache(maxsize=256)
def parse_path(path: str):
	"""
	A parsed JSONPath. Parsing takes about a thousand times as long as finding, and a sync runs every row of a
	map for every record.
	"""
	return parse(path)


def get_transforms() -> dict[str, str]:
	"""
	The transforms the installed apps register, by name. Frappe lists a dict hook's values per app, so when two
	apps register a name, the one loaded last wins, as with Frappe's own overrides.
	"""
	return {
		name: paths[-1] if isinstance(paths, list | tuple) else paths
		for name, paths in frappe.get_hooks(TRANSFORMS_HOOK, default={}).items()
		if paths
	}


def get_target_fieldname(row) -> str:
	# The Select stores "fieldname | Label"
	return cstr(row.erpnext_field_name).split(" | ")[0]


def get_first_field_names(path: str) -> tuple[str, ...] | None:
	"""
	The field or fields a JSONPath starts with, `meta_data` for `$.meta_data[?key=='_gtin'].value`. None when
	it doesn't start with a named field, as `$..value` or `$.*` don't.
	"""
	node = parse_path(path)
	while isinstance(node, Child) and not isinstance(node.left, Root):
		node = node.left
	first = node.right if isinstance(node, Child) else node
	if isinstance(first, Fields) and "*" not in first.fields:
		return first.fields
	return None


def get_value_fields(doctype: str) -> set[str]:
	return {df.fieldname for df in frappe.get_meta(doctype).fields if df.fieldtype not in no_value_fields}


def get_item_map_fields() -> set[str]:
	"""
	The Item fields the Item Field Map may write
	"""
	return get_value_fields("Item") - set(REFUSED_ITEM_FIELDS)


def get_order_item_map_fields() -> set[str]:
	"""
	The Sales Order Item fields the Order Line Fields map may write: the custom ones but the sync's own, and
	the line's name, description and notes
	"""
	custom_fields = {
		df.fieldname
		for df in frappe.get_meta("Sales Order Item").get_custom_fields()
		if df.fieldtype not in no_value_fields
	}
	return (custom_fields - set(SYNC_ORDER_ITEM_FIELDS)) | set(ORDER_ITEM_MAP_FIELDS)


def get_product_fields() -> set[str]:
	"""
	The fields of a WooCommerce Product that a path of the Item Field Map can start at: those `to_dict` gives
	the path, which are the doctype's own and its name (`<server>~<id>`)
	"""
	return get_value_fields("WooCommerce Product") | {"name"}


def validate_map_row(
	row,
	map_label: str,
	target_doctype: str,
	target_fields: Container[str],
	source_doctype: str | None = None,
	source_fields: Container[str] | None = None,
	refused_sources: Container[str] = (),
) -> None:
	"""
	Throw when a row of a field map can't work: a target that isn't one of `target_fields`, a JSONPath that
	doesn't parse, or, with `source_fields`, one that doesn't start at one of them. A path that starts nowhere
	in particular (`$..name`, `$.*`) is refused too, as what it finds is anyone's guess.
	"""
	fieldname = get_target_fieldname(row)
	if fieldname not in target_fields:
		frappe.throw(
			_("{0} row {1}: {2} is not a field of {3} that the map can write").format(
				map_label, row.idx, frappe.bold(fieldname), _(target_doctype)
			)
		)

	if row.get("transform") and row.transform not in get_transforms():
		frappe.throw(
			_("{0} row {1}: no installed app registers the transform {2}").format(
				map_label, row.idx, frappe.bold(row.transform)
			)
		)

	path = cstr(row.woocommerce_field_name)
	try:
		parse_path(path)
	except Exception as e:
		frappe.throw(
			_("{0} row {1}: the JSONPath isn't valid:<br><br><pre>{2}</pre>").format(
				map_label, row.idx, escape_html(cstr(e))
			)
		)

	if source_fields is None:
		return
	first_fields = get_first_field_names(path)
	if not first_fields:
		frappe.throw(
			_("{0} row {1}: start the JSONPath at a field of {2}, as in {3}").format(
				map_label, row.idx, _(source_doctype), "<code>$.sku</code>"
			)
		)
	for field in first_fields:
		if field in refused_sources:
			frappe.throw(_("{0} row {1}: {2} can't be mapped").format(map_label, row.idx, frappe.bold(field)))
		if field not in source_fields:
			frappe.throw(
				_("{0} row {1}: {2} has no field {3}").format(
					map_label, row.idx, _(source_doctype), frappe.bold(field)
				)
			)


def get_mapped_values(rows, record: dict, meta, record_name: str) -> dict:
	"""
	The value each row's JSONPath finds in `record` (its first match), changed by the row's transform, for the
	field of `meta` the row names. A row whose path finds nothing, or whose transform returns None, is left
	out, and so is a value the field can't take or a transform that fails, which is logged under
	`record_name`: a mapped value must never fail the document it goes into.
	"""
	values = {}
	transforms = get_transforms() if any(row.get("transform") for row in rows or []) else {}
	for row in rows or []:
		fieldname = get_target_fieldname(row)
		try:
			matches = parse_path(cstr(row.woocommerce_field_name)).find(record)
		except Exception as e:
			log_problem(row, cstr(e), record_name)
			continue
		if not matches:
			continue

		df, value = meta.get_field(fieldname), matches[0].value
		# The transform gets the value as WooCommerce has it, and the record it was found in
		if row.get("transform"):
			if row.transform not in transforms:
				log_problem(
					row, _("no installed app registers the transform {0}").format(row.transform), record_name
				)
				continue
			try:
				value = frappe.get_attr(transforms[row.transform])(value, row=row, record=record)
			except Exception:
				log_problem(row, frappe.get_traceback(), record_name)
				continue
			if value is None:
				continue
		# WordPress keeps names HTML-escaped. In an HTML field the escaped text is right as it is, and
		# unescaped it could turn into markup.
		if df and df.fieldtype not in HTML_FIELDTYPES:
			value = unescape_text(value)
		value, problem = fit_value(df, value)
		if problem:
			log_problem(row, problem, record_name)
			continue
		values[fieldname] = value
	return values


def fit_value(df, value):
	"""
	The value as the field takes it, and None; or None and why the field can't take it. The checks are those
	that would otherwise fail the save, or that Frappe would get wrong without a word: it reads "12,5" as 125
	in a number field, and "yes" as 0 in a Check.
	"""
	if not df:
		return None, _("the field doesn't exist")
	if isinstance(value, list | dict):
		if df.fieldtype == "JSON":
			return json.dumps(value), None
		return None, _("a list or dict, which only a JSON field takes")
	if value is None or value == "":
		if df.reqd:
			return None, _("no value, and {0} is mandatory").format(df.fieldname)
		return value, None

	if df.fieldtype in numeric_fieldtypes:
		if isinstance(value, int | float):
			return value, None
		try:
			float(cstr(value))
		except ValueError:
			return None, _("{0} is not a number").format(frappe.bold(value))
		return value, None

	if df.fieldtype == "Link":
		# As the database has the name, so the next sync compares it with what the Item holds
		if name := frappe.db.exists(df.options, cstr(value)):
			return name, None
		return None, _("{0} {1} doesn't exist").format(_(df.options), frappe.bold(value))

	if df.fieldtype == "Select":
		value = cstr(value).strip()
		if df.options and value not in df.options.split("\n"):
			return None, _("{0} is not one of the options of {1}").format(frappe.bold(value), df.fieldname)
		return value, None

	if (frappe.db.type_map.get(df.fieldtype) or ("",))[0] == "varchar":
		max_length = cint(df.get("length")) or cint(frappe.db.type_map[df.fieldtype][1])
		if len(cstr(value)) > max_length:
			return None, _("longer than the {0} characters {1} takes").format(max_length, df.fieldname)
	return value, None


def log_problem(row, problem: str, record_name: str) -> None:
	"""
	Log why a row's value was left out, once per request or job for each row and problem, naming the record
	it was first seen on
	"""
	key = (cstr(row.woocommerce_field_name), get_target_fieldname(row), problem)
	logged = frappe.flags.setdefault(LOGGED_PROBLEMS_FLAG, set())
	if key in logged:
		return
	logged.add(key)
	frappe.log_error(
		"WooCommerce Field Mapping Error",
		f"{row.woocommerce_field_name} → {get_target_fieldname(row)}: {problem}\n\n"
		f"First seen on {record_name}; other records with the same problem aren't logged again in this run.",
	)


def sanitize_text(value):
	"""
	A mapped value as a save would store it, for one written without a save: text with markup in it, which can
	come from a customer's checkout, without its scripts and event handlers
	"""
	if isinstance(value, str) and ("<" in value or ">" in value):
		return sanitize_html(value)
	return value
