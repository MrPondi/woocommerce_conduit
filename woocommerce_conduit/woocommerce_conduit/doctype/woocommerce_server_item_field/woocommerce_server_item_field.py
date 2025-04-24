# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class WooCommerceServerItemField(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		erpnext_field_name: DF.Literal[None]  # type: ignore
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		woocommerce_field_name: DF.Data
		# end: auto-generated types
		erpnext_field_name: str
	pass
