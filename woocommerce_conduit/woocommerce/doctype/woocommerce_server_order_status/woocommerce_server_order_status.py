# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

from frappe.model.document import Document

# What a WooCommerce Order with a status means in ERPNext. The statuses are WooCommerce's, and a shop can
# have more than the core ones, so the server keeps a table of them instead of this code knowing them all.
ACTION_SKIP = "Skip"
ACTION_SUBMIT = "Submit SO"
ACTION_SUBMIT_INVOICE = "Submit SO + Invoice"
ACTION_SUBMIT_HOLD = "Submit SO + Hold"
ACTION_COMPLETE = "Complete SO"
ACTION_CANCEL = "Cancel SO"
ACTION_CLOSE = "Close SO"
ACTION_STATUS_ONLY = "Status only"

# The actions for which a Sales Order is created when there is none yet. Every other action only acts on a
# Sales Order that exists, so an order first seen with it (say one that failed, or was cancelled or refunded
# before it was ever synced) stays out of ERPNext, and no Customer, Address or Contact is made for it either.
CREATING_ACTIONS = (ACTION_SUBMIT, ACTION_SUBMIT_INVOICE, ACTION_SUBMIT_HOLD, ACTION_COMPLETE)
# The actions for which a Sales Invoice is made. Complete SO only makes one for an order first seen
# completed: once an order is in ERPNext, ERPNext is where it is completed.
INVOICING_ACTIONS = (ACTION_SUBMIT_INVOICE, ACTION_COMPLETE)
# The actions after which a Sales Order is not changed any more
ENDING_ACTIONS = (ACTION_CANCEL, ACTION_CLOSE)

# The payment gateway decides on these, so ERPNext must not push them over the gateway's
PAYMENT_OWNED_STATUSES = ("checkout-draft", "pending", "failed", "on-hold", "processing")

# (slug, label, action, the Sales Order status that is pushed as this status)
DEFAULT_STATUSES = (
	("checkout-draft", "Draft", ACTION_SKIP, ""),
	("pending", "Pending payment", ACTION_SUBMIT_INVOICE, ""),
	("failed", "Failed", ACTION_CANCEL, ""),
	("on-hold", "On hold", ACTION_SUBMIT_HOLD, ""),
	("processing", "Processing", ACTION_SUBMIT_INVOICE, ""),
	("completed", "Completed", ACTION_COMPLETE, "Completed"),
	("cancelled", "Cancelled", ACTION_CANCEL, "Cancelled"),
	("refunded", "Refunded", ACTION_CLOSE, ""),
	("trash", "Trash", ACTION_CANCEL, ""),
)


def normalise_status(status: str | None) -> str:
	"""
	The slug as the REST API has it. Statuses are registered as `wc-processing`, the API says `processing`.
	"""
	return (status or "").strip().removeprefix("wc-")


def get_default_status_rows(
	live_statuses: dict[str, str] | None = None, existing: list[str] | tuple[str, ...] = ()
) -> list[dict]:
	"""
	The rows of the status map for the statuses that are not in `existing` yet: the core statuses with
	their defaults, and any other status the shop has (`live_statuses` as slug -> name) that does nothing
	until the user decides what it means.
	"""
	live = {normalise_status(slug): name for slug, name in (live_statuses or {}).items()}
	existing = {normalise_status(slug) for slug in existing}

	rows = []
	for slug, label, action, push_status in DEFAULT_STATUSES:
		if slug not in existing:
			rows.append(
				{
					"woocommerce_status": slug,
					"woocommerce_status_label": live.get(slug, label),
					"erpnext_action": action,
					"push_from_erpnext_status": push_status,
				}
			)

	core = {slug for slug, *_ in DEFAULT_STATUSES}
	for slug, name in live.items():
		if slug and slug not in existing and slug not in core:
			rows.append(
				{
					"woocommerce_status": slug,
					"woocommerce_status_label": name,
					"erpnext_action": ACTION_STATUS_ONLY,
					"push_from_erpnext_status": "",
				}
			)
	return rows


class WooCommerceServerOrderStatus(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		erpnext_action: DF.Literal[
			"Skip",
			"Skip, cancel existing",
			"Submit SO",
			"Submit SO + Invoice",
			"Submit SO + Hold",
			"Cancel SO",
			"Close SO",
			"Status only",
		]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		push_from_erpnext_status: DF.Literal[
			"",
			"Draft",
			"On Hold",
			"To Deliver and Bill",
			"To Bill",
			"To Deliver",
			"Completed",
			"Cancelled",
			"Closed",
		]
		woocommerce_status: DF.Data
		woocommerce_status_label: DF.Data | None
	# end: auto-generated types

	pass
