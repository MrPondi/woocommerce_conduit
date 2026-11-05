import frappe
from frappe.utils.password import set_encrypted_password


def execute():
	"""
	The webhook secret is a Password field now, kept encrypted as the API keys are. Each server's secret moves
	from its column, which then holds the mask Frappe keeps there, into Frappe's encrypted store. The column is
	text both before and after, so the model sync left the secret in it.
	"""
	for server in frappe.get_all("WooCommerce Server", fields=["name", "secret"]):
		if not server.secret or is_masked(server.secret):
			continue
		set_encrypted_password("WooCommerce Server", server.name, server.secret, "secret")
		frappe.db.set_value(
			"WooCommerce Server", server.name, "secret", "*" * len(server.secret), update_modified=False
		)


def is_masked(value: str) -> bool:
	"""
	Whether a Password field's column holds the mask rather than a secret, as Frappe's is_dummy_password tells
	"""
	return set(value) == {"*"}
