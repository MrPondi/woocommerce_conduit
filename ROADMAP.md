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

### Shipment tracking

Fusion reads and writes the tracking numbers of the Advanced Shipment Tracking plugin (`orders/{id}/shipment-trackings`) from the Sales Order. Conduit has nothing for tracking.

Start at: Fusion's `woocommerce_order.py` (`get_additional_order_attributes`, `update_shipment_tracking`).

## Items

### Field transforms and order line field mapping

The Item Field Map copies a JSONPath value as it is. Fusion can transform the value (`tasks/field_transforms.py`, registered through a hook) and also maps order line fields to Sales Order Item fields (`WooCommerce Server Order Item Field`).

Start at: `SynchroniseItem.set_item_fields`, `order_lines.build_item_row`.

### Bulk product import from the form

`sync_woocommerce_products_modified_since` imports a shop's products, but it can only be run by hand (`bench execute`). A button on the server form that enqueues it and reports progress (`frappe.publish_progress`) would make a shop's first import self-service.

Start at: `sync_items.sync_woocommerce_products_modified_since`, `woocommerce_server.js`.

## API and lists

### Retries on rate limits and server errors

A 429 or 5xx from the shop fails the request at once, and the sync job with it. Retry with backoff in the client, honouring `Retry-After`.

Start at: `WooCommerceAPI._API__request` in `woocommerce/woocommerce_api.py`.

### Sorted lists across several shops

With more than one shop, a list page holds one shop's records and then the next's, sorted within the page only. A true merge needs `start + page_length` records from each shop, merged and then cut to the page.

Start at: `WooCommerceDocument.get_list_of_records`.

## Configuration

### Webhook secret as a Password field

`WooCommerce Server.secret` is a read-only Small Text, so it is stored in plain text. Make it a Password field, read with `get_password` in `endpoint.validate_request`, and add a patch that moves the existing secrets.

Start at: `woocommerce_server.json`, `endpoint.validate_request`, and `view_webhook_config` in `woocommerce_server.js`, which shows the secret.

## Tooling

### Keep the lint tools current

The hooks in `.pre-commit-config.yaml` are pinned, as pre-commit pins every hook to a tag. Frappe and ERPNext don't hold any of them back: each app has its own config, and core is further behind. Frappe and ERPNext run prettier 2.7.1 and eslint 8.44.0, and ERPNext runs ruff 0.2.0. This app's old versions came from `bench new-app`'s template, which in v16 still writes prettier 2.7.1 and eslint 8.44.0.

- **ruff, pre-commit-hooks, eslint**: `pre-commit autoupdate` bumps them. pre-commit.ci does it weekly once it is enabled for the repo, as the config's `ci:` block asks. A ruff bump can bring new rules and formatting to fix, as 0.17's UP037 did.
- **prettier**: `mirrors-prettier` is archived at 3.1.0, so autoupdate can't move it. The hook installs `prettier@3.6.2` through `additional_dependencies`, which only a person bumps (the latest is 3.9). For automatic bumps, move prettier and eslint into a `package.json` as devDependencies, kept current by Dependabot, and run them from `repo: local` hooks.
- **Node**: pinned to 24.22.2. pre-commit installs its own Node, because it ignores the one nvm puts under the home directory, and nodeenv 1.11.0 takes the prerelease that nodejs.org now lists first (v27.0.0-alpha) as the latest version, then fails to parse it. `language_version: lts` follows the newest LTS and avoids the bug. nodeenv 1.12.0 fixes it, and the pin can go once everyone has it.
- **eslint 9 and 10** don't read `.eslintrc`. Moving past 8 needs the config rewritten as a flat `eslint.config.mjs`, with the globals Frappe's JS uses.

Start at: `.pre-commit-config.yaml`, `.eslintrc`.

## Testing and documentation

### Integration tests against a real WooCommerce

The tests mock WooCommerce, so API behaviour is only checked by hand: what a PUT to a variation's id answers, or how filters and `_fields` combine. Fusion's CI runs WordPress Playground with WooCommerce behind Caddy (`wp_woo_blueprint.json`, `wp_woo_caddy`, `.github/workflows/ci.yml`, `tasks/test_integration_helpers.py`).

### Setup and usage documentation

A guide for users rather than developers: how to connect a shop (API keys, the two webhooks and their secret), Pause Order Sync while the Items are set up, and the maps. Fusion's `docs/` (vitepress) is a model.

## Open questions

These need a decision before anything is built.

- **A deleted regular price.** Deleting the server's Price List price for an Item leaves the shop's old price in place, while deleting a sale price ends the sale. Clearing the regular price would make the product unpurchasable, so leaving it may be right.
- **Guest customers.** Each guest order gets its own Customer (`Guest-{order id}`). Fusion merges guests by email and has a Duplicate Customers report. Merging would change how existing guests are found.
