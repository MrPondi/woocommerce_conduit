// Copyright (c) 2025, Karol Parzonka and contributors
// For license information, please see license.txt

frappe.ui.form.on("WooCommerce Order", {
	refresh: function (frm) {
		frm.trigger("set_status_options");
		frm.trigger("render_lines");

		if (frm.doc.sales_order) {
			frm.add_custom_button(__("Open Sales Order"), function () {
				frappe.set_route("Form", "Sales Order", frm.doc.sales_order);
			});
			frm.add_custom_button(
				__("Sync with Sales Order"),
				function () {
					frm.trigger("sync_sales_order");
				},
				__("Actions"),
			);
		} else {
			frm.add_custom_button(__("Create Sales Order"), function () {
				frm.trigger("sync_sales_order");
			});
		}
	},

	// Suggest the statuses of the server's Order Status Map
	set_status_options: function (frm) {
		if (!frm.doc.woocommerce_server) return;
		frappe.call({
			method: "woocommerce_conduit.woocommerce.doctype.woocommerce_order.woocommerce_order.get_status_options",
			args: { woocommerce_server: frm.doc.woocommerce_server },
			callback: function (r) {
				frm.set_df_property("status", "options", r.message || []);
			},
		});
	},

	// The lines as a table, with the Item each one is linked to
	render_lines: function (frm) {
		const field = frm.get_field("line_items_html");
		if (!field) return;

		let lines = [];
		try {
			lines = JSON.parse(frm.doc.line_items || "[]");
		} catch (e) {
			lines = [];
		}
		const item_codes = (frm.doc.__onload && frm.doc.__onload.item_codes) || {};
		const currency = frm.doc.currency;
		const escape = (value) => frappe.utils.escape_html(String(value ?? ""));

		const rows = lines.map((line) => {
			const product_id = String(line.variation_id || line.product_id || 0);
			const item_code = item_codes[product_id];
			const qty = flt(line.quantity);
			const item = item_code
				? frappe.utils.get_form_link("Item", item_code, true)
				: `<span class="text-muted">${__("Not linked")}</span>`;
			return `<tr>
				<td>${escape(line.name)}</td>
				<td>${escape(line.sku)}</td>
				<td class="text-right">${qty}</td>
				<td class="text-right">${format_currency(qty ? flt(line.total) / qty : 0, currency)}</td>
				<td class="text-right">${format_currency(line.total_tax, currency)}</td>
				<td class="text-right">${format_currency(line.total, currency)}</td>
				<td>${item}</td>
			</tr>`;
		});

		field.$wrapper.html(`
			<table class="table table-bordered table-sm">
				<thead>
					<tr>
						<th>${__("Product")}</th>
						<th>${__("SKU")}</th>
						<th class="text-right">${__("Qty")}</th>
						<th class="text-right">${__("Rate")}</th>
						<th class="text-right">${__("Tax")}</th>
						<th class="text-right">${__("Total")}</th>
						<th>${__("Item")}</th>
					</tr>
				</thead>
				<tbody>${rows.join("")}</tbody>
			</table>
		`);
	},

	sync_sales_order: function (frm) {
		// A failure is shown by frappe.call itself
		frappe.call({
			method: "woocommerce_conduit.tasks.sync_sales_orders.sync_woocommerce_order",
			args: {
				woocommerce_order_name: frm.doc.name,
			},
			freeze: true,
			freeze_message: __("Sync Order with ERPNext..."),
			callback: function (r) {
				const sales_order = r.message && r.message[0];
				frappe.show_alert(
					{
						message: sales_order
							? __("Synced with Sales Order {0}", [sales_order.name])
							: __("An order with status {0} doesn't make a Sales Order", [
									frm.doc.status,
								]),
						indicator: sales_order ? "green" : "blue",
					},
					5,
				);
				frm.reload_doc();
			},
		});
	},
});
