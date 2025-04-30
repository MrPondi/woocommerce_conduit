// Copyright (c) 2025, Karol Parzonka and contributors
// For license information, please see license.txt

frappe.ui.form.on("WooCommerce Server", {
//   onload_post_render: function(frm) {
//     setTimeout(() => {
//       frm.fields_dict.api_consumer_secret.$wrapper.find('.password-strength-indicator').remove();
//       frm.fields_dict.api_consumer_secret.$wrapper.find('.help-box.small.text-muted').remove();
//     }, 0);
//   }
    refresh(frm) {

        // Set the Options for erpnext_field_name field on 'Fields Mapping' child table
        frappe.call({
            method: "get_item_docfields",
            doc: frm.doc,
            callback: function (r) {
                // Sort the array of objects alphabetically by the label property
                r.message.sort((a, b) => a.label.localeCompare(b.label));

                // Use map to create an array of strings in the desired format
                const formattedStrings = r.message.map(fields => `${fields.fieldname} | ${fields.label}`);

                // Join the strings with newline characters to create the final string
                const options = formattedStrings.join('\n');

                // Set the Options property
                frm.fields_dict.item_field_map.grid.update_docfield_property(
                    "erpnext_field_name",
                    "options",
                    options
                );
            }
        });

		// Only list enabled warehouses
		frm.fields_dict.warehouses.get_query = function (doc) {
			return {
				filters: {
					disabled: 0,
					is_group: 0
				}
			};
		}

		if (frm.doc.enabled_order_status && !frm.fields_dict.sales_order_status_map.grid.get_docfield("woocommerce_sales_order_status").options) {
			frm.trigger('get_woocommerce_order_status_list');
		}

    },
	// Handle click of 'Keep the Status of ERPNext Sales Orders and WooCommerce Orders in sync'
	enabled_order_status: function(frm){
		if (frm.doc.enabled_order_status && !frm.fields_dict.sales_order_status_map.grid.get_docfield("woocommerce_sales_order_status").options){
			frm.trigger('get_woocommerce_order_status_list');
		}
	},

	// Retrieve WooCommerce order statuses
	get_woocommerce_order_status_list: function(frm){
		frappe.call({
			method: "get_woocommerce_order_status_list",
			doc: frm.doc,
			callback: function(r) {
				// Join the strings with newline characters to create the final string
				const options = r.message.join('\n');

				// Set the Options property
				frm.fields_dict.sales_order_status_map.grid.update_docfield_property(
					"woocommerce_sales_order_status",
					"options",
					options
				);
			}
		});
	},
});