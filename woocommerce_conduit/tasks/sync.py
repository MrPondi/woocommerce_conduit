import frappe
from frappe import _, _dict

from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)


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
