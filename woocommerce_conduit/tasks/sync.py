import html
import re

import frappe
from erpnext.stock.get_item_details import get_conversion_factor
from frappe import _dict
from frappe.utils import flt, now
from frappe.utils.background_jobs import get_job_status
from rq.job import JobStatus

# An HTML entity, as WordPress writes them: always with its semicolon
HTML_ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def unescape_text(value):
	"""
	Text as WordPress keeps it, HTML-escaped (`Sleeves &amp; Toploader`), as plain text. Only entities with
	their semicolon are decoded: html.unescape alone would also read the `&reg` of a plain `Smith&regular`.
	Anything but a string is returned as it is.
	"""
	if not isinstance(value, str) or "&" not in value:
		return value
	return HTML_ENTITY.sub(lambda match: html.unescape(match[0]), value)


def record_sync_time(server_name: str):
	"""
	Note on the WooCommerce Server when prices or stock were last pushed to it. Written with db.set_value, so
	the server doesn't get a new modified timestamp (and a version) for every push.
	"""
	frappe.db.set_value("WooCommerce Server", server_name, "last_sync_time", now(), update_modified=False)


def get_sales_units(item_codes) -> dict[str, _dict]:
	"""
	The Items among these that sell in another unit than they are stocked in: their Sales UOM (`uom`), their
	`stock_uom`, and how many Stock UOMs one Sales UOM holds (`factor`). The factor is ERPNext's own
	conversion: the UOMs of the Item or its template, else the UOM Conversion Factors.
	"""
	if not item_codes:
		return {}
	units = {}
	for item in frappe.get_all(
		"Item", filters={"name": ["in", list(item_codes)]}, fields=["name", "stock_uom", "sales_uom"]
	):
		if not item.sales_uom or item.sales_uom == item.stock_uom:
			continue
		factor = flt(get_conversion_factor(item.name, item.sales_uom).get("conversion_factor"))
		if factor > 0:
			units[item.name] = _dict(uom=item.sales_uom, stock_uom=item.stock_uom, factor=factor)
	return units


def enqueue_unless_queued(method: str, job_id: str, **kwargs):
	"""
	Enqueue a job, unless a job with this id is still waiting in the queue: that one reads the values when
	it runs. A job that has started may have read them already, so it doesn't count, unlike with Frappe's
	`deduplicate`. The next job then gets an id of its own, as the started job holds this one.
	"""
	status = get_job_status(job_id)
	if status == JobStatus.QUEUED:
		return
	if status == JobStatus.STARTED:
		frappe.enqueue(method, **kwargs)
	else:
		frappe.enqueue(method, job_id=job_id, deduplicate=True, **kwargs)


def enqueue_after_commit_unless_queued(method: str, job_id: str, **kwargs):
	"""
	enqueue_unless_queued once the transaction is committed. A job that is still waiting then starts after
	the commit, so it reads what this transaction wrote.
	"""
	frappe.db.after_commit.add(lambda: enqueue_unless_queued(method, job_id, **kwargs))
