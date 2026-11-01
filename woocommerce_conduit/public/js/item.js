// Copyright (c) 2026, Karol Parzonka and contributors
// For license information, please see license.txt

frappe.ui.form.on("Item", {
	refresh(frm) {
		if (frm.is_new() || !(frm.doc.woocommerce_servers || []).length) return;

		// Prices and stock go from ERPNext to WooCommerce. The Item itself was pulled once, when it
		// was created; the last button pulls it again on request.
		if (frm.doc.is_stock_item && !frm.doc.has_variants) {
			frm.add_custom_button(
				__("Sync Stock to WooCommerce"),
				() =>
					sync_with_woocommerce(
						frm,
						"woocommerce_conduit.tasks.stock_update.sync_item_stock_to_woocommerce",
						__("Syncing stock to WooCommerce..."),
					),
				__("WooCommerce"),
			);
		}
		frm.add_custom_button(
			__("Sync Prices to WooCommerce"),
			() =>
				sync_with_woocommerce(
					frm,
					"woocommerce_conduit.tasks.sync_item_prices.sync_item_price_to_woocommerce",
					__("Syncing prices to WooCommerce..."),
				),
			__("WooCommerce"),
		);
		frm.add_custom_button(
			__("Update Item from WooCommerce"),
			() =>
				frappe.confirm(
					__(
						"This overwrites the Item's name, image and mapped fields with those of its primary WooCommerce Product. Continue?",
					),
					() =>
						sync_with_woocommerce(
							frm,
							"woocommerce_conduit.tasks.sync_items.sync_item_from_woocommerce",
							__("Updating Item from WooCommerce..."),
						),
				),
			__("WooCommerce"),
		);
	},
});

frappe.ui.form.on("Item WooCommerce Server", {
	view_product(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.woocommerce_server || !row.woocommerce_id) return;
		frappe.set_route(
			"Form",
			"WooCommerce Product",
			`${row.woocommerce_server}~${row.woocommerce_id}`,
		);
	},
});

function sync_with_woocommerce(frm, method, freeze_message) {
	frappe.call({
		method: method,
		args: { item_code: frm.doc.name },
		freeze: true,
		freeze_message: freeze_message,
		callback: () => {
			frappe.show_alert(
				{ message: __("Sync completed successfully"), indicator: "green" },
				5,
			);
			frm.reload_doc();
		},
	});
}
