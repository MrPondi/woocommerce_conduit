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
				const formattedStrings = r.message.map(
					(fields) => `${fields.fieldname} | ${fields.label}`,
				);

				// Join the strings with newline characters to create the final string
				const options = formattedStrings.join("\n");

				// Set the Options property
				frm.fields_dict.item_field_map.grid.update_docfield_property(
					"erpnext_field_name",
					"options",
					options,
				);
			},
		});

		// Only list enabled warehouses
		frm.fields_dict.warehouses.get_query = function (doc) {
			return {
				filters: {
					disabled: 0,
					is_group: 0,
				},
			};
		};
	},

	// Add the order statuses of the shop that the Order Status Map lacks
	load_default_order_statuses: function (frm) {
		add_missing_rows(frm, "get_missing_order_status_rows", "sales_order_status_map", {
			added: __("{0} statuses added"),
			none: __("No statuses missing"),
			unreachable: __("Couldn't reach WooCommerce ({0}), only the core statuses were added"),
		});
	},

	load_payment_methods: function (frm) {
		add_missing_rows(frm, "get_missing_payment_method_rows", "payment_method_map", {
			added: __("{0} payment methods added"),
			none: __("No payment methods missing"),
			unreachable: __("Couldn't reach WooCommerce ({0}), no payment methods were added"),
		});
	},

	// View WooCommerce Webhook Configuration
	view_webhook_config: function (frm) {
		let d = new frappe.ui.Dialog({
			title: __("WooCommerce Webhook Settings"),
			fields: [
				{
					label: __("Status"),
					fieldname: "status",
					fieldtype: "Data",
					default: "Active",
					read_only: 1,
				},
				{
					label: __("Topic"),
					fieldname: "topic",
					fieldtype: "Data",
					default: "Order created, and a second webhook with Order updated",
					read_only: 1,
				},
				{
					label: __("Delivery URL"),
					fieldname: "url",
					fieldtype: "Data",
					default: window.location.protocol.concat(
						"//",
						window.location.host,
						"/api/method/woocommerce_conduit.endpoint.order_created",
					),
					read_only: 1,
				},
				{
					label: __("Secret"),
					fieldname: "secret",
					fieldtype: "Data",
					default: frm.doc.secret,
					read_only: 1,
				},
				{
					label: __("API Version"),
					fieldname: "api_version",
					fieldtype: "Data",
					default: "WP REST API Integration v3",
					read_only: 1,
				},
			],
			size: "large", // small, large, extra-large
			primary_action_label: __("OK"),
			primary_action(values) {
				d.hide();
			},
		});

		d.show();
	},
});

// Add the rows of a map table that the server says are missing, and say what happened
function add_missing_rows(frm, method, table, messages) {
	frm.call({
		doc: frm.doc,
		method: method,
		callback: function (r) {
			const { rows = [], error } = r.message || {};
			rows.forEach((row) => frm.add_child(table, row));
			frm.refresh_field(table);
			if (error) {
				frappe.show_alert(
					{ message: messages.unreachable.replace("{0}", error), indicator: "orange" },
					10,
				);
				return;
			}
			frappe.show_alert({
				message: rows.length ? messages.added.replace("{0}", rows.length) : messages.none,
				indicator: rows.length ? "green" : "blue",
			});
		},
	});
}
