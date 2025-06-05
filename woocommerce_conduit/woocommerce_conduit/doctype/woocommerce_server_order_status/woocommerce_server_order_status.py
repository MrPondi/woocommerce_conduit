# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class WooCommerceServerOrderStatus(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		erpnext_sales_order_status: DF.Literal[
			"Draft",
			"On Hold",
			"To Deliver and Bill",
			"To Bill",
			"To Deliver",
			"Completed",
			"Cancelled",
			"Closed",
		]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		woocommerce_sales_order_status: DF.Literal[None]
	# end: auto-generated types
	pass
