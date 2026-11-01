import frappe
from frappe.utils import now


def record_sync_time(server_name: str):
	"""
	Note on the WooCommerce Server when prices or stock were last pushed to it. Written with db.set_value, so
	the server doesn't get a new modified timestamp (and a version) for every push.
	"""
	frappe.db.set_value("WooCommerce Server", server_name, "last_sync_time", now(), update_modified=False)
