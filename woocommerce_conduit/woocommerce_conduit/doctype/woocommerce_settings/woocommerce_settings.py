# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class WooCommerceSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		fetch_variations: DF.Check
		max_variations: DF.Int
		minimum_creation_date: DF.Datetime
		variation_batch_size: DF.Int
		wc_last_sync_date_items: DF.Datetime | None
		wc_last_sync_date_orders: DF.Datetime | None
	# end: auto-generated types
	pass
