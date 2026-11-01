// Copyright (c) 2026, Karol Parzonka and contributors
// For license information, please see license.txt

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		// A button rather than a Link field: Frappe validates a link to a virtual doctype by fetching it,
		// which would call WooCommerce on every save
		if (!frm.doc.woocommerce_server || !frm.doc.woocommerce_id) return;
		if (!frappe.model.can_read("WooCommerce Order")) return;

		frm.add_custom_button(
			__("WooCommerce Order"),
			() =>
				frappe.set_route(
					"Form",
					"WooCommerce Order",
					`${frm.doc.woocommerce_server}~${frm.doc.woocommerce_id}`,
				),
			__("View"),
		);
	},
});
