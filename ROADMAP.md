# Roadmap

What is still to be built in woocommerce_conduit, by area. Each entry says why it matters and where to start. When you find something to do later, add it here (see CLAUDE.md), and remove the entry in the commit that does it.

woocommerce_fusion, beside this app in the bench's `apps/`, has a working version of several of these. Port the idea, not the code, as the sync classes differ.

## Orders and accounting

### Refunds from ERPNext

Once a shop is synced, a refund is made in ERPNext, as a Credit Note against the order's Sales Invoice, and sent to the shop. No refund should then be made in WooCommerce. Nothing sends one yet.

- Push: when a return Sales Invoice that bills a synced Sales Order is submitted, POST it to `orders/{id}/refunds`. Run it as a job after commit, under the order's lock, like the status push. Send the `amount`, and the lines found through each item's `so_detail` and the Sales Order Item's `woocommerce_line_item_id`. Send `api_restock: false`: ERPNext owns stock, and its own push sets the shop's stock once the goods are back.
- Money: send `api_refund: false`, so the shop only records the refund and no gateway pays anything out. Nothing books payments on its own, so paying the customer back, through the gateway's panel or by transfer, and the Payment Entry that pays out the Credit Note are left to a person.
- Once only: a retried job must never refund twice. Put the Credit Note's name in the refund's `reason`, and look for it among the order's refunds before sending.
- Coming back: a full refund sets the order to `refunded`, and the pull closes the Sales Order (the `refunded` row of the Order Status Map). A partial one leaves the status as it was. A refund that names no Credit Note was made in the shop, so flag it (`woocommerce_sync_issue`) rather than book it.

Refunds the shop made before ERPNext saw the order are only found when its Sales Order is made. A fully refunded order is skipped, as the `refunded` row doesn't tick Submit. A partly refunded one keeps its status, and WooCommerce's `total` doesn't subtract refunds, so the totals check doesn't notice:

- The Order Import makes no invoice for an order with refunds, and flags it. Its invoice and payment, then a Credit Note for each refund (details at `orders/{id}/refunds`) and the money paid back, would import it whole.
- The sync makes the Sales Order of a partly refunded active order at the full amount. Flag it, so whoever invoices it sees the refund.

Start at: ERPNext's `make_sales_return` (Sales Invoice), `order_status.enqueue_status_push_from_hook` and `SynchroniseSalesOrder.push_status_to_woocommerce` for the shape of a push, `OrderImport.make_invoice` and `make_payment_entry`, and `SynchroniseSalesOrder.create_sales_order`.

### VAT on shipping and fees at WooCommerce's rate

With a template whose VAT is included in the rate, shipping and fees that WooCommerce taxed have their VAT taken out again at the rates of the default Item Tax Template, not at the rate WooCommerce charged. A shop whose shipping takes the rate of the items in the cart (WooCommerce's "Shipping tax class based on cart items") taxes it at 8% on an order of 8% goods, and ERPNext books 23%. The grand total still matches, so the totals check doesn't flag it. Without an inclusive template the VAT is WooCommerce's amount, but all of it goes on the account of the template's first VAT row. The order's `tax_lines` say what WooCommerce charged at each rate (`rate_id`, `shipping_tax_total`), which could pick the VAT rows instead.

Start at: `order_lines.build_charges`, `taxes.get_vat_rates`.

### Imported orders without a payment method

The Order Import skips an order whose payment method has no Mode of Payment in its Payment Method Map, and an order made in wp-admin with no payment method (N/A, an empty `payment_method`) can't have a row, as the map's method is mandatory. They are skipped by every run, and a shop that takes phone orders has many. A row for orders without a method, with its Mode of Payment and Payment Date, would import them. wp-admin's Other (`other`) can be mapped, though Add Missing Payment Methods doesn't add it, as it isn't a gateway: Start Import's count names it with its id.

Start at: `OrderImport.import_wc_order`, `order_import.get_booking_row`, `WooCommerceOrderImport.get_payment_method_row` and `validate_payment_method_map`.

### Shipping methods outside the zones

Add Missing Shipping Methods reads the methods of the shop's shipping zones. Some orders are shipped with a method no zone lists: WooCommerce's block-based local pickup (`pickup_location`), plugin methods, and orders made in the admin or imported from elsewhere. They get a Shipping Rule only through a bare `method_id` row typed by hand. Reading the shipping lines of the latest orders, as Check Order Items does for their products, would find them and could add their rows.

Start at: `item_parity.get_unlinked_order_products`, `WooCommerceServer.fetch_shipping_methods`.

### Shipping Rules for some countries

ERPNext refuses a Shipping Rule whose Valid for Countries leaves out the order's shipping country (`ShippingRule.validate_countries`), so a Sales Order with such a rule isn't made and the order's sync fails. Give the Sales Order the rule only where it applies, and charge the shipping as WooCommerce did otherwise, flagging the order.

Start at: `SynchroniseSalesOrder.create_sales_order`, `WooCommerceServer.get_shipping_rule`.

### The Sales Order's price list

A synced Sales Order has Selling Settings' default price list, which `new_doc` puts on it, so the Price List Rate and the discount a line shows (a free line's 100% too) are against that list rather than the shop's prices. Decided: give it the server's Price List, which the shop's regular prices come from, so a line's discount is the shop's sale or coupon against its regular price. Keep the default when the server has no Price List, or its list is disabled, not for selling, or in another currency than the order. It changes every new Sales Order and the invoices made from them, the Order Import's too. Existing ones keep theirs.

Start at: `SynchroniseSalesOrder.create_sales_order`, `order_lines.build_item_row` (a free line's discount).

## Customers

### Moving an order to another Customer

A Sales Order is flagged when the Customer its account or email found bears another name than the order (`order_customer.py`), as the buyer may be someone else on that email or account. The Customer of a submitted Sales Order can't be changed: it is cancelled and amended with the right one. A cancel made by hand pushes the WooCommerce status the Order Status Map gives Cancelled when the server pushes status changes (`get_status_to_push`), which cancels the order in the shop. A server button, or a flag that keeps the push off while a person amends the Sales Order, would let them move it.

Start at: `order_status.enqueue_status_push_from_hook`, `SalesOrderStatusMixin.amend_sales_order`.

### A parcel for someone other than the buyer

An order shipped to someone else, a gift say, keeps the recipient's name only as the title of its shipping Address, and their phone on it, or the buyer's when the order gave none. The Sales Order's Contact is the buyer, and DHL24 names the receiver after the Customer (`polish_shipping` `get_delivery_name`) and calls the Delivery Note's Contact, so the parcel is addressed to the buyer. A Contact for the recipient, which the Delivery Note could carry, or a receiver's name taken from the shipping Address, would address it to them.

Start at: `SalesOrderCustomerMixin.set_order_addresses`, ERPNext's `make_shipment` (Delivery Note), `polish_shipping/dhl24/provider.py`.

### One Customer across shops, and Customers made by hand

Every Customer lookup searches only the order's shop (`Customer.woocommerce_server`): the tax ID, the account's identifier and the email. So a company or person who buys in two shops, or whom someone made by hand for an offline sale, gets another Customer with its first order there, and a person merges the two in the duplicate review, which compares all Customers already. After a merge a registered account's orders go to the kept Customer (`get_merged_customer`), but a guest's next order in the other shop makes a new one again, as the email lookup doesn't look beyond its shop.

Decided: the order sync matches across shops and with Customers made by hand, by tax ID and by email.

- The tax ID and email lookups search every enabled Customer: the order's shop's first, then the others, made by hand or of another shop, oldest first. A Customer found by email that bears another name than the order still flags the Sales Order.
- One shop field can't say which shops a Customer is in, nor keep an account's username for each. A child table on Customer, a row per shop with its identifier (as `Item WooCommerce Server` is for Items), would replace `woocommerce_server` and `woocommerce_identifier`, with a patch that moves them. A registered account that no row names yet is looked up by its email, like a guest, and its row added. A Customer made by hand gets its shop's row with its first order.
- What reads the two fields follows: `get_merged_customer` and the Merged decision (`record_merge` keeps the merged Customer's shop and identifier), the Customer form's WooCommerce section, and the Duplicate Customers report's shop filter.

Start at: `SalesOrderCustomerMixin` in `order_customer.py` (`find_customer_by_tax_id`, `find_customer_by_email`, `create_customer`), `custom_fields.py`, `tasks/customer_duplicates.py`, `public/js/customer.js`, the `woocommerce_duplicate_customers` report, `patches/v1/set_customer_woocommerce_server.py`.

## Items

### Variations 100 a request

Import Products and Update Linked Items read a shop's products 100 a request, but each variation with a GET of its own (`products/{id}`, as `load_wc_product` reads it), so a shop of many variable products still takes a request a variation. The record of `products/{parent}/variations` isn't the one `products/{id}` gives: it has no `type` (`during_get_list_of_records` sets it), and its name and other fields may differ, which would make the Items or their mapped values differ. Compare the two records of a few variations of the shop, and read the variations 100 a request through their parent once the two agree on what the sync and the Item Field Map use.

Start at: `sync_items.read_products`, `WooCommerceProduct.during_get_list_of_records`, `product_push.load_products`.

### Update Item from WooCommerce shows what changes

The Item form's Update Item from WooCommerce button, and Sync on the WooCommerce Product form, overwrite the Item's name, image, tax template and mapped fields with its primary product's after a confirm that doesn't say what changes, and on a template ERPNext then copies the Item Variant Settings fields to all its variants. Update Linked Items shows each value before it is written, and keeps a template in step with its variants. The buttons could open that review for their one Item.

Start at: `public/js/item.js`, `sync_item_from_woocommerce`, `SynchroniseItem.update_item`, `tasks/linked_item_update.py`.

### A notice of Items without a price

The price push leaves out an Item with no price in the server's Price List today (deleted, past its Valid Upto, or never set), so the shop keeps selling it at the last price ERPNext sent. That stays: WooCommerce can't sell a product with an empty regular price, so a price deleted by mistake would take it off sale at once. But nobody is told. Decided: the daily push sends the System Managers one bell notice per shop naming the linked Items it left out, as `notify_orders_to_import` does for the orders waiting for an import. Work them out once in `run_item_price_sync_in_background`, not in each batch job, with the same conditions as `get_connections` (a disabled Item only with `sync_prices_for_disabled_items`).

Start at: `SynchroniseItemPrice.get_erpnext_item_prices` (the connections without a rate), `run_item_price_sync_in_background`, `order_import.notify_orders_to_import`.

## Configuration

### A credentials check that says what failed

Saving an enabled server with new keys asks the shop for its `system_status`, and any answer but a 200 reads as "WooCommerce API credentials are not valid": a shop that is down (a 5xx, once the retries are spent), a firewall or security plugin that blocks the request (403), or a REST API that is switched off (404). Only a 401 says the keys are wrong. For anything else, say what the shop answered.

Start at: `WooCommerceServer.test_api_credentials`.

### Roles beyond System Manager

Every doctype of the app is for System Manager only. A Sales User doesn't get the Sales Order's WooCommerce Order button, which checks read access, an Item Manager can't open the product from the Item's WooCommerce tab, and neither can pick a WooCommerce Server in a filter. Fusion gives read and write on WooCommerce Order to Sales User and Sales Manager, on WooCommerce Product to Item Manager and the Stock roles, and select on WooCommerce Server to all of them. Mind that writing a WooCommerce Order pushes its status to the shop. Mind also that the list views pass the request's arguments to `get_list_of_records` as they come, so whoever can read a list can set `endpoint`, `extra_wc_fields` and `servers`, and read any endpoint of the shop, its customers say, with the server's keys. Drop those from the desk's arguments before the lists go to other roles.

Start at: the `permissions` of `woocommerce_order.json`, `woocommerce_product.json` and `woocommerce_server.json`.

### WooCommerce Settings' defaults on a new site

Frappe gives a Single its fields' defaults only while nothing of it is stored (`Document.load_from_db`). On a new site, once a server is enabled, the hourly order sync stores its cursor (`wc_last_sync_date_orders`) in WooCommerce Settings, whether or not anyone has saved them. From then on the defaulted fields come up empty: Variation Batch Size and Max Variations, which the product list falls back from in code (20 and 100), and Ignore Email Domains, which leaves the Duplicate Customers report grouping Customers by gmail.com and the other free mail providers until someone fills it in. Storing the defaults on install, or with the cursor while nothing else is stored, would keep them.

Start at: `install.after_install`, `sync_sales_orders.sync_woocommerce_orders_modified_since`, `patches/v1/set_ignore_email_domains.py`.

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

A guide for users rather than developers: how to connect a shop (API keys, the four order webhooks and their secret), Pause Order Sync while the Items are set up, and the maps. Fusion's `docs/` (vitepress) is a model.
