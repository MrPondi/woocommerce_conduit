from collections import defaultdict

import frappe

# The child doctype the map was in on the WooCommerce Server. Its folder is gone, and migrate deletes the
# DocType after the patches, but Frappe keeps its table, so the rows can be read here.
OLD_TABLE = "WooCommerce Server Payment Method"
FIELDS = ("woocommerce_payment_method", "woocommerce_payment_method_title", "mode_of_payment", "bank_account")


def execute():
	"""
	The Payment Method Map moved from the WooCommerce Server to the WooCommerce Order Import, the one place the
	app books payments now. A server whose map gives a payment method a Mode of Payment gets a Pending import
	with its rows, so nothing a person typed is lost. The fieldnames are written out, so the patch stays as it
	is when the doctypes change.
	"""
	move_rows(read_old_rows())


def read_old_rows() -> list[dict]:
	if not frappe.db.table_exists(OLD_TABLE):
		return []
	table = frappe.qb.Table(f"tab{OLD_TABLE}")
	return (
		frappe.qb.from_(table)
		.select(table.parent, *(table[field] for field in FIELDS))
		.where((table.parenttype == "WooCommerce Server") & (table.parentfield == "payment_method_map"))
		.orderby(table.parent)
		.orderby(table.idx)
	).run(as_dict=True)


def move_rows(rows: list[dict]) -> list[str]:
	"""
	Make an import of each server's rows, and return their names. A server that has an import already keeps
	it as it is.
	"""
	rows_by_server = defaultdict(list)
	for row in rows:
		rows_by_server[row["parent"]].append(row)

	made = []
	for server, server_rows in rows_by_server.items():
		if not any(row.get("mode_of_payment") for row in server_rows):
			continue
		if not frappe.db.exists("WooCommerce Server", server):
			continue
		if frappe.db.exists("WooCommerce Order Import", {"woocommerce_server": server}):
			continue

		doc = frappe.get_doc(
			{
				"doctype": "WooCommerce Order Import",
				"woocommerce_server": server,
				# Its fetch isn't run, as the links aren't checked
				"company": frappe.db.get_value("WooCommerce Server", server, "company"),
				"payment_method_map": [{field: row.get(field) for field in FIELDS} for row in server_rows],
			}
		)
		# The server checked the rows when they were saved, and an import made by a patch calls no shop
		doc.flags.ignore_validate = True
		doc.flags.ignore_links = True
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		made.append(doc.name)
	return made
