# Copyright (c) 2026, Karol Parzonka and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WooCommerceServerTaxClass(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		item_tax_template: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		woocommerce_tax_class: DF.Data
		woocommerce_tax_class_title: DF.Data | None
	# end: auto-generated types

	pass
