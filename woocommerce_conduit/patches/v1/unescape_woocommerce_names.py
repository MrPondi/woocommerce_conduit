import frappe

from woocommerce_conduit.tasks.sync import unescape_text

# Matches a value with an HTML entity in it, as WordPress writes them
ENTITY_LIKE = "%&%;%"


def execute():
	"""
	The sync takes names as plain text now, as WordPress keeps them HTML-escaped (`Sleeves &amp; Toploader`).
	This does the same for the names it took before: the Item Attribute values with an entity, renamed
	through the attribute so ERPNext carries the rename to its variants, and the names of the Items linked
	to WooCommerce. A value whose plain twin exists already is left, as the variants of the two would have
	to be merged by hand, and so is an attribute ERPNext won't save. Those are listed.
	"""
	left = rename_attribute_values()
	unescape_item_names()
	if left:
		print(
			"These Item Attribute values were left HTML-escaped, as a plain twin exists or the attribute "
			f"couldn't be saved (see the Error Log): {', '.join(left)}"
		)


def rename_attribute_values() -> list[str]:
	"""
	Unescape the values of the Item Attributes on the rows they have, which ERPNext takes for a rename and
	carries to the variants. Returns the values that were left as they are.
	"""
	left = []
	names = frappe.get_all(
		"Item Attribute Value",
		filters={"parenttype": "Item Attribute", "attribute_value": ["like", ENTITY_LIKE]},
		pluck="parent",
		distinct=True,
	)
	for name in names:
		attribute = frappe.get_doc("Item Attribute", name)
		if attribute.numeric_values:
			continue
		# ERPNext refuses two values that differ only in case
		values = {row.attribute_value.lower() for row in attribute.item_attribute_values}
		renamed = []
		for row in attribute.item_attribute_values:
			plain = unescape_text(row.attribute_value)
			if plain == row.attribute_value:
				continue
			if plain.lower() in values:
				left.append(f"{name}: {row.attribute_value}")
				continue
			values.add(plain.lower())
			renamed.append(row.attribute_value)
			# The abbreviation stays, as ERPNext would rename the variants' codes with it
			row.attribute_value = plain
		if not renamed:
			continue

		# A patch that fails stops the migration, so an attribute ERPNext won't save is left and logged
		frappe.db.savepoint("unescape_attribute")
		try:
			attribute.save()
		except Exception:
			frappe.db.rollback(save_point="unescape_attribute")
			frappe.log_error("WooCommerce Patch Error", f"{frappe.get_traceback()}\n\nItem Attribute: {name}")
			left.extend(f"{name}: {value}" for value in renamed)
	return left


def unescape_item_names() -> None:
	"""
	Unescape the names of the Items linked to WooCommerce, and the copy their Item Prices keep, which a save
	of the Item would have set
	"""
	item = frappe.qb.DocType("Item")
	connection = frappe.qb.DocType("Item WooCommerce Server")
	item_price = frappe.qb.DocType("Item Price")
	linked = frappe.qb.from_(connection).select(connection.parent).where(connection.parenttype == "Item")
	rows = (  # pyright: ignore[reportCallIssue]
		frappe.qb.from_(item)
		.where(item.item_name.like(ENTITY_LIKE) & item.name.isin(linked))
		.select(item.name, item.item_name)
	).run(as_dict=True)
	for row in rows:
		plain = unescape_text(row.item_name)
		if plain == row.item_name:
			continue
		frappe.db.set_value("Item", row.name, "item_name", plain, update_modified=False)
		frappe.qb.update(item_price).set(item_price.item_name, plain).where(
			item_price.item_code == row.name
		).run()
