# Roadmap

What is still to be built in woocommerce_conduit, by area. Each entry says why it matters and where to start. When you find something to do later, add it here (see CLAUDE.md), and remove the entry in the commit that does it.

woocommerce_fusion, beside this app in the bench's `apps/`, has a working version of several of these. Port the idea, not the code, as the sync classes differ.

## Orders and accounting

### Refunds from ERPNext

Once a shop is synced, a refund is made in ERPNext, as a Credit Note against the order's Sales Invoice, and sent to the shop. No refund should then be made in WooCommerce. Nothing sends one yet.

- Push: when a return Sales Invoice that bills a synced Sales Order is submitted, POST it to `orders/{id}/refunds`. Run it as a job after commit, under the order's lock, like the status push. Send the `amount`, and the lines found through each item's `so_detail` and the Sales Order Item's `woocommerce_line_item_id`. Send `api_restock: false`: ERPNext owns stock, and its own push sets the shop's stock once the goods are back.
- Money: with `api_refund: true` the gateway pays the customer back. It fails for a method that can't refund, such as cash on delivery or a bank transfer. A tick on each row of the Payment Method Map could say which methods refund through the gateway. The others are only recorded in the shop, and the money goes back by hand. Either way, book a Payment Entry that pays out the Credit Note (ERPNext's `get_payment_entry` on the return).
- Once only: a retried job must never refund twice. Put the Credit Note's name in the refund's `reason`, and look for it among the order's refunds before sending.
- Coming back: a full refund sets the order to `refunded`, and the pull closes the Sales Order (the `refunded` row of the Order Status Map). A partial one leaves the status as it was. A refund that names no Credit Note was made in the shop, so flag it (`woocommerce_sync_issue`) rather than book it.

The one place to pull refunds is an order seen for the first time. A fully refunded order is skipped, as `refunded` isn't one of the `CREATING_ACTIONS`, so it never reaches ERPNext. A partly refunded order is still processing or completed, so it gets its Sales Order, invoice and payment at the full amount. The totals check doesn't notice, as WooCommerce's `total` doesn't subtract refunds. This happens to an order from before the shop was connected, or to one refunded while Pause Order Sync was on. When the Sales Order is created, make a Credit Note for each refund in the order's `refunds` (details at `orders/{id}/refunds`), or at least flag the order.

Start at: ERPNext's `make_sales_return` (Sales Invoice) and `get_payment_entry`, `order_status.enqueue_status_push_from_hook` and `SynchroniseSalesOrder.push_status_to_woocommerce` for the shape of a push, `SynchroniseSalesOrder.create_sales_order`, and `woocommerce_server_payment_method.json`.

### Deleted orders by webhook

`order_created` takes the "created" and "updated" events and answers any other with a 400. A trashed order is only picked up by the hourly sync, through the `trash` row of the status map. Accept "deleted" and "restored" as well. A permanently deleted order then answers 404 when it is loaded, which the sync has to treat as cancelled rather than as an error.

Start at: `endpoint.order_created`, `sync_sales_orders.load_wc_order`.

### Sales Order names from the shop's order number

Sales Orders follow ERPNext's naming series, and the shop's order number goes in `po_no`. Fusion names them by the number through an `override_doctype_class` on Sales Order. Look for a native way first, such as a naming series with a field placeholder or a Document Naming Rule, so it can't clash with other apps that override Sales Order.

Start at: `SynchroniseSalesOrder.create_sales_order`.

### Untaxed shipping and fees

With a template whose VAT is included in the rate, the sync takes VAT out of shipping and every fee at the rates of the default Item Tax Template, even when WooCommerce charged no tax on it. ERPNext then books VAT the shop didn't charge, and less income. The grand total still matches the shop's, so the totals check doesn't flag it. Shipping or a fee with no `shipping_tax` or `total_tax` should be booked as it is.

Start at: `order_lines.build_charges`.

### What counts as paid, per payment method

A mapped method is booked once `date_paid` is set, which WooCommerce does when a payment completes or the order reaches processing or completed. An order that a gateway or plugin marks paid with a status of its own isn't booked until it is completed. Cash on delivery goes the other way: WooCommerce puts the order in processing at checkout, so a mapped COD method is booked as paid before the courier has collected the money. Fusion can ignore the date for a whole server (`ignore_date_paid`), which books pending orders as well. Say per method, or per status of the Order Status Map, what counts as paid instead.

Start at: `SalesOrderPaymentMixin.create_payment_entry`, `woocommerce_server_payment_method.json`.

## Customers

### Duplicate Customers report

Fusion's WooCommerce Duplicate Customers report groups the Customers that look like the same buyer, by email, by name or by company email domain (the big free mail providers left out), with each one's Sales Orders and their total, oldest first, so they can be merged by hand. It changes nothing. Conduit makes a Customer for every guest order, so the report is of use whichever way Guest customers (Open questions) is decided.

Start at: Fusion's `report/woocommerce_duplicate_customers/`. Conduit has no `woocommerce_is_guest` field, but a guest's identifier starts with `Guest-`, and as the identifier of an account is its username, the email is the Customer's `email_id`, from its primary Contact.

### Company customers by email domain

Every account in the shop becomes a Customer of its own, so a B2B shop gets one for each employee who orders. Fusion attaches an order from an email domain listed on the server (`customer_matching_email_domains`) to the oldest Customer of that domain, and leaves that Customer's name and details alone. Only listed domains are matched, as matching every domain would put all users of a free mail provider on one Customer. Conduit would match the domain on the Customer's `email_id`.

Start at: `SynchroniseSalesOrder.create_or_link_customer_and_address`, Fusion's `find_customer_by_email_domain`.

## Items

### Import Products reads each product again

Import Products lists the shop's products 100 a request, then reads each one again with a GET of its own (`load_wc_product` in `run_item_sync`), every field of the doctype. It could read them 100 a request with `WooCommerceDocument.load_records`, as the daily pushes do, and give each to `run_item_sync(woocommerce_product=…)`. Mind that `load_records` leaves out what `after_load_from_db` adds (the dimensions, the indented JSON), and that a variation's record from `products/{parent}/variations` isn't the one `products/{id}` gives, which the import reads now (it has no `type`, for one). So read only the parent products that way, or compare the Items each way makes.

Start at: `sync_items.import_products`, `get_products_to_import`, `WooCommerceDocument.load_records`.

### Big stock transactions push one Item a job

The stock hooks enqueue a job for each Item a transaction moves (`push_pending_stock_updates`), and each job reads its product and writes it when it differs. A Stock Reconciliation of 500 Items makes 500 jobs and up to 1000 requests. A transaction of many Items could enqueue jobs of 100 Items through the batch path instead (`update_stock_levels_of_items`), as the daily push does. The per-Item dedupe (`enqueue_unless_queued`) would then only cover the transactions of one Item.

Start at: `stock_update.push_pending_stock_updates`, `enqueue_stock_updates`.

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
