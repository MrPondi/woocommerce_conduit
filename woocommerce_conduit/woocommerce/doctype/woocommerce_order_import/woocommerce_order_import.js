// Copyright (c) 2026, Karol Parzonka and contributors
// For license information, please see license.txt

frappe.ui.form.on("WooCommerce Order Import", {
	setup(frm) {
		frm.set_query("mode_of_payment", "payment_method_map", () => ({
			filters: { enabled: 1 },
		}));
		// The company's own bank accounts. The server checks that each is on its Mode of Payment's account.
		frm.set_query("bank_account", "payment_method_map", () => ({
			filters: { company: frm.doc.company, is_company_account: 1, disabled: 0 },
		}));

		// The progress of the import, which only the viewers of its form get
		frappe.realtime.on("woocommerce_order_import", (data) => {
			if (data.order_import !== frm.doc.name) return;
			if (data.finished) {
				frm.dashboard.hide_progress();
				frm.reload_doc();
				return;
			}
			const percent = data.total ? (data.done / data.total) * 100 : 100;
			frm.dashboard.show_progress(
				__("Importing Orders"),
				percent,
				__("{0} of {1} orders", [data.done, data.total])
			);
		});
	},

	refresh(frm) {
		frm.set_intro();
		if (frm.is_new()) return;
		if (frm.doc.__onload && frm.doc.__onload.running) {
			frm.set_intro(__("An import of this shop is running."), "blue");
			return;
		}
		frm.add_custom_button(__("Start Import"), () => frm.trigger("start_import")).addClass(
			"btn-primary"
		);
	},

	start_import(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the import first: it runs with its saved settings"));
			return;
		}
		const server = frappe.utils.escape_html(frm.doc.woocommerce_server);
		// The orders it would take, read from the shop as the import reads them, so whoever starts it sees
		// beforehand which it would skip
		frappe.call({
			method: "woocommerce_conduit.tasks.order_import.count_orders_to_import",
			args: { order_import: frm.doc.name },
			freeze: true,
			freeze_message: __("Counting the orders of {0}…", [server]),
			callback: (r) => {
				const { total, skipped = [], error } = r.message || {};
				if (!error && !total) {
					frappe.msgprint(
						__("{0} has no completed orders without a Sales Order in this range", [
							server,
						])
					);
					return;
				}
				let message = error
					? __("Import the completed orders of {0} that have no Sales Order yet?", [
							server,
					  ]) +
					  " " +
					  __("They couldn't be counted ({0}).", [frappe.utils.escape_html(error)])
					: __("Import the {0} completed orders of {1} that have no Sales Order yet?", [
							total,
							server,
					  ]);
				message +=
					" " +
					__(
						"Each gets its Sales Invoice and Payment Entry, submitted, when its payment method has a Mode of Payment."
					);
				message += error ? describe_unmapped_rows(frm) : describe_skipped(skipped);
				frappe.confirm(message, () => frm.trigger("run_import"));
			},
		});
	},

	run_import(frm) {
		frappe.call({
			method: "woocommerce_conduit.tasks.order_import.start_order_import",
			args: { order_import: frm.doc.name },
			callback: (r) => {
				frappe.show_alert(
					r.message
						? { message: __("The import has started"), indicator: "green" }
						: {
								message: __("An import of this shop is running already"),
								indicator: "orange",
						  }
				);
				frm.reload_doc();
			},
		});
	},

	load_payment_methods(frm) {
		frm.call({
			doc: frm.doc,
			method: "get_missing_payment_method_rows",
			callback: (r) => {
				const { rows = [], error } = r.message || {};
				rows.forEach((row) => frm.add_child("payment_method_map", row));
				frm.refresh_field("payment_method_map");
				if (error) {
					frappe.show_alert(
						{
							message: __(
								"Couldn't reach WooCommerce ({0}), no payment methods were added",
								[frappe.utils.escape_html(error)]
							),
							indicator: "orange",
						},
						10
					);
					return;
				}
				frappe.show_alert({
					message: rows.length
						? __("{0} payment methods added", [rows.length])
						: __("No payment methods missing"),
					indicator: rows.length ? "green" : "blue",
				});
			},
		});
	},
});

function describe_skipped(skipped) {
	// The payment methods without a Mode of Payment that orders were paid with, those not in the map
	// included, with their ids, which a row of the map needs
	if (!skipped.length) return "";
	const count = skipped.reduce((sum, row) => sum + row.orders, 0);
	const methods = skipped.map((row) => {
		const method = row.payment_method
			? `${frappe.utils.escape_html(
					row.title || row.payment_method
			  )} (${frappe.utils.escape_html(row.payment_method)})`
			: __("no payment method");
		return `${method}: ${row.orders}`;
	});
	return (
		"<br><br>" +
		__("{0} of them would be skipped, as their payment method has no Mode of Payment:", [
			count,
		]) +
		"<br>" +
		methods.join("<br>")
	);
}

function describe_unmapped_rows(frm) {
	// Without a count, at least the rows of the map that skip their orders
	const unmapped = (frm.doc.payment_method_map || [])
		.filter((row) => !row.mode_of_payment)
		.map((row) =>
			frappe.utils.escape_html(
				row.woocommerce_payment_method_title || row.woocommerce_payment_method
			)
		);
	if (!unmapped.length) return "";
	return (
		"<br><br>" +
		__("These have none, so the orders paid with them are skipped: {0}", [unmapped.join(", ")])
	);
}
