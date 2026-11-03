import frappe

# The actions that each choice of the old Select ticks; Status only ticks none. The fieldnames are written
# out, so the patch stays as it is when the app's own constants change.
TICKS_OF_CHOICES = {
	"Skip": ("action_skip",),
	"Submit SO": ("action_submit",),
	"Submit SO + Invoice": ("action_submit", "action_invoice"),
	"Submit SO + Hold": ("action_submit", "action_hold"),
	"Submit SO + Invoice + Hold": ("action_submit", "action_invoice", "action_hold"),
	"Complete SO": ("action_submit", "action_invoice", "action_deliver"),
	"Cancel SO": ("action_cancel",),
	"Close SO": ("action_close",),
}


def execute():
	"""
	A row of the Order Status Map ticks each action of its status now, instead of choosing one of a list of
	combinations (erpnext_action). Frappe keeps the column of a field that was removed, so the choices can
	still be read here.
	"""
	if not frappe.db.has_column("WooCommerce Server Order Status", "erpnext_action"):
		return

	table = frappe.qb.DocType("WooCommerce Server Order Status")
	for choice, ticks in TICKS_OF_CHOICES.items():
		query = frappe.qb.update(table).where(table.erpnext_action == choice)
		for fieldname in ticks:
			query = query.set(table[fieldname], 1)
		query.run()
