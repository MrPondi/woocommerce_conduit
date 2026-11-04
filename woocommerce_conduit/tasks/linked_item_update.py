"""
Update Linked Items, on the server form: the Item Field Map applied to the Items linked to a shop already, as
the sync only maps a product into its Item when it makes the Item or a person syncs it. Nothing changes on its
own: a background job finds what each Item would get from its product (review_linked_items), the form lists
it, and a person ticks the Items that take it (update_linked_items).

A template follows its variants: when all its enabled variants would have the same value in a field, the
template gets it too, whether it is linked to the shop or not, as some families are made in ERPNext with each
variant linked to its product by its own id. ERPNext copies the fields of Item Variant Settings from a
template to all its variants whenever the template is saved, so a template left behind would undo its
variants' values. Saving a template here doesn't copy anything to its variants: they change only when they
are ticked themselves.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cstr, get_datetime, now_datetime, strip_html
from frappe.utils.background_jobs import is_job_enqueued

from woocommerce_conduit.tasks.field_map import (
	LOGGED_PROBLEMS_FLAG,
	get_first_field_names,
	get_target_fieldname,
)
from woocommerce_conduit.tasks.product_push import load_products
from woocommerce_conduit.tasks.sync_items import (
	get_item_field_changes,
	get_modified_in_system_timezone,
	get_primary_connection,
	values_match,
)
from woocommerce_conduit.woocommerce.woocommerce_api import WC_PAGE_LIMIT

# How long a review is kept for a person to tick its Items
REVIEW_TTL = 6 * 60 * 60
# The longest a review reads the shop for: a variation takes a request of its own
REVIEW_TIMEOUT = 2 * 60 * 60
# The most Items one request updates. The form sends more one request after another.
MAX_ITEMS_PER_UPDATE = 50
# The realtime event with the progress of a review, sent to the person who started it
REVIEW_EVENT = "woocommerce_linked_item_review"
ERROR_TITLE = "WooCommerce Linked Item Review Error"
# The fields of an Item a review reads besides the mapped ones
ITEM_FIELDS = ("name", "item_name", "has_variants", "variant_of", "disabled", "modified")

# Why an Item that was ticked is left alone, besides "updated" and "failed"
CHANGED_SINCE = "changed"
RELINKED = "relinked"
VARIANTS_DIFFER = "variants"
NOT_IN_REVIEW = "missing"
# The results after which an Item is done with, so it leaves the review
DONE = ("updated", CHANGED_SINCE, RELINKED)


def get_review_key(woocommerce_server: str) -> str:
	# Per person, as each reviews and ticks their own. The job's id too.
	return f"woocommerce_linked_item_review::{woocommerce_server}::{frappe.session.user}"


def get_entries_key(woocommerce_server: str) -> str:
	return f"{get_review_key(woocommerce_server)}::items"


def get_review_job_id(woocommerce_server: str) -> str:
	return get_review_key(woocommerce_server)


@frappe.whitelist(methods=["POST"])
def start_linked_item_review(woocommerce_server: str, rows: list[str] | str | None = None) -> bool:
	"""
	From the server form: find in the background what the rows of the Item Field Map would change in the
	Items linked to the shop, all rows unless `rows` names some. Returns whether the review started, which it
	doesn't while the person's last review of the shop still runs.
	"""
	server = frappe.get_doc("WooCommerce Server", woocommerce_server)
	server.check_permission("read")
	# The review is for updating Items, which the person must be allowed to do
	frappe.has_permission("Item", "write", throw=True)
	if not server.enabled:
		frappe.throw(_("Enable WooCommerce Server {0} first").format(frappe.bold(server.name)))
	if isinstance(rows, str):
		rows = frappe.parse_json(rows)
	row_names = [row.name for row in server.item_field_map if not rows or row.name in rows]
	if not row_names:
		frappe.throw(_("Pick a row of the Item Field Map to look for changes in"))
	return bool(
		frappe.enqueue(
			review_linked_items,
			queue="long",
			timeout=REVIEW_TIMEOUT,
			job_id=get_review_job_id(server.name),
			deduplicate=True,
			woocommerce_server=server.name,
			rows=row_names,
		)
	)


@frappe.whitelist()
def get_linked_item_review(woocommerce_server: str) -> dict | None:
	"""
	The person's last review of the shop's linked Items while it is kept, or {"running": True} while it is
	being made
	"""
	frappe.has_permission("WooCommerce Server", "read", doc=woocommerce_server, throw=True)
	frappe.has_permission("Item", "write", throw=True)
	if is_job_enqueued(get_review_job_id(woocommerce_server)):
		return {"running": True}
	return get_review(woocommerce_server)


def keep_review(woocommerce_server: str, review: dict):
	"""
	Keep a review for the person who made it, REVIEW_TTL long: what it says of the shop, with the order of its
	Items, under one key, and each Item under its code in a hash, so an update reads and drops only the Items
	it saves rather than the whole review, which can run to megabytes of descriptions
	"""
	entries_key = get_entries_key(woocommerce_server)
	frappe.cache.delete_value(entries_key)
	for entry in review["items"]:
		frappe.cache.hset(entries_key, entry["item_code"], entry)
	frappe.cache.expire_key(entries_key, REVIEW_TTL)
	summary = {key: value for key, value in review.items() if key != "items"}
	summary["order"] = [entry["item_code"] for entry in review["items"]]
	frappe.cache.set_value(get_review_key(woocommerce_server), summary, expires_in_sec=REVIEW_TTL)


def get_review(woocommerce_server: str) -> dict | None:
	"""
	The person's review of the shop while it is kept, with the Items it still holds
	"""
	if not (summary := get_review_summary(woocommerce_server)):
		return None
	# Redis gives the codes as bytes
	entries = {
		cstr(code): entry for code, entry in frappe.cache.hgetall(get_entries_key(woocommerce_server)).items()
	}
	review = {key: value for key, value in summary.items() if key != "order"}
	review["items"] = [entries[code] for code in summary["order"] if code in entries]
	return review


def get_review_summary(woocommerce_server: str) -> dict | None:
	return frappe.cache.get_value(get_review_key(woocommerce_server), expires=True)


def get_review_entries(woocommerce_server: str, item_codes: list[str]) -> dict[str, dict]:
	"""
	These Items of the person's review of the shop, by code, those it still holds
	"""
	entries_key = get_entries_key(woocommerce_server)
	return {code: entry for code in item_codes if (entry := frappe.cache.hget(entries_key, code))}


def drop_from_review(woocommerce_server: str, item_codes: list[str]):
	if item_codes:
		frappe.cache.hdel(get_entries_key(woocommerce_server), item_codes)


def review_linked_items(woocommerce_server: str, rows: list[str]):
	"""
	Background job: find what these rows of the Item Field Map would change in each Item whose primary
	connection is to the shop, and in the templates of those that are variants, and keep it for the person
	who started it (get_linked_item_review). Nothing is saved.
	"""
	frappe.flags.pop(LOGGED_PROBLEMS_FLAG, None)
	try:
		review = make_review(woocommerce_server, rows)
	except Exception:
		frappe.log_error(ERROR_TITLE, f"{frappe.get_traceback()}\n\nWooCommerce Server: {woocommerce_server}")
		publish_review_progress(woocommerce_server, 0, 0, finished=True, error=True)
		frappe.msgprint(
			_("The linked Items of {0} couldn't be reviewed, see the Error Log").format(woocommerce_server),
			title=_("Update Linked Items"),
			indicator="red",
			realtime=True,
		)
		return
	keep_review(woocommerce_server, review)
	publish_review_progress(woocommerce_server, review["checked"], review["checked"], finished=True)


def publish_review_progress(woocommerce_server: str, done: int, total: int, **result):
	"""
	Tell the person who started the review how far it is
	"""
	frappe.publish_realtime(
		REVIEW_EVENT,
		{"woocommerce_server": woocommerce_server, "done": done, "total": total, **result},
		user=frappe.session.user,
	)


def make_review(woocommerce_server: str, row_names: list[str]) -> dict:
	"""
	What the rows would change in the Items linked to the shop, as a review: its Items in the order the form
	lists them, each template before its variants, and how many Items were checked, how many take their
	fields from another shop, how many products couldn't be read, and how many values were left out (logged)
	"""
	server = frappe.get_cached_doc("WooCommerce Server", woocommerce_server)
	rows = [row for row in server.item_field_map if row.name in row_names]
	meta = frappe.get_meta("Item")
	targets = list(
		dict.fromkeys(get_target_fieldname(row) for row in rows if meta.has_field(get_target_fieldname(row)))
	)
	links, elsewhere = get_primary_links(woocommerce_server)
	items = get_item_values(list(links), targets)
	fields = get_fields_to_read(rows)

	entries, unread = {}, 0
	item_codes = [item_code for item_code in links if item_code in items]
	for start in range(0, len(item_codes), WC_PAGE_LIMIT):
		chunk = item_codes[start : start + WC_PAGE_LIMIT]
		products = load_products(
			woocommerce_server,
			[links[item_code] for item_code in chunk],
			ERROR_TITLE,
			fields,
			variations_alone=True,
		)
		for item_code in chunk:
			if not (product := products.get(links[item_code])):
				unread += 1
				continue
			item = items[item_code]
			if changes := get_item_field_changes(item, product, rows, meta):
				entries[item_code] = make_entry(item, changes, meta)
				entries[item_code]["woocommerce_id"] = links[item_code]
				entries[item_code]["edited_since"] = get_datetime(
					item.modified
				) > get_modified_in_system_timezone(product)
		publish_review_progress(woocommerce_server, start + len(chunk), len(item_codes))

	add_template_changes(entries, items, targets, meta)
	return {
		"woocommerce_server": woocommerce_server,
		# Which review the form shows, so it never updates Items with another one's values
		"review_id": frappe.generate_hash(length=12),
		"made": now_datetime(),
		"rows": [
			f"{meta.get_label(get_target_fieldname(row))} ← {row.woocommerce_field_name}" for row in rows
		],
		"checked": len(item_codes),
		"elsewhere": elsewhere,
		"unread": unread,
		"problems": len(frappe.flags.get(LOGGED_PROBLEMS_FLAG) or ()),
		"items": sorted(entries.values(), key=get_list_order),
	}


def get_primary_links(woocommerce_server: str) -> tuple[dict[str, str], int]:
	"""
	The Items whose primary connection (get_primary_connection) is to a product of this shop, with the
	product's id, and how many Items are linked to the shop as another shop's, which take their fields from
	that one
	"""
	linked = set(
		frappe.get_all(
			"Item WooCommerce Server",
			filters={"parenttype": "Item", "woocommerce_server": woocommerce_server},
			pluck="parent",
		)
	)
	if not linked:
		return {}, 0

	connections = defaultdict(list)
	for row in frappe.get_all(
		"Item WooCommerce Server",
		filters={"parenttype": "Item", "parent": ["in", list(linked)]},
		fields=["parent", "woocommerce_server", "woocommerce_id", "is_primary", "idx"],
		order_by="idx asc",
	):
		connections[row.parent].append(row)

	links, elsewhere = {}, 0
	for item_code in sorted(linked):
		primary = get_primary_connection(frappe._dict(woocommerce_servers=connections[item_code]))
		if primary and primary.woocommerce_server == woocommerce_server and primary.woocommerce_id:
			links[item_code] = cstr(primary.woocommerce_id)
		else:
			elsewhere += 1
	return links, elsewhere


def get_item_values(item_codes: list[str], targets: list[str]) -> dict[str, frappe._dict]:
	"""
	The fields of these Items a review needs, the mapped ones among them, by Item code
	"""
	if not item_codes:
		return {}
	fields = list(dict.fromkeys([*ITEM_FIELDS, *targets]))
	return {
		item.name: item
		for item in frappe.get_all("Item", filters={"name": ["in", item_codes]}, fields=fields)
	}


def get_fields_to_read(rows) -> list[str]:
	"""
	The fields of the products the rows' paths start at, or all of them when a row has a transform, which gets
	the whole record
	"""
	if any(row.get("transform") for row in rows):
		return ["*"]
	fields = {
		field for row in rows for field in (get_first_field_names(cstr(row.woocommerce_field_name)) or ())
	}
	return sorted(fields) or ["*"]


def make_entry(item, changes: dict, meta, follows=()) -> dict:
	"""
	An Item of a review: the values it would get, each with the one it has
	"""
	return {
		"item_code": item.name,
		"item_name": item.item_name,
		"template": bool(item.has_variants),
		"variant_of": item.variant_of,
		"disabled": bool(item.disabled),
		"changes": [
			{
				"fieldname": fieldname,
				"label": meta.get_label(fieldname),
				"old": item.get(fieldname),
				"new": value,
				"follows": fieldname in follows,
			}
			for fieldname, value in changes.items()
		],
	}


def get_list_order(entry: dict) -> tuple:
	# A family together, its template first
	return (entry["variant_of"] or entry["item_code"], bool(entry["variant_of"]), entry["item_code"])


def is_empty(df, value) -> bool:
	"""
	Whether a value is none at all: nothing, or a number of nought, which ERPNext gives a number never set. A
	Check's nought says No.
	"""
	if value in (None, ""):
		return True
	return bool(df and df.fieldtype in ("Float", "Currency", "Int", "Percent") and not value)


def add_template_changes(entries: dict, items: dict, targets: list[str], meta):
	"""
	Have each template of the reviewed variants follow them: for each field all its enabled variants would
	have the same value in once the review's values are taken, the template gets that value rather than its
	own product's, unless it has it already. An empty value proposes nothing, so a template isn't cleared
	because its variants were never filled in. The template's entry names the variants whose values it
	waits for, which the form ticks it with.
	"""
	templates = {item.variant_of for item in items.values() if item.variant_of}
	if not templates or not targets:
		return
	variants = defaultdict(list)
	for variant in frappe.get_all(
		"Item",
		filters={"variant_of": ["in", list(templates)], "disabled": 0},
		fields=["name", "variant_of", *targets],
	):
		variants[variant.variant_of].append(variant)
	template_values = get_item_values(list(templates), targets)

	for template_code, template_variants in variants.items():
		if not (template := template_values.get(template_code)):
			continue
		own = entries.get(template_code)
		own_changes = {change["fieldname"]: change["new"] for change in own["changes"]} if own else {}
		followed, waits_for = {}, set()
		for fieldname in targets:
			after = [get_value_after_review(entries, variant, fieldname) for variant in template_variants]
			shared = after[0]
			if is_empty(meta.get_field(fieldname), shared) or not all(
				values_match(value, shared) for value in after
			):
				continue
			# The variants agree, so what the template's own product has doesn't count
			own_changes.pop(fieldname, None)
			if not values_match(template.get(fieldname), shared):
				followed[fieldname] = shared
				waits_for.update(
					variant.name
					for variant in template_variants
					if has_change(entries, variant.name, fieldname)
				)

		if not (followed or own):
			continue
		if not (own_changes or followed):
			# Its own product's values were all taken over by its variants', which it has already
			entries.pop(template_code)
			continue
		entry = make_entry(template, {**own_changes, **followed}, meta, follows=followed)
		if own and own_changes:
			# Only its own product's values need it to be linked to the product still
			entry["woocommerce_id"] = own["woocommerce_id"]
			entry["edited_since"] = own["edited_since"]
		entry["follows"] = sorted(waits_for)
		entry["variants"] = len(template_variants)
		entries[template_code] = entry


def get_value_after_review(entries: dict, variant, fieldname: str):
	for change in (entries.get(variant.name) or {}).get("changes", ()):
		if change["fieldname"] == fieldname:
			return change["new"]
	return variant.get(fieldname)


def has_change(entries: dict, item_code: str, fieldname: str) -> bool:
	return any(
		change["fieldname"] == fieldname for change in (entries.get(item_code) or {}).get("changes", ())
	)


@frappe.whitelist(methods=["POST"])
def update_linked_items(
	woocommerce_server: str, item_codes: list[str] | str, review_id: str | None = None
) -> dict[str, dict]:
	"""
	Give these Items, the ones a person ticked, the values of their review, at most MAX_ITEMS_PER_UPDATE of
	them. Templates go last, so a template's check sees its variants saved; the form sends them after the other
	Items too. An Item whose fields changed since the review, or that is no longer linked to the product it was
	reviewed with, is left alone, and so is a template whose enabled variants don't all have the values it
	follows. `review_id` is the review the person looked at, which a newer one mustn't stand in for. Returns
	what happened to each Item, by its code.
	"""
	frappe.has_permission("WooCommerce Server", "read", doc=woocommerce_server, throw=True)
	frappe.has_permission("Item", "write", throw=True)
	if isinstance(item_codes, str):
		item_codes = frappe.parse_json(item_codes)
	if len(item_codes) > MAX_ITEMS_PER_UPDATE:
		frappe.throw(_("Update at most {0} Items at a time").format(MAX_ITEMS_PER_UPDATE))
	if not (summary := get_review_summary(woocommerce_server)):
		frappe.throw(_("The review of the linked Items has expired. Find the changes again."))
	if review_id and summary.get("review_id") != review_id:
		frappe.throw(_("The linked Items have been reviewed again since. Look at the new review first."))

	entries = get_review_entries(woocommerce_server, item_codes)
	results = {}
	for item_code in sorted(item_codes, key=lambda code: bool((entries.get(code) or {}).get("template"))):
		if not (entry := entries.get(item_code)):
			results[item_code] = {"status": NOT_IN_REVIEW}
			continue
		results[item_code] = update_item(woocommerce_server, entry)

	# What was saved, or changed since, is done with. A failure can be tried again.
	drop_from_review(
		woocommerce_server, [code for code, result in results.items() if result["status"] in DONE]
	)
	return results


def update_item(woocommerce_server: str, entry: dict) -> dict:
	"""
	Give one Item the values of its review, unless it is to be left alone (get_reason_to_leave). A failure
	is rolled back and logged, so the other Items of the request are kept.
	"""
	savepoint = "woocommerce_linked_item"
	frappe.db.savepoint(savepoint)
	try:
		item = frappe.get_doc("Item", entry["item_code"])
		if reason := get_reason_to_leave(item, woocommerce_server, entry):
			return {"status": reason}
		for change in entry["changes"]:
			item.set(change["fieldname"], change["new"])
		item.flags.created_by_sync = True
		# Its variants take the values only when they are ticked themselves
		item.flags.dont_update_variants = True
		item.save()
	except Exception as error:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(ERROR_TITLE, f"{frappe.get_traceback()}\n\nItem: {entry['item_code']}")
		return {"status": "failed", "error": strip_html(cstr(error)) or type(error).__name__}
	finally:
		# The form shows each Item's result in its row, rather than a message of each save or failure
		frappe.clear_messages()
	return {"status": "updated"}


def get_reason_to_leave(item, woocommerce_server: str, entry: dict) -> str | None:
	"""
	Why a ticked Item is left alone: a field has changed since the review, its primary connection is no longer
	the product it was reviewed with, or, for a template, its enabled variants don't all have the values it
	follows (one wasn't ticked, or failed)
	"""
	if woocommerce_id := entry.get("woocommerce_id"):
		primary = get_primary_connection(item)
		if not (
			primary
			and primary.woocommerce_server == woocommerce_server
			and cstr(primary.woocommerce_id) == woocommerce_id
		):
			return RELINKED
	if any(not values_match(item.get(change["fieldname"]), change["old"]) for change in entry["changes"]):
		return CHANGED_SINCE
	if followed := [change for change in entry["changes"] if change.get("follows")]:
		variants = frappe.get_all(
			"Item",
			filters={"variant_of": item.name, "disabled": 0},
			fields=["name", *{change["fieldname"] for change in followed}],
		)
		if any(
			not values_match(variant.get(change["fieldname"]), change["new"])
			for variant in variants
			for change in followed
		):
			return VARIANTS_DIFFER
	return None
