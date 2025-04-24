// Copyright (c) 2025, Karol Parzonka and contributors
// For license information, please see license.txt

frappe.ui.form.on("WooCommerce Settings", {
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

	},
});
