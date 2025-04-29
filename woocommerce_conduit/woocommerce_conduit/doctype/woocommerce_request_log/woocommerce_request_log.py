# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class WooCommerceRequestLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		data: DF.JSON | None
		endpoint: DF.Data | None
		error: DF.Code | None
		method: DF.Data | None
		params: DF.JSON | None
		response: DF.Text | None
		seen: DF.Check
		status: DF.Literal["Success", "Error"]
		traceback: DF.Code | None
		url: DF.SmallText | None
		user: DF.Link | None
	# end: auto-generated types
	pass
