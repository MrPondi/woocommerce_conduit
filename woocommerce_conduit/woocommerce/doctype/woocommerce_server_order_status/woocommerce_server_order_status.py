# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

from frappe.model.document import Document

# What a WooCommerce Order with a status does in ERPNext, each action a Check of the status row. The statuses
# are WooCommerce's, and a shop can have more than the core ones, so the server keeps a table of them instead
# of this code knowing them all. A status that ticks nothing, or that the map doesn't know, only updates the
# Sales Order of an order.

# The one action that creates a Sales Order when there is none yet. Every other action only acts on a Sales
# Order that exists, so an order first seen without it (say one that failed, or was cancelled or refunded
# before it was ever synced) stays out of ERPNext, and no Customer, Address or Contact is made for it either.
ACTION_SUBMIT = "action_submit"
ACTION_INVOICE = "action_invoice"
ACTION_DELIVER = "action_deliver"
# ERPNext doesn't invoice a held order, so with an invoice the hold comes after it
ACTION_HOLD = "action_hold"
ACTION_CANCEL = "action_cancel"
ACTION_CLOSE = "action_close"
# Orders with the status are left out of the hourly sync, as nothing is to be done with them
ACTION_SKIP = "action_skip"
ACTIONS = (
	ACTION_SUBMIT,
	ACTION_INVOICE,
	ACTION_DELIVER,
	ACTION_HOLD,
	ACTION_CANCEL,
	ACTION_CLOSE,
	ACTION_SKIP,
)

# The actions that are ticked alone, as nothing else is done with an order that is cancelled, closed or skipped
SOLE_ACTIONS = frozenset({ACTION_CANCEL, ACTION_CLOSE, ACTION_SKIP})
# The actions after which a Sales Order is not changed any more
ENDING_ACTIONS = frozenset({ACTION_CANCEL, ACTION_CLOSE})
# The actions that lift a hold, as ERPNext doesn't invoice or deliver an order on hold
RESUMING_ACTIONS = frozenset({ACTION_SUBMIT, ACTION_INVOICE, ACTION_DELIVER})

# The payment gateway decides on these, so ERPNext must not push them over the gateway's
PAYMENT_OWNED_STATUSES = ("checkout-draft", "pending", "failed", "on-hold", "processing")
# The status of an order in the trash. An order that the shop deleted for good is taken for one in the trash,
# which it usually went through.
TRASH_STATUS = "trash"

# (slug, label, actions, the Sales Order status that is pushed as this status)
DEFAULT_STATUSES = (
	("checkout-draft", "Draft", (ACTION_SKIP,), ""),
	("pending", "Pending payment", (ACTION_SUBMIT, ACTION_INVOICE), ""),
	("failed", "Failed", (ACTION_CANCEL,), ""),
	("on-hold", "On hold", (ACTION_SUBMIT, ACTION_HOLD), ""),
	("processing", "Processing", (ACTION_SUBMIT, ACTION_INVOICE), ""),
	("completed", "Completed", (ACTION_SUBMIT, ACTION_INVOICE, ACTION_DELIVER), "Completed"),
	("cancelled", "Cancelled", (ACTION_CANCEL,), "Cancelled"),
	("refunded", "Refunded", (ACTION_CLOSE,), ""),
	("trash", "Trash", (ACTION_CANCEL,), ""),
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
	for slug, label, actions, push_status in DEFAULT_STATUSES:
		if slug not in existing:
			rows.append(
				{
					"woocommerce_status": slug,
					"woocommerce_status_label": live.get(slug, label),
					**dict.fromkeys(actions, 1),
					"push_from_erpnext_status": push_status,
				}
			)

	core = {slug for slug, *_ in DEFAULT_STATUSES}
	for slug, name in live.items():
		if slug and slug not in existing and slug not in core:
			# Nothing ticked
			rows.append(
				{"woocommerce_status": slug, "woocommerce_status_label": name, "push_from_erpnext_status": ""}
			)
	return rows


class WooCommerceServerOrderStatus(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		action_cancel: DF.Check
		action_close: DF.Check
		action_deliver: DF.Check
		action_hold: DF.Check
		action_invoice: DF.Check
		action_skip: DF.Check
		action_submit: DF.Check
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

	def get_actions(self) -> frozenset[str]:
		"""
		The actions that the row ticks
		"""
		return frozenset(action for action in ACTIONS if self.get(action))
