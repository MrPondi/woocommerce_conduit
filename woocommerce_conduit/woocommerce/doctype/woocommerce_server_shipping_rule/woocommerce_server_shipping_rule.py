# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import cstr


def get_shipping_method_key(method_id: str | None, instance_id: str | int | None = None) -> str:
	"""
	How the Shipping Method Map names a method of a shipping zone: `flat_rate:3`, as WooCommerce names the
	method's rate at checkout. A method without an instance (none, or 0) is named by its bare method id.
	"""
	method_id = cstr(method_id).strip()
	instance_id = cstr(instance_id).strip()
	if not method_id or instance_id in ("", "0"):
		return method_id
	return f"{method_id}:{instance_id}"


class WooCommerceServerShippingRule(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		shipping_rule: DF.Link | None
		wc_shipping_method_id: DF.Data
		wc_shipping_method_title: DF.Data | None
		wc_shipping_zone: DF.Data | None
	# end: auto-generated types

	pass
