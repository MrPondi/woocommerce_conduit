// Copyright (c) 2025, Karol Parzonka and contributors
// For license information, please see license.txt

frappe.ui.form.on("WooCommerce Server", {
	setup(frm) {
		// The enabled warehouses of the server's company. A group counts with the warehouses under it.
		frm.set_query("warehouses", () => ({
			filters: {
				disabled: 0,
				company: frm.doc.company,
			},
		}));

		// Ledger accounts of the server's company
		const account_query = () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		});
		frm.set_query("freight_and_forwarding_account", account_query);
		frm.set_query("fee_account", account_query);

		// Only the tax templates of the server's company
		const tax_template_query = () => ({
			filters: { company: frm.doc.company, disabled: 0 },
		});
		frm.set_query("sales_taxes_and_charges_template", tax_template_query);
		frm.set_query("default_item_tax_template", tax_template_query);
		frm.set_query("item_tax_template", "tax_class_map", tax_template_query);
	},

	refresh(frm) {
		// The Item fields a WooCommerce field can be mapped to, from Item's meta (custom fields included)
		frappe.model.with_doctype("Item", () => {
			const options = frappe.meta
				.get_docfields("Item")
				.filter(
					(df) =>
						!frappe.model.no_value_type.includes(df.fieldtype) &&
						!frappe.model.table_fields.includes(df.fieldtype)
				)
				.sort((a, b) => (a.label || a.fieldname).localeCompare(b.label || b.fieldname))
				.map((df) => `${df.fieldname} | ${df.label || df.fieldname}`)
				.join("\n");
			frm.fields_dict.item_field_map.grid.update_docfield_property(
				"erpnext_field_name",
				"options",
				options
			);
		});
	},

	// Add the order statuses of the shop that the Order Status Map lacks
	load_default_order_statuses: function (frm) {
		add_missing_rows(frm, "get_missing_order_status_rows", "sales_order_status_map", {
			added: __("{0} statuses added"),
			none: __("No statuses missing"),
			unreachable: __("Couldn't reach WooCommerce ({0}), only the core statuses were added"),
		});
	},

	check_order_items: function (frm) {
		new OrderItemCheck(frm).show();
	},

	load_payment_methods: function (frm) {
		add_missing_rows(frm, "get_missing_payment_method_rows", "payment_method_map", {
			added: __("{0} payment methods added"),
			none: __("No payment methods missing"),
			unreachable: __("Couldn't reach WooCommerce ({0}), no payment methods were added"),
		});
	},

	load_tax_classes: function (frm) {
		add_missing_rows(frm, "get_missing_tax_class_rows", "tax_class_map", {
			added: __("{0} tax classes added"),
			none: __("No tax classes missing"),
			unreachable: __("Couldn't reach WooCommerce ({0}), no tax classes were added"),
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
						"/api/method/woocommerce_conduit.endpoint.order_created"
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
					10
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

// The products of the latest orders that have no Item, each to link to the Item that looks like it or to
// create an Item for
class OrderItemCheck {
	constructor(frm) {
		this.frm = frm;
		this.dialog = new frappe.ui.Dialog({
			title: __("Check Order Items"),
			size: "extra-large",
			fields: [
				{
					label: __("Orders to Check"),
					fieldname: "order_count",
					fieldtype: "Int",
					default: 200,
					description: __("The newest orders of the shop, up to 2000"),
				},
				{ fieldtype: "HTML", fieldname: "results" },
			],
			primary_action_label: __("Check"),
			primary_action: () => this.check(),
		});
		this.dialog.fields_dict.results.$wrapper.on("click", "button[data-action]", (e) =>
			this.act($(e.currentTarget).data())
		);
	}

	show() {
		this.dialog.show();
	}

	check() {
		const results = this.dialog.fields_dict.results;
		results.$wrapper.html(`<p class="text-muted">${__("Reading the orders...")}</p>`);
		frappe.call({
			method: "woocommerce_conduit.tasks.item_parity.get_unlinked_order_products",
			args: {
				woocommerce_server: this.frm.doc.name,
				order_count: this.dialog.get_value("order_count"),
			},
			freeze: true,
			callback: (r) => this.render(r.message || []),
			error: () => results.$wrapper.empty(),
		});
	}

	render(products) {
		const results = this.dialog.fields_dict.results;
		if (!products.length) {
			results.$wrapper.html(
				`<p class="text-success">${__("Every product of these orders has an Item")}</p>`
			);
			return;
		}
		const esc = frappe.utils.escape_html;
		const rows = products
			.map((p) => {
				const link = p.suggested_item
					? `<button class="btn btn-xs btn-primary" data-action="link" data-product-id="${esc(
							p.product_id
					  )}" data-item-code="${esc(p.suggested_item)}">${__("Link to {0}", [
							esc(p.suggested_item),
					  ])}</button>`
					: "";
				return `<tr>
					<td><a href="${frappe.utils.get_form_link(
						"WooCommerce Product",
						p.woocommerce_product_name
					)}" target="_blank">${esc(p.name || p.product_id)}</a></td>
					<td>${esc(p.sku || "")}</td>
					<td class="text-right">${p.orders}</td>
					<td class="text-right">${format_number(p.qty)}</td>
					<td class="text-nowrap">${link}
						<button class="btn btn-xs btn-default" data-action="create" data-product-name="${esc(
							p.woocommerce_product_name
						)}">${__("Create Item")}</button>
					</td>
				</tr>`;
			})
			.join("");
		results.$wrapper.html(`
			<p class="text-muted">${__("{0} products have no Item", [products.length])}</p>
			<table class="table table-bordered table-condensed">
				<thead><tr>
					<th>${__("Product")}</th><th>${__("SKU")}</th>
					<th class="text-right">${__("Orders")}</th><th class="text-right">${__("Qty")}</th><th></th>
				</tr></thead>
				<tbody>${rows}</tbody>
			</table>`);
	}

	act(data) {
		const call =
			data.action === "link"
				? {
						method: "woocommerce_conduit.tasks.item_parity.link_product_to_item",
						args: {
							woocommerce_server: this.frm.doc.name,
							product_id: String(data.productId),
							item_code: data.itemCode,
						},
				  }
				: {
						method: "woocommerce_conduit.tasks.sync_items.sync_woocommerce_product",
						args: { woocommerce_product_name: data.productName },
				  };
		frappe.call({ ...call, freeze: true, callback: () => this.check() });
	}
}
