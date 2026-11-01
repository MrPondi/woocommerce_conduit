from functools import cached_property

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

	def __init__(self, servers: list[WooCommerceServer | _dict] | None = None) -> None:
		if servers:
			self.servers = servers

	@cached_property
	def servers(self) -> list[WooCommerceServer | _dict]:
		"""
		Loaded when first used, so a sync that stops early (a paused order sync) doesn't query them
		"""
		return self.get_wc_servers()

	@staticmethod
	def get_wc_servers() -> list[WooCommerceServer | _dict]:
		wc_servers = frappe.get_all("WooCommerce Server")
		return [frappe.get_doc("WooCommerce Server", server.name) for server in wc_servers]  # type: ignore
