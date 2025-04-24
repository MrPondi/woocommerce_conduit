// Copyright (c) 2025, Karol Parzonka and contributors
// For license information, please see license.txt

frappe.ui.form.on("WooCommerce Product", {
    refresh(frm) {
        // Add a custom button to sync this WooCommerce order to a Sales Order
        frm.add_custom_button(__("Sync this Item to ERPNext"), function () {
            frm.trigger("sync_product");
        }, __('Actions'));
    },
    sync_product: function (frm) {
        // Sync this WooCommerce Product
        frappe.dom.freeze(__("Sync Product with ERPNext..."));
        frappe.call({
            method: "woocommerce_conduit.tasks.sync_items.run_item_sync",
            args: {
                woocommerce_product_name: frm.doc.name
            },
            callback: function (r) {
                console.log(r);
                frappe.dom.unfreeze();
                frappe.show_alert({
                    message: __('Sync completed successfully'),
                    indicator: 'green'
                }, 5);
                frm.reload_doc();
            },
            error: (r) => {
                frappe.dom.unfreeze();
                frappe.show_alert({
                    message: __('There was an error processing the request. See Error Log.'),
                    indicator: 'red'
                }, 5);
            }
        });
    },
});

