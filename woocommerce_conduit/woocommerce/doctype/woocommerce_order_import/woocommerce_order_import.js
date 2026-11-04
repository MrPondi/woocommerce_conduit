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
		let message = __(
			"Import the completed orders of {0} that have no Sales Order yet? Each gets its Sales Invoice and Payment Entry, submitted, when its payment method has a Mode of Payment.",
			[frappe.utils.escape_html(frm.doc.woocommerce_server)]
		);
		const unmapped = (frm.doc.payment_method_map || [])
			.filter((row) => !row.mode_of_payment)
			.map((row) =>
				frappe.utils.escape_html(
					row.woocommerce_payment_method_title || row.woocommerce_payment_method
				)
			);
		if (unmapped.length) {
			message +=
				"<br><br>" +
				__("These have none, so the orders paid with them are skipped: {0}", [
					unmapped.join(", "),
				]);
		}
		frappe.confirm(message, () =>
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
			})
		);
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
