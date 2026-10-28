// Copyright (c) 2025, Karol Parzonka and contributors
// For license information, please see license.txt

// Zoom level for the pin of an address
const ADDRESS_MAP_ZOOM = 14;

// A single pin has no bounds to fit, which leaves the map zoomed far out
function zoom_map_to_pin(control) {
	if (!control) return;
	control.fit_and_recenter_map = function () {
		try {
			this.map.invalidateSize();
			const bounds = this.editableLayers.getBounds();
			if (bounds.isValid()) {
				this.map.setView(bounds.getCenter(), ADDRESS_MAP_ZOOM);
			}
		} catch (err) {
			// the map or its layers are not there yet
		}
	};
	// Once the map is drawn, in case the pin was bound before the override
	control.load_lib().then(() => control.fit_and_recenter_map());
}

frappe.ui.form.on("WooCommerce Order", {
	refresh: function (frm) {
		zoom_map_to_pin(frm.fields_dict.billing_map);
		zoom_map_to_pin(frm.fields_dict.shipping_map);
		// Add a custom button to sync this WooCommerce order to a Sales Order
		frm.add_custom_button(
			__("Sync this Order to ERPNext"),
			function () {
				frm.trigger("sync_sales_order");
			},
			__("Actions")
		);
	},
	sync_sales_order: function (frm) {
		// Sync this WooCommerce Order
		frappe.dom.freeze(__("Sync Order with ERPNext..."));
		frappe.call({
			method: "woocommerce_conduit.tasks.sync_sales_orders.run_sales_order_sync",
			args: {
				woocommerce_order_name: frm.doc.name,
			},
			callback: function (r) {
				console.log(r);
				frappe.dom.unfreeze();
				frappe.show_alert(
					{
						message: __("Sync completed successfully"),
						indicator: "green",
					},
					5
				);
				frm.reload_doc();
			},
			error: (r) => {
				frappe.dom.unfreeze();
				frappe.show_alert(
					{
						message: __("There was an error processing the request. See Error Log."),
						indicator: "red",
					},
					5
				);
			},
		});
	},
});
