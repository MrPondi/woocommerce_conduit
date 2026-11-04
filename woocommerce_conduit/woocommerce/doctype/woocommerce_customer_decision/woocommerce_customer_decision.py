# Copyright (c) 2026, Karol Parzonka and contributors
# For license information, please see license.txt

from frappe.model.document import Document

# What a person decided about two Customers that looked like the same buyer
DECISION_NOT_THE_SAME = "Not the Same"
DECISION_MERGED = "Merged"


class WooCommerceCustomerDecision(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		customer: DF.Link
		decision: DF.Literal["Not the Same", "Merged"]
		merged_customer: DF.Data | None
		merged_customer_name: DF.Data | None
		other_customer: DF.Link | None
		woocommerce_identifier: DF.Data | None
		woocommerce_server: DF.Link | None
	# end: auto-generated types

	pass
