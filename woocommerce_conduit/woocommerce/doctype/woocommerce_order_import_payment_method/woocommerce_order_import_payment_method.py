# Copyright (c) 2026, Karol Parzonka and contributors
# For license information, please see license.txt

from frappe.model.document import Document

# When the invoice and payment of an order paid with a method are dated: when WooCommerce took the order as
# paid, or when it was completed, for money that comes in after WooCommerce's paid date
PAYMENT_DATE_PAID = "When Paid"
PAYMENT_DATE_COMPLETED = "When Completed"


class WooCommerceOrderImportPaymentMethod(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bank_account: DF.Link | None
		mode_of_payment: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		payment_date: DF.Literal["When Paid", "When Completed"]
		woocommerce_payment_method: DF.Data
		woocommerce_payment_method_title: DF.Data | None
	# end: auto-generated types

	pass
