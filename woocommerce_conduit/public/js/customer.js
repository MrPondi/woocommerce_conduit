// Copyright (c) 2026, Karol Parzonka and contributors
// For license information, please see license.txt

// The Customers that may be the same buyer as this one, which a person merges into one or tells apart. The
// server finds them and keeps what was decided (tasks/customer_duplicates.py).
const DUPLICATES_API = "woocommerce_conduit.tasks.customer_duplicates";

frappe.ui.form.on("Customer", {
	refresh(frm) {
		const field = frm.get_field("woocommerce_duplicates");
		if (!field) return;
		// The field shows only when there are some (its depends_on)
		const duplicates = frm.doc.__onload?.woocommerce_duplicates || [];
		if (!duplicates.length) {
			field.$wrapper.empty();
			return;
		}

		field.$wrapper.html(render_duplicates(duplicates));
		field.$wrapper
			.find("button[data-customer]")
			.on("click", (event) =>
				review_duplicate(frm, $(event.currentTarget).attr("data-customer"))
			);
	},
});

function describe_reasons(reasons) {
	const labels = {
		email: __("same email"),
		phone: __("same phone"),
		tax_id: __("same tax ID"),
	};
	return reasons.map((reason) => labels[reason] || reason).join(", ");
}

function render_duplicates(duplicates) {
	const rows = duplicates
		.map(
			(row) => `
				<div class="flex justify-between align-center" style="gap: var(--margin-md)">
					<div>
						${frappe.utils.get_form_link(
							"Customer",
							row.customer,
							true,
							frappe.utils.escape_html(row.customer_name)
						)}
						<span class="text-muted">· ${frappe.utils.escape_html(describe_reasons(row.reasons))}</span>
					</div>
					<button class="btn btn-xs btn-default" data-customer="${frappe.utils.escape_html(
						row.customer
					)}">${__("Review")}</button>
				</div>`
		)
		.join("");
	return `
		<div class="form-message yellow">
			<div><strong>${__("This Customer may be the same buyer as:")}</strong></div>
			${rows}
		</div>`;
}

function review_duplicate(frm, other_customer) {
	frappe.call({
		method: `${DUPLICATES_API}.get_duplicate_review`,
		args: { customer: frm.doc.name, other_customer: other_customer },
		callback: (r) => show_review(frm, r.message),
	});
}

function show_review(frm, review) {
	const [first, second] = review.customers;
	const dialog = new frappe.ui.Dialog({
		title: __("Is this the same buyer?"),
		size: "large",
		fields: [
			{ fieldtype: "HTML", fieldname: "comparison" },
			{
				fieldtype: "Select",
				fieldname: "keep",
				label: __("Customer to Keep"),
				options: review.customers.map((customer) => ({
					value: customer.name,
					label: `${customer.customer_name} (${customer.name})`,
				})),
				default: review.keep,
				description: __(
					"A merge moves the other Customer's documents, Addresses and Contacts to this one, and removes the other one."
				),
			},
		],
		primary_action_label: __("Merge"),
		primary_action: ({ keep }) => {
			const removed = keep === first.name ? second.name : first.name;
			frappe.confirm(
				__("Merge {0} into {1}? This cannot be undone.", [
					frappe.utils.bold(removed),
					frappe.utils.bold(keep),
				]),
				() => {
					dialog.hide();
					merge_customers(frm, removed, keep);
				}
			);
		},
		secondary_action_label: __("Not the Same"),
		secondary_action: () =>
			frappe.call({
				method: `${DUPLICATES_API}.mark_customers_different`,
				args: { customer: first.name, other_customer: second.name },
				callback: () => {
					dialog.hide();
					frm.reload_doc();
				},
			}),
	});
	dialog.fields_dict.comparison.$wrapper.html(render_comparison(review));
	dialog.show();
}

function merge_customers(frm, customer, into) {
	frappe.call({
		method: `${DUPLICATES_API}.merge_customers`,
		args: { customer: customer, into: into },
		freeze: true,
		freeze_message: __("Merging Customers..."),
		callback: (r) => {
			if (r.message === frm.doc.name) {
				frm.reload_doc();
				return;
			}
			// This Customer is gone, its documents are the other one's now
			frappe.model.remove_from_locals("Customer", frm.doc.name);
			frappe.set_route("Form", "Customer", r.message);
		},
	});
}

function render_comparison(review) {
	const escape = (value) => frappe.utils.escape_html(cstr(value));
	const list = (values) => (values || []).map(escape).join("<br>");
	const rows = [
		[
			__("Customer"),
			(customer) => frappe.utils.get_form_link("Customer", customer.name, true),
		],
		[__("Customer Name"), (customer) => escape(customer.customer_name)],
		[__("Customer Type"), (customer) => escape(__(customer.customer_type))],
		[__("Tax ID"), (customer) => escape(customer.tax_id)],
		[
			__("WooCommerce"),
			(customer) =>
				escape(
					[customer.woocommerce_server, customer.woocommerce_identifier]
						.filter(Boolean)
						.join(" · ")
				),
		],
		[__("Emails"), (customer) => list(customer.emails)],
		[__("Phones"), (customer) => list(customer.phones)],
		[__("Addresses"), (customer) => list(customer.addresses)],
		[
			__("Sales Orders"),
			(customer) => `${cint(customer.orders)} · ${format_currency(customer.ordered)}`,
		],
		[__("Created"), (customer) => escape(frappe.datetime.str_to_user(customer.creation))],
	];
	const warning = review.tax_ids_differ
		? `<div class="form-message red">${__(
				"Their tax IDs differ, so they are most likely two companies."
		  )}</div>`
		: "";
	return `
		<p>${__("Matched by: {0}", [escape(describe_reasons(review.reasons))])}</p>
		${warning}
		<table class="table table-bordered">
			<tbody>
				${rows
					.map(
						([label, value]) =>
							`<tr><th>${label}</th>${review.customers
								.map((customer) => `<td>${value(customer)}</td>`)
								.join("")}</tr>`
					)
					.join("")}
			</tbody>
		</table>`;
}
