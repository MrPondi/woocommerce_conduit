# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ItemWooCommerceServer(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enable_sync: DF.Check
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		woocommerce_id: DF.Data
		woocommerce_last_sync_hash: DF.Datetime | None
		woocommerce_server: DF.Link
	# end: auto-generated types
	pass
