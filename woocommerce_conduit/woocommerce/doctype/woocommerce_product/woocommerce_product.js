// Copyright (c) 2025, Karol Parzonka and contributors
// For license information, please see license.txt

// Cloudflare hotlink protection on the shop blocks image requests carrying a foreign Referer.
// Send none while a WooCommerce Product form is open (covers gallery thumbnails and lightbox)
// and restore the default as soon as the route leaves it.
const REFERRER_META_ID = "woocommerce-product-no-referrer";

function set_no_referrer(enabled) {
	const existing = document.getElementById(REFERRER_META_ID);
	if (enabled && !existing) {
		const meta = document.createElement("meta");
		meta.id = REFERRER_META_ID;
		meta.name = "referrer";
		meta.content = "no-referrer";
		document.head.appendChild(meta);
	} else if (!enabled && existing) {
		existing.remove();
	}
}

frappe.router.on("change", () => {
	const route = frappe.get_route();
	set_no_referrer(route[0] === "Form" && route[1] === "WooCommerce Product");
});

frappe.ui.form.on("WooCommerce Product", {
	refresh(frm) {
		set_no_referrer(true);
		frm.trigger("set_image_gallery");

		// Pull this product into its ERPNext Item, making the Item if there is none
		frm.add_custom_button(
			__("Sync this Item to ERPNext"),
			function () {
				frm.trigger("sync_product");
			},
			__("Actions")
		);
	},
	set_image_gallery(frm) {
		// The doctype is virtual, so there are no File records. Feed the Attachment Gallery
		// with the WooCommerce image URLs (all of them, including the main image).
		const docinfo = frm.get_docinfo();
		if (!docinfo) return;

		let images = frm.doc.images || [];
		if (typeof images === "string") {
			try {
				images = JSON.parse(images);
			} catch (e) {
				images = [];
			}
		}

		// Zero-padded index keeps the gallery's file_name sort in WooCommerce order
		docinfo.attachments = images
			.filter((image) => image && image.src)
			.map((image, i) => {
				const basename = decodeURIComponent(image.src.split("?")[0].split("/").pop());
				// Frappe's is_image_file() doesn't recognise webp/avif, so the gallery would render
				// them as plain file links. A trailing fragment is ignored by the browser but
				// satisfies the extension check.
				const needs_hint = /\.(webp|avif)(\?.*)?$/i.test(image.src);
				return {
					name: `${frm.docname}-image-${i + 1}`,
					file_name: `${String(i + 1).padStart(2, "0")} - ${image.name || basename}`,
					file_url: needs_hint ? `${image.src.replace(/\?.*$/, "")}#.jpg` : image.src,
					is_private: 0,
					attached_to_doctype: frm.doctype,
					attached_to_name: frm.docname,
				};
			});

		$(frm.wrapper).trigger("attachments_change");
	},
	sync_product: function (frm) {
		// A failure is shown by frappe.call itself
		frappe.call({
			method: "woocommerce_conduit.tasks.sync_items.sync_woocommerce_product",
			args: {
				woocommerce_product_name: frm.doc.name,
			},
			freeze: true,
			freeze_message: __("Sync Product with ERPNext..."),
			callback: function () {
				frappe.show_alert(
					{
						message: __("Sync completed successfully"),
						indicator: "green",
					},
					5
				);
				frm.reload_doc();
			},
		});
	},
});
