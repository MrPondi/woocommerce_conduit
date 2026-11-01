# Roadmap

What is still to be built in woocommerce_conduit, by area. Each entry says why it matters and where to start. When you find something to do later, add it here (see CLAUDE.md), and remove the entry in the commit that does it.

woocommerce_fusion, beside this app in the bench's `apps/`, has a working version of several of these. Port the idea, not the code, as the sync classes differ.

## Planned

### Batch pushes

The daily price and stock pushes still go one product at a time: one GET, plus a PUT when something differs. For big catalogues, read 100 products a request and send only the changes to WooCommerce's batch endpoints.

- Read: `products?include=<ids>`, 100 at a time. The API layer turns a `woocommerce_id in` filter into `include`. Read variations through `products/{parent}/variations?include=…`, finding the parent through the Item's `variant_of` (Fusion's `tasks/sync.py:get_variation_parent_woocommerce_id`).
- Compare: with `get_changes`, as now. WooCommerce dates every save as a change and fires its product-updated hooks, so an unchanged product must not be written. Fusion's batch path writes them all; don't copy that part.
- Write: to `products/batch` and `products/{parent}/variations/batch`, at most 100 updates a request (Fusion's `WooCommerceResource.batch_update` and `queue_manager.run_chunks`).
- The daily jobs already hold 100 Items each (`ITEMS_PER_JOB`), the size of one batch.

Start at: `stock_update.update_stock_levels_of_items`, `SynchroniseItemPrice.sync_items_with_woocommerce_products`, `WooCommerceDocument.write`.

## Orders and accounting

### Refunds as credit notes

A refunded order closes its Sales Order (the `refunded` row of the Order Status Map), but no money moves in ERPNext: there is no Credit Note and no refund Payment Entry. Full and partial refunds are in the order's `refunds` field, with details at `orders/{id}/refunds`.

Start at: `order_status.SalesOrderStatusMixin`, ERPNext's `make_sales_return` (Sales Invoice), and `get_payment_entry` for the return.

### Deleted orders by webhook

`order_created` takes the "created" and "updated" events and answers any other with a 400. A trashed order is only picked up by the hourly sync, through the `trash` row of the status map. Accept "deleted" and "restored" as well. A permanently deleted order then answers 404 when it is loaded, which the sync has to treat as cancelled rather than as an error.

Start at: `endpoint.order_created`, `sync_sales_orders.load_wc_order`.

### Sales Order names from the shop's order number

Sales Orders follow ERPNext's naming series, and the shop's order number goes in `po_no`. Fusion names them by the number through an `override_doctype_class` on Sales Order. Look for a native way first, such as a naming series with a field placeholder or a Document Naming Rule, so it can't clash with other apps that override Sales Order.

Start at: `SynchroniseSalesOrder.create_sales_order`.

### Vouchers and untaxed fees

Every fee line is booked on the Fee Account, a negative one too. Germanized for WooCommerce turns a coupon marked as a voucher into a negative fee, so redeeming a voucher lowers the fee income rather than the account its sale was booked on. Fusion books negative fees on an account of their own (`account_for_negative_order_fee_lines`).

With a template whose VAT is included in the rate, the sync also takes VAT out of every fee at the rates of the default Item Tax Template, including a fee that WooCommerce charged no tax on, such as an untaxed voucher, and the VAT of the order drops.

Start at: `order_lines.build_charges`.

### Payment reference and paid date per payment method

A Payment Entry's reference is the order's `transaction_id`, else the title of its payment method. Some gateways keep their id in the order's meta instead: Fusion reads Yoco's `yoco_order_payment_id`. A meta key on each row of the Payment Method Map would cover any gateway. The order's `meta_data` isn't fetched yet.

A mapped method is booked once `date_paid` is set, which WooCommerce does when a payment completes or the order reaches processing or completed. An order that a gateway or plugin marks paid with a status of its own isn't booked until it is completed. Fusion can ignore the date for a whole server (`ignore_date_paid`), which books pending orders as well. Say per method, or per status of the Order Status Map, what counts as paid instead.

Start at: `SalesOrderPaymentMixin.create_payment_entry`, `woocommerce_server_payment_method.json`, and `woocommerce_order.json` for `meta_data`.

## Customers

### Duplicate Customers report

Fusion's WooCommerce Duplicate Customers report groups the Customers that look like the same buyer, by email, by name or by company email domain (the big free mail providers left out), with each one's Sales Orders and their total, oldest first, so they can be merged by hand. It changes nothing. Conduit makes a Customer for every guest order, so the report is of use whichever way Guest customers (Open questions) is decided.

Start at: Fusion's `report/woocommerce_duplicate_customers/`. Conduit has no `woocommerce_is_guest` field, but a guest's identifier starts with `Guest-`, and as the identifier of an account is its username, the email is the Customer's `email_id`, from its primary Contact.

### Company customers by email domain

Every account in the shop becomes a Customer of its own, so a B2B shop gets one for each employee who orders. Fusion attaches an order from an email domain listed on the server (`customer_matching_email_domains`) to the oldest Customer of that domain, and leaves that Customer's name and details alone. Only listed domains are matched, as matching every domain would put all users of a free mail provider on one Customer. Conduit would match the domain on the Customer's `email_id`.

Start at: `SynchroniseSalesOrder.create_or_link_customer_and_address`, Fusion's `find_customer_by_email_domain`.

## Items

### Meta data, categories and tags in the Item Field Map

The Item Field Map reads the product as the doctype has it: `load_from_db` asks WooCommerce for the doctype's fields only (`get_wc_fields`), and `to_dict` returns no others. So a mapping can't reach `meta_data` (a GTIN, a brand, any plugin's field) or `categories` (for the Item Group, say), and finds nothing without saying so. Fusion's WooCommerce Product has `meta_data`, `categories` and `tags` as JSON fields. `validate_item_map` could also refuse a path into a field the doctype doesn't have. The field transforms below build on this.

Start at: `woocommerce_product.json`, `WooCommerceProduct.after_load_from_db`, `SynchroniseItem.set_item_fields`.

### Field transforms and order line field mapping

The Item Field Map copies a JSONPath value as it is. Fusion can transform the value (`tasks/field_transforms.py`, registered through a hook) and also maps order line fields to Sales Order Item fields (`WooCommerce Server Order Item Field`).

Start at: `SynchroniseItem.set_item_fields`, `order_lines.build_item_row`.

### Bulk product import from the form

`sync_woocommerce_products_modified_since` imports a shop's products, but it can only be run by hand (`bench execute`). A button on the server form that enqueues it and reports progress (`frappe.publish_progress`) would make a shop's first import self-service.

Start at: `sync_items.sync_woocommerce_products_modified_since`, `woocommerce_server.js`.

### Prices of a product linked by hand

Check Order Items links a product to an Item (`link_product_to_item`) and leaves the product's prices in the shop alone. The next price push sends the Item's prices instead, and when the server's Sales Price List has no price for the Item it ends a sale that is running in the shop. The sync's own link (`SynchroniseItem.link_item`) first takes the product's prices for the price lists the Item has none in. The button could do the same, which needs the product loaded.

Start at: `item_parity.link_product_to_item`, `SynchroniseItem.link_item`.

### HTML entities in names

WordPress stores term names HTML-escaped, so categories, tags and the options of global attributes come back as `Sleeves &amp; Toploader`, and so do product names saved by a user without the `unfiltered_html` capability. They reach Item names, Item Attribute values and mapped fields like that. Fusion unescapes the names and mapped values it takes from WooCommerce (`unescape_woocommerce_value`).

Start at: `sync_items.to_item_name`, `SynchroniseItem.create_or_update_item_attributes`, `set_item_fields`.

## Configuration

### Webhook secret as a Password field

`WooCommerce Server.secret` is a read-only Small Text, so it is stored in plain text. Make it a Password field, read with `get_password` in `endpoint.validate_request`, and add a patch that moves the existing secrets.

Start at: `woocommerce_server.json`, `endpoint.validate_request`, and `view_webhook_config` in `woocommerce_server.js`, which shows the secret.

### A credentials check that says what failed

Saving an enabled server with new keys asks the shop for its `system_status`, and any answer but a 200 reads as "WooCommerce API credentials are not valid": a shop that is down (a 5xx, once the retries are spent), a firewall or security plugin that blocks the request (403), or a REST API that is switched off (404). Only a 401 says the keys are wrong. For anything else, say what the shop answered.

Start at: `WooCommerceServer.test_api_credentials`.

### Roles beyond System Manager

Every doctype of the app is for System Manager only. A Sales User doesn't get the Sales Order's WooCommerce Order button, which checks read access, an Item Manager can't open the product from the Item's WooCommerce tab, and neither can pick a WooCommerce Server in a filter. Fusion gives read and write on WooCommerce Order to Sales User and Sales Manager, on WooCommerce Product to Item Manager and the Stock roles, and select on WooCommerce Server to all of them. Mind that writing a WooCommerce Order pushes its status to the shop. Mind also that the list views pass the request's arguments to `get_list_of_records` as they come, so whoever can read a list can set `endpoint`, `extra_wc_fields` and `servers`, and read any endpoint of the shop, its customers say, with the server's keys. Drop those from the desk's arguments before the lists go to other roles.

Start at: the `permissions` of `woocommerce_order.json`, `woocommerce_product.json` and `woocommerce_server.json`.

## Desk

### A WooCommerce workspace

The app opens on the WooCommerce Server list. Fusion has a workspace with its servers, orders, products and logs. Conduit's could have the servers and settings, the WooCommerce Orders and Products, the Integration Requests of the WooCommerce service, and a count of the Sales Orders with a `woocommerce_sync_issue`, which wait for a person.

Start at: Fusion's `workspace/woocommerce/woocommerce.json`, `app_home` and `add_to_apps_screen` in `hooks.py`.

### The shop's admin from the forms

Fusion's Sales Order has Open in WooCommerce, which opens the order in wp-admin, and Sync this Order. Conduit's Sales Order gets to the sync through its WooCommerce Order, and no form opens the shop's admin. With HPOS an order's admin page is `admin.php?page=wc-orders&action=edit&id=…` rather than `post.php?post=…&action=edit`.

Start at: `public/js/sales_order.js`, `woocommerce_order.js`, `woocommerce_product.js`.

## Testing and documentation

### Integration tests against a real WooCommerce

The tests mock WooCommerce, so API behaviour is only checked by hand: what a PUT to a variation's id answers, or how filters and `_fields` combine. Fusion's CI runs WordPress Playground with WooCommerce behind Caddy (`wp_woo_blueprint.json`, `wp_woo_caddy`, `.github/workflows/ci.yml`, `tasks/test_integration_helpers.py`).

### Setup and usage documentation

A guide for users rather than developers: how to connect a shop (API keys, the two webhooks and their secret), Pause Order Sync while the Items are set up, and the maps. Fusion's `docs/` (vitepress) is a model.

## Open questions

These need a decision before anything is built.

- **A deleted regular price.** Deleting the server's Price List price for an Item leaves the shop's old price in place, while deleting a sale price ends the sale. Clearing the regular price would make the product unpurchasable, so leaving it may be right.
- **Guest customers.** Each guest order gets its own Customer (`Guest-{order id}`). Fusion merges guests by email and has a Duplicate Customers report. Merging would change how existing guests are found.
- **One customer in several shops.** A Customer is looked up within its shop (`Customer.woocommerce_server`), so a person who buys in two shops gets a Customer in each. Matching across shops by email, as Fusion does, makes them one, but then the Customer's name and addresses follow whichever shop it last ordered in, and one shop field can't say which shops it is in.
- **The Sales Order's price list.** A synced Sales Order has Selling Settings' default price list, which `new_doc` puts on it, so the discount a line shows (a free line's 100% too) is against that list. Fusion uses the customer's default price list when it is in the order's currency (`get_customer_selling_price_list`). The server's Price List, which the shop's prices come from, would show each line's discount against the shop's regular price. Either changes every new Sales Order, and a list in another currency or not enabled for selling would have to be passed over.
