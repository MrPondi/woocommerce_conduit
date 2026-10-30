import frappe
from frappe import _, _dict
from frappe.utils import now

from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)


def record_sync_time(server_name: str):
	"""
	Note on the WooCommerce Server when prices or stock were last pushed to it. Written with db.set_value, so
	the server doesn't get a new modified timestamp (and a version) for every push.
	"""
	frappe.db.set_value("WooCommerce Server", server_name, "last_sync_time", now(), update_modified=False)


class SynchroniseWooCommerce:
	"""
	Class for managing synchronisation of WooCommerce data with ERPNext data
	"""

	servers: list[WooCommerceServer | _dict]

	def __init__(self, servers: list[WooCommerceServer | _dict] | None = None) -> None:
		self.servers = servers if servers else self.get_wc_servers()

	@staticmethod
	def get_wc_servers() -> list[WooCommerceServer | _dict]:
		wc_servers = frappe.get_all("WooCommerce Server")
		return [frappe.get_doc("WooCommerce Server", server.name) for server in wc_servers]  # type: ignore
