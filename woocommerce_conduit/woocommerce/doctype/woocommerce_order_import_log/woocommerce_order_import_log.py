# Copyright (c) 2026, Karol Parzonka and contributors
# For license information, please see license.txt

from frappe.model.document import Document

# What a WooCommerce Order Import did with an order
RESULT_PAID = "Paid"
RESULT_NEEDS_REVIEW = "Needs Review"
RESULT_SKIPPED = "Skipped"
RESULT_FAILED = "Failed"
# The results a later run of the same import tries again, as nothing of the order was kept
RETRIED_RESULTS = (RESULT_SKIPPED, RESULT_FAILED)


class WooCommerceOrderImportLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		message: DF.SmallText | None
		order_import: DF.Link
		order_number: DF.Data | None
		payment_entry: DF.Link | None
		result: DF.Literal["Paid", "Needs Review", "Skipped", "Failed"]
		sales_invoice: DF.Link | None
		sales_order: DF.Link | None
		woocommerce_id: DF.Data | None
	# end: auto-generated types

	pass
