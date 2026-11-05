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

		// The Selling rules of the server's company, as a Sales Order offers them
		frm.set_query("shipping_rule", "shipping_rule_map", () => ({
			filters: { company: frm.doc.company, shipping_rule_type: "Selling", disabled: 0 },
		}));

		// The progress of a product import, shown on the server's form. One form is used for every server,
		// so the events of the others are left out. The bar stays at its end until the form refreshes.
		frappe.realtime.on("woocommerce_product_import", (data) => {
			if (data.woocommerce_server !== frm.doc.name) return;
			let message = __("{0} of {1} products", [data.done, data.total]);
			if (data.error) {
				message = __("The products couldn't be read, see the Error Log");
			} else if (data.finished) {
				message = __("{0} imported, {1} skipped, {2} failed", [
					data.imported,
					data.skipped,
					data.failed,
				]);
			}
			const percent = data.total ? (data.done / data.total) * 100 : 100;
			frm.dashboard.show_progress(__("Importing Products"), percent, message);
		});
	},

	import_products(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the server first: the import works with its saved settings"));
			return;
		}
		frappe.confirm(
			__(
				"Make an Item for every published product of {0} that has none yet? It runs in the background, and takes a while for a big shop.",
				[frm.doc.name]
			),
			() =>
				frappe.call({
					method: "woocommerce_conduit.tasks.sync_items.start_product_import",
					args: { woocommerce_server: frm.doc.name },
					callback: (r) =>
						frappe.show_alert(
							r.message
								? { message: __("The import has started"), indicator: "green" }
								: {
										message: __(
											"The products of this shop are being imported already"
										),
										indicator: "orange",
								  }
						),
				})
		);
	},

	refresh(frm) {
		// The Item fields a WooCommerce field can be mapped to, from Item's meta (custom fields included),
		// without those the map may not write
		const refused = frm.doc.__onload?.refused_item_fields || [];
		frappe.model.with_doctype("Item", () => {
			const options = frappe.meta
				.get_docfields("Item")
				.filter(
					(df) =>
						!frappe.model.no_value_type.includes(df.fieldtype) &&
						!frappe.model.table_fields.includes(df.fieldtype) &&
						!refused.includes(df.fieldname)
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

		// The Sales Order Item and Customer fields the Order Line Fields and Customer Field maps may write, as
		// the server says
		for (const [table, doctype, fieldnames] of [
			["order_item_field_map", "Sales Order Item", frm.doc.__onload?.order_item_fields],
			["customer_field_map", "Customer", frm.doc.__onload?.customer_fields],
		]) {
			frappe.model.with_doctype(doctype, () => {
				const options = (fieldnames || [])
					.map((fieldname) => frappe.meta.get_docfield(doctype, fieldname))
					.filter(Boolean)
					.sort((a, b) => (a.label || a.fieldname).localeCompare(b.label || b.fieldname))
					.map((df) => `${df.fieldname} | ${df.label || df.fieldname}`)
					.join("\n");
				frm.fields_dict[table].grid.update_docfield_property(
					"erpnext_field_name",
					"options",
					options
				);
			});
		}

		// The transforms the installed apps register, after a blank for none
		const transforms = ["", ...(frm.doc.__onload?.field_transforms || [])].join("\n");
		for (const table of ["item_field_map", "order_item_field_map", "customer_field_map"]) {
			frm.fields_dict[table].grid.update_docfield_property(
				"transform",
				"options",
				transforms
			);
		}
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

	update_linked_items(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(
				__("Save the server first: the review works with its saved Item Field Map")
			);
			return;
		}
		new LinkedItemUpdate(frm).show();
	},

	// Add the methods of the shop's shipping zones that the Shipping Method Map lacks, and name the rows of
	// methods the shop no longer has
	load_shipping_methods: function (frm) {
		add_missing_rows(frm, "get_missing_shipping_method_rows", "shipping_rule_map", {
			added: __("{0} shipping methods added"),
			none: __("No shipping methods missing"),
			unreachable: __("Couldn't reach WooCommerce ({0}), no shipping methods were added"),
			gone: __(
				"These shipping methods are no longer in the shop's zones: {0}. Their rows stay, for orders shipped with them before they were removed."
			),
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
	view_webhook_config: async function (frm) {
		// The secret is made when the server is saved. Before then Frappe would only say it has no password.
		if (frm.is_new() || !frm.doc.secret) {
			frappe.msgprint(__("Save the server first: its webhook secret is made then."));
			return;
		}
		// A Password field: the form only has its mask
		const secret = await frappe.xcall("frappe.client.get_password", {
			doctype: frm.doctype,
			name: frm.doc.name,
			fieldname: "secret",
		});
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
					default:
						"Order created, Order updated, Order deleted and Order restored: a webhook each, all with this URL and secret",
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
					default: secret,
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

// The actions of the Order Status Map that can't be ticked together, which the server refuses as well
// (validate_status_actions): Cancel, Close and Skip are ticked alone. Ticking an action unticks those it
// can't go with.
const STATUS_ACTIONS = [
	"action_submit",
	"action_hold",
	"action_cancel",
	"action_close",
	"action_skip",
];
const SOLE_STATUS_ACTIONS = ["action_cancel", "action_close", "action_skip"];

frappe.ui.form.on(
	"WooCommerce Server Order Status",
	Object.fromEntries(
		STATUS_ACTIONS.map((action) => [
			action,
			(frm, cdt, cdn) => untick_conflicting_actions(cdt, cdn, action),
		])
	)
);

function untick_conflicting_actions(cdt, cdn, action) {
	const row = locals[cdt][cdn];
	if (!row[action]) return;
	const conflicting = SOLE_STATUS_ACTIONS.includes(action)
		? STATUS_ACTIONS
		: SOLE_STATUS_ACTIONS;
	conflicting
		.filter((other) => other !== action && row[other])
		.forEach((other) => frappe.model.set_value(cdt, cdn, other, 0));
}

// Add the rows of a map table that the server says are missing, and say what happened. A map whose
// messages have `gone` also names the rows the server says the shop no longer has.
function add_missing_rows(frm, method, table, messages) {
	frm.call({
		doc: frm.doc,
		method: method,
		callback: function (r) {
			const { rows = [], gone = [], error } = r.message || {};
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
			if (gone.length && messages.gone) {
				frappe.msgprint({
					message: messages.gone.replace(
						"{0}",
						gone.map((row) => frappe.utils.escape_html(row)).join(", ")
					),
					indicator: "orange",
				});
			}
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

// Update Linked Items: what the Item Field Map would change in the Items linked to this shop, found in the
// background (tasks/linked_item_update.py), for a person to tick the Items that take it. Nothing changes
// before Update. A template's row proposes the values all its enabled variants would share, and ticks itself
// when every variant whose values it waits for is ticked.
const LINKED_ITEMS_API = "woocommerce_conduit.tasks.linked_item_update";
const LINKED_ITEM_REVIEW_EVENT = "woocommerce_linked_item_review";
// As many as the server updates in one request
const ITEMS_PER_UPDATE = 50;
// How much of a long value a row shows. The whole value is in its tooltip.
const SHOWN_VALUE_LENGTH = 120;
// The results after which an Item is done with, as it left the review
const DONE_RESULTS = ["updated", "changed", "relinked", "missing"];

class LinkedItemUpdate {
	constructor(frm) {
		this.frm = frm;
		this.dialog = new frappe.ui.Dialog({
			title: __("Update Linked Items"),
			size: "extra-large",
			fields: [
				{
					fieldtype: "MultiCheck",
					fieldname: "rows",
					label: __("Map Rows to Apply"),
					columns: 2,
					options: (frm.doc.item_field_map || []).map((row) => ({
						label: frappe.utils.escape_html(
							`${__(map_row_label(row))} ← ${row.woocommerce_field_name}`
						),
						value: row.name,
						checked: 1,
					})),
				},
				{ fieldtype: "HTML", fieldname: "results" },
			],
		});
		this.results = this.dialog.fields_dict.results.$wrapper;
		this.on_progress = (data) => this.progress(data);
		this.dialog.onhide = () => {
			this.closed = true;
			frappe.realtime.off(LINKED_ITEM_REVIEW_EVENT, this.on_progress);
		};
		this.results.on("change", "input[data-item]", (e) =>
			this.tick([e.currentTarget.dataset.item], e.currentTarget.checked)
		);
		this.results.on("change", "input[data-tick-shown]", (e) =>
			this.tick(this.shown_items(), e.currentTarget.checked)
		);
		this.results.on("input", "input[data-filter]", (e) => this.filter(e.currentTarget.value));
	}

	show() {
		frappe.realtime.on(LINKED_ITEM_REVIEW_EVENT, this.on_progress);
		this.dialog.show();
		this.load();
	}

	// The person's last review of this shop while it is kept, or the progress of one that runs
	load() {
		return frappe
			.xcall(`${LINKED_ITEMS_API}.get_linked_item_review`, {
				woocommerce_server: this.frm.doc.name,
			})
			.then((review) => {
				if (review?.running) {
					this.show_progress();
				} else if (review) {
					this.show_review(review);
				} else {
					this.show_rows();
				}
			});
	}

	show_rows(message = "") {
		this.dialog.set_df_property("rows", "hidden", 0);
		this.results.html(message);
		this.dialog.get_secondary_btn().addClass("hide");
		this.dialog.set_primary_action(__("Find Changes"), () => this.find());
		this.dialog.enable_primary_action();
	}

	find() {
		const rows = this.dialog.get_value("rows") || [];
		if (!rows.length) {
			frappe.msgprint(__("Tick a row of the map to look for changes in"));
			return;
		}
		return frappe
			.xcall(`${LINKED_ITEMS_API}.start_linked_item_review`, {
				woocommerce_server: this.frm.doc.name,
				rows: rows,
			})
			.then((started) => {
				if (!started) {
					frappe.show_alert({
						message: __("Your review of this shop is running already"),
						indicator: "orange",
					});
				}
				this.show_progress();
			});
	}

	progress(data) {
		if (data.woocommerce_server !== this.frm.doc.name || this.closed) return;
		if (data.error) {
			this.show_rows(
				`<p class="text-danger">${__("The review failed, see the Error Log")}</p>`
			);
		} else if (data.finished) {
			this.load();
		} else {
			this.show_progress(data);
		}
	}

	show_progress(data = {}) {
		this.dialog.set_df_property("rows", "hidden", 1);
		this.dialog.get_secondary_btn().addClass("hide");
		this.dialog.disable_primary_action();
		const message = data.total
			? __("{0} of {1} linked Items checked", [data.done, data.total])
			: __("Reading the shop's products...");
		this.results.html(`<p class="text-muted">${message}</p>`);
	}

	show_review(review) {
		// The review whose values the Items take, which a newer one mustn't stand in for
		this.review_id = review.review_id;
		this.entries = Object.fromEntries(review.items.map((entry) => [entry.item_code, entry]));
		this.ticked = new Set();
		this.done = new Set();
		// The templates that follow each variant
		this.followers = {};
		for (const entry of review.items) {
			for (const variant of entry.follows || []) {
				(this.followers[variant] ||= []).push(entry.item_code);
			}
		}

		this.dialog.set_df_property("rows", "hidden", 1);
		this.dialog.set_secondary_action(() => this.show_rows());
		this.dialog.set_secondary_action_label(__("Find Again"));
		this.dialog.get_secondary_btn().prop("disabled", false);
		this.dialog.set_primary_action(__("Update Ticked Items"), () => this.update());
		this.results.html(render_linked_item_review(review));
		this.inputs = {};
		this.results.find("input[data-item]").each((i, input) => {
			this.inputs[input.dataset.item] = input;
		});
		this.update_count();
	}

	// Tick or untick these Items, then have each template whose variants they are follow its variants
	tick(item_codes, checked) {
		const templates = new Set();
		for (const item_code of item_codes) {
			this.set_ticked(item_code, checked);
			(this.followers[item_code] || []).forEach((template) => templates.add(template));
		}
		for (const template of templates) {
			this.set_ticked(
				template,
				this.entries[template].follows.every(
					(variant) => this.ticked.has(variant) || this.done.has(variant)
				)
			);
		}
		this.update_count();
	}

	set_ticked(item_code, checked) {
		const input = this.inputs[item_code];
		if (!input || input.disabled) return;
		input.checked = checked;
		if (checked) {
			this.ticked.add(item_code);
		} else {
			this.ticked.delete(item_code);
		}
	}

	// The Items the filter leaves shown
	shown_items() {
		return Object.keys(this.inputs).filter(
			(item_code) => !$(this.inputs[item_code]).closest("tr").hasClass("hide")
		);
	}

	filter(text) {
		const words = text.trim().toLowerCase();
		this.results.find("tr[data-search]").each((i, row) => {
			$(row).toggleClass("hide", Boolean(words) && !row.dataset.search.includes(words));
		});
	}

	update_count() {
		this.results.find("[data-count]").text(__("{0} ticked", [this.ticked.size]));
		if (this.ticked.size) {
			this.dialog.enable_primary_action();
		} else {
			this.dialog.disable_primary_action();
		}
	}

	// Send the ticked Items, ITEMS_PER_UPDATE a request, and show what happened to each in its row
	async update() {
		// The templates last, so their check sees their variants saved
		const item_codes = [...this.ticked].sort(
			(a, b) => Number(this.entries[a].template) - Number(this.entries[b].template)
		);
		this.dialog.disable_primary_action();
		this.dialog.get_secondary_btn().prop("disabled", true);
		const counts = { updated: 0, left: 0, failed: 0 };
		for (let start = 0; start < item_codes.length && !this.closed; start += ITEMS_PER_UPDATE) {
			this.results
				.find("[data-count]")
				.text(__("Updating {0} of {1} Items...", [start, item_codes.length]));
			let results;
			try {
				results = await frappe.xcall(`${LINKED_ITEMS_API}.update_linked_items`, {
					woocommerce_server: this.frm.doc.name,
					item_codes: item_codes.slice(start, start + ITEMS_PER_UPDATE),
					review_id: this.review_id,
				});
			} catch (e) {
				// The request failed as a whole, which the desk shows, and its Items are as they were
				break;
			}
			for (const [item_code, result] of Object.entries(results)) {
				this.show_result(item_code, result);
				const kind = ["updated", "failed"].includes(result.status)
					? result.status
					: "left";
				counts[kind] += 1;
			}
		}
		this.dialog.get_secondary_btn().prop("disabled", false);
		this.update_count();
		frappe.show_alert(
			{
				message: __("{0} Items updated, {1} left alone, {2} failed", [
					counts.updated,
					counts.left,
					counts.failed,
				]),
				indicator: counts.failed ? "orange" : "green",
			},
			10
		);
	}

	show_result(item_code, result) {
		const input = this.inputs[item_code];
		if (!input) return;
		$(input).closest("tr").find("[data-result]").html(render_linked_item_result(result));
		if (DONE_RESULTS.includes(result.status)) {
			input.checked = false;
			input.disabled = true;
			this.ticked.delete(item_code);
			this.done.add(item_code);
		}
	}
}

// The Select of a map row stores "fieldname | Label"
function map_row_label(row) {
	const [fieldname, label] = (row.erpnext_field_name || "").split(" | ");
	return label || fieldname;
}

function render_linked_item_review(review) {
	const esc = frappe.utils.escape_html;
	const summary = [
		__("Checked {0} Items that take their fields from this shop, for {1}.", [
			review.checked,
			esc(review.rows.join(", ")),
		]),
	];
	if (review.elsewhere) {
		summary.push(
			__("{0} more are linked to it but take their fields from another shop.", [
				review.elsewhere,
			])
		);
	}
	if (review.unread) {
		summary.push(__("{0} products couldn't be read, see the Error Log.", [review.unread]));
	}
	if (review.problems) {
		summary.push(
			__("{0} values were left out, as their fields can't take them: see the Error Log.", [
				review.problems,
			])
		);
	}
	summary.push(__("Found {0}.", [frappe.datetime.prettyDate(review.made)]));
	const found = `<p class="text-muted">${summary.join(" ")}</p>`;
	if (!review.items.length) {
		return `${found}<p class="text-success">${__(
			"Every one of them has the values of the map already"
		)}</p>`;
	}
	return `${found}
		<div class="flex justify-between align-center" style="gap: var(--margin-md); margin-bottom: var(--margin-sm)">
			<input type="text" class="form-control input-xs" data-filter style="max-width: 300px" placeholder="${__(
				"Filter by Item code or name"
			)}">
			<span class="text-muted" data-count></span>
		</div>
		<table class="table table-bordered table-condensed">
			<thead><tr>
				<th style="width: 32px"><input type="checkbox" data-tick-shown title="${__(
					"Tick the Items shown"
				)}"></th>
				<th>${__("Item")}</th><th>${__("Changes")}</th><th style="width: 20%">${__("Result")}</th>
			</tr></thead>
			<tbody>${review.items.map(render_linked_item).join("")}</tbody>
		</table>`;
}

function render_linked_item(entry) {
	const esc = frappe.utils.escape_html;
	const tags = [];
	if (entry.template) {
		tags.push(entry.follows?.length ? __("Template, follows its variants") : __("Template"));
	}
	if (entry.variant_of) tags.push(__("Variant of {0}", [esc(entry.variant_of)]));
	if (entry.disabled) tags.push(__("Disabled"));
	if (entry.edited_since) tags.push(__("edited in ERPNext since the product changed"));
	const changes = entry.changes
		.map((change) => {
			const shared = change.follows
				? ` <span class="text-muted">(${__("all {0} variants", [entry.variants])})</span>`
				: "";
			return `<div><span class="text-muted">${esc(
				__(change.label)
			)}:</span> ${show_linked_value(change.old)} → ${show_linked_value(
				change.new
			)}${shared}</div>`;
		})
		.join("");
	const search = `${entry.item_code} ${entry.item_name || ""}`.toLowerCase();
	return `<tr data-search="${esc(search)}">
		<td><input type="checkbox" data-item="${esc(entry.item_code)}"></td>
		<td${entry.variant_of ? ' style="padding-left: var(--padding-xl)"' : ""}>
			<a href="${frappe.utils.get_form_link("Item", entry.item_code)}" target="_blank">${esc(
		entry.item_code
	)}</a>
			<div class="small">${esc(entry.item_name || "")}</div>
			<div class="small text-muted">${tags.join(" · ")}</div>
		</td>
		<td>${changes}</td>
		<td data-result></td>
	</tr>`;
}

// A value of a review as text, a long one cut with the whole of it in its tooltip
function show_linked_value(value) {
	if (value === null || value === undefined || value === "") {
		return `<span class="text-muted">${__("(empty)")}</span>`;
	}
	const text = typeof value === "object" ? JSON.stringify(value) : String(value);
	const shown =
		text.length > SHOWN_VALUE_LENGTH ? `${text.slice(0, SHOWN_VALUE_LENGTH)}…` : text;
	return `<span title="${frappe.utils.escape_html(text)}">${frappe.utils.escape_html(
		shown
	)}</span>`;
}

function render_linked_item_result(result) {
	const labels = {
		updated: ["green", __("Updated")],
		changed: ["orange", __("Changed since the review, left alone")],
		relinked: ["orange", __("No longer linked to its product, left alone")],
		variants: ["orange", __("Its variants don't all have the values, left alone")],
		missing: ["orange", __("No longer in the review, left alone")],
		failed: ["red", __("Failed")],
	};
	const [color, label] = labels[result.status] || [
		"gray",
		frappe.utils.escape_html(result.status),
	];
	const error = result.error
		? `<div class="small text-muted">${frappe.utils.escape_html(result.error)}</div>`
		: "";
	return `<span class="indicator-pill ${color}">${label}</span>${error}`;
}
