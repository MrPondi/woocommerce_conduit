# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Frappe/ERPNext app (`woocommerce_conduit`, requires `erpnext`) that syncs WooCommerce items, item prices and orders with ERPNext. It is a slimmer rewrite related to the `woocommerce_fusion` app that sits beside it in `apps/`. Fusion has features Conduit does not: batch/queue API, field transforms, two-way Item sync. Conduit's stock sync (`tasks/stock_update.py`) and Item form buttons (`public/js/item.js`) are ported from Fusion. Do not assume Fusion code can be copied as-is, because the sync classes differ.

The repo root is the app dir; the Python package is the nested `woocommerce_conduit/` folder. The Frappe module is `woocommerce_conduit/woocommerce/`, which holds all doctypes.

## Commands

Run from the bench root (`/workspace/development/frappe-bench`), not the app dir. The site used for development is `conduit.localhost`.

```bash
bench --site conduit.localhost migrate                       # after doctype JSON / patch changes
bench --site conduit.localhost clear-cache
bench --site conduit.localhost set-config allow_tests true   # once, needed for tests
bench --site conduit.localhost run-tests --app woocommerce_conduit
bench --site conduit.localhost run-tests --module woocommerce_conduit.woocommerce.doctype.woocommerce_product.test_woocommerce_product
bench --site conduit.localhost run-tests --doctype "WooCommerce Product"
```

Lint/format (run in the app dir): `pre-commit run --all-files`. It uses ruff (lint, import sort, format), eslint, prettier and pyupgrade.

- Python style: **tabs** for indentation, double quotes, line length 110, target py310 (see `[tool.ruff]` in `pyproject.toml`).
- CI (`.github/workflows/ci.yml`) installs the app on a fresh bench and runs `run-tests --app woocommerce_conduit`. `linter.yml` runs Frappe Semgrep rules and pip-audit.

## Architecture

### Virtual doctypes backed by the WooCommerce REST API

`WooCommerce Product` and `WooCommerce Order` are **virtual doctypes**. They have no DB tables. They subclass `WooCommerceDocument` in [woocommerce/woocommerce_api.py](woocommerce_conduit/woocommerce/woocommerce_api.py), which implements `load_from_db`, `get_list_of_records`, `get_count_of_records` and `db_update`. `db_insert`/`delete` are no-ops.

- `db_update` PUTs only the fields that differ from `_doc_before_save` (which `save()` re-fetches from WooCommerce), filtered and shaped by the subclass's `before_db_update`, and stores the returned `date_modified`. `WooCommerce Product` only ever writes the price and stock fields in `writable_fields`. `WooCommerce Order` only writes `status`. Frappe's length/select validators are disabled on these doctypes, because WooCommerce decides what is valid.
- A record's `name` is `"{server_domain}~{wc_id}"` (`WC_RESOURCE_DELIMITER = "~"`). Use `generate_woocommerce_record_name_from_domain_and_id` and `get_domain_and_id_from_woocommerce_record_name` rather than building or parsing it by hand.
- `WooCommerceDocument.__init__` builds one `WooCommerceAPI` client per **enabled** `WooCommerce Server` and throws `SyncDisabledError` if none exist. Queries fan out across all of those servers.
- Each subclass declares `resource`, `field_setter_map`, `field_getter_map` (Frappe fieldname → WC fields it needs), `orderby_map` and `mandatory_wc_fields`. Only the WC fields needed for the requested Frappe fields are requested (`_fields`). When you add a field to a virtual doctype, update these maps so it is actually fetched.
- `WooCommerceAPI` wraps the `woocommerce` client and enqueues a `WooCommerce Request Log` entry for every request (skipped under `frappe.flags.in_test`).
- Complex values (dicts/lists) are JSON-serialised into JSON fields. `after_load_from_db` on each subclass flattens or prettifies them for the form.
- `WooCommerce Product.get_list` also fetches variations. Its behaviour (variation fetching, batch size, max variations) is driven by `WooCommerce Settings`. Lists are never cached, so they show what WooCommerce has now.

### Sync layer (`tasks/`)

- Each sync is a class of its own (`SynchroniseItem`, `SynchroniseSalesOrder`, `SynchroniseItemPrice`), with no shared base class. Each loads the servers it needs with `get_cached_doc`. `tasks/sync.py` has `record_sync_time`.
- `sync_items.py` has `SynchroniseItem`, which syncs WC products **into** ERPNext Items, one way. A product is pulled **once**: a missing Item is created (with its regular and sale price as Item Prices) when an order needs it or on a manual sync, and a linked Item is only updated again with `force_update` (the Sync buttons). One Item can be linked to several shops, and each shop keeps its own name, description, etc., so Item fields are never written back to WooCommerce. Only prices (`sync_item_prices.py`) and stock (`stock_update.py`) go from ERPNext to WooCommerce, each switched per `WooCommerce Server` (`enable_price_list_sync`, `enable_stock_level_synchronisation`) and per Item connection (`Item WooCommerce Server.enable_sync`, "Sync Prices and Stock", default on). Stock is the sum of the server's `warehouses`. ERPNext has no notion of supplier stock, so backorders and the like are left to other apps: they register a method under the `woocommerce_stock_values` hook (see `apply_stock_value_hooks` and the comment in `hooks.py`) and call `enqueue_stock_update(item_code)` to push again. Keep this app free of company-specific stock rules. Only the **primary** connection (`Item WooCommerce Server.is_primary`, the first row when none is marked, see `get_primary_connection`) updates an existing Item. The other shops only record their sync hash. An Item `validate` hook allows one primary and marks the first row when none is. `sync_sales_orders.py` and `sync_item_prices.py` follow the same pattern.
- Change detection uses a **sync hash**: `Item WooCommerce Server.woocommerce_last_sync_hash` stores the WC `date_modified` from the last sync. Changing how it is written affects loop prevention. It is a Datetime field, so compare it with `get_datetime` on both sides. A product is only pulled when its `date_modified_gmt` (converted to the system timezone) is newer than `Item.modified`, so edits made in ERPNext since aren't overwritten.
- `sync_sales_orders.py` has `SynchroniseSalesOrder`, which pulls WC orders into Sales Orders, built from mixins: `order_status.py` (what a status does, the status push), `order_payment.py` (Sales Invoice and Payment Entry) and `order_lines.py` (lines, taxes and charges).
  - The server's **Order Status Map** (`WooCommerce Server Order Status`, one row per WC status slug) decides what an order means: its `erpnext_action` (skip, submit, submit + invoice, submit + hold, submit + invoice + hold, complete, cancel, close, status only), and which Sales Order status is pushed as that WC status. Defaults are in `DEFAULT_STATUSES`. An action means one thing for an order seen for the first time and another for one that has a Sales Order:
    - Only `CREATING_ACTIONS` create a Sales Order, and with it a Customer, Address and Contact. The others act on an existing Sales Order only, so a first-seen failed, cancelled or refunded order stays out of ERPNext.
    - Complete SO makes whatever a completed order lacks of its Sales Order, Sales Invoice, Payment Entry and Delivery Note, also when it was completed in WC by mistake, so ERPNext doesn't drift from the shop.
    - Submit SO + Invoice + Hold invoices (and books the payment) before holding, as ERPNext won't invoice a held order. `invoice_and_hold` runs once, when the status is applied, so a hold lifted in ERPNext isn't put back.
    - On an existing Sales Order the hold, resume, cancel and close of an action run once, when the WC status changes. The invoice, payment and delivery steps are idempotent and run on every sync, so pending makes the SI and processing then only adds the PE.
  - **Taxes** are ERPNext's (`tasks/taxes.py`): the server's Sales Taxes and Charges Template taxes every Sales Order, at the rates of each line's Item Tax Template. Its "Is this Tax included in Basic Rate?" tick says whether the rates are the shop's gross prices (ERPNext takes the VAT out, per line) or net; the sync only reads it (`prices_include_tax`) and never converts prices. Items get the template of their product's tax class from the server's Tax Class Map, else `default_item_tax_template`, when created or force-synced. ERPNext can't include tax in an Actual charge, so with an inclusive template the sync splits the VAT out of shipping and fees itself. Those Actual rows carry `set_by_item_tax_template`, as ERPNext reads a line's VAT rate from the last row on each account.
  - The **Payment Method Map** (`WooCommerce Server Payment Method`) maps a WC gateway id to a Mode of Payment, whose default account for the company is the Payment Entry's account. A method without a row or without a Mode of Payment is never booked; a mapped one is booked once `date_paid` is set.
  - **Pause Order Sync** (`WooCommerce Server.pause_order_sync`, on for a new server) keeps a shop's orders out while its Items are set up: `enqueue_sales_order_sync` and `run_locked` drop them. Unpausing enqueues `sync_paused_orders` for the orders changed since `order_sync_paused_since`. Without that date nothing is enqueued, as it would be the whole shop. The pause doesn't empty the queue: jobs already queued end at once in `run_locked`. Product, price and stock syncs aren't paused.
  - `tasks/item_parity.py` backs the server's **Check Order Items** button: the products of recent orders that no Item is linked to, with an Item suggested by SKU, to link (`link_product_to_item`) or create.
  - `Sales Order.woocommerce_last_sync_hash` is the WC `date_modified` seen at the last pull **or** push. WC is pulled when its `date_modified` is newer (`wc_order_changed`). A push re-reads the order first and pulls instead when WC changed meanwhile, so a payment is never reverted. Never compare with `Sales Order.modified`.
  - Everything for one WC order runs under a file lock (`get_lock_name`), and jobs are enqueued by name with `job_id` + `deduplicate`.
  - Steps the Sales Order doesn't depend on (lines update, invoice, payment) run through `run_flagged`: a failure rolls back to a savepoint and is written to `Sales Order.woocommerce_sync_issue` instead of failing the sync. Totals that differ from WC are flagged too, which blocks auto-invoicing.
- Documents created or updated by the sync set `doc.flags.created_by_sync` so the doc-event hooks don't re-trigger a sync. Keep that guard on any new hook.
- The sync entry points are:
  - `doc_events` in [hooks.py](woocommerce_conduit/hooks.py): `Sales Order` on_submit (order sync) and on_change (status push, which also sees the status changes ERPNext makes with `db_set`), `Sales Invoice`/`Payment Entry` on_cancel (unlink from the Sales Order, which would block the cancel), `Item Price` on_update (price push), and submit/cancel of stock-moving documents (stock push). Each enqueues a job after commit. There is no Item hook, as Item changes don't go to WooCommerce. There is no queue or batching layer.
  - Jobs get names (`item_code`, `woocommerce_product_name`), never documents, and load the product themselves.
  - Scheduler: `hourly_long` (orders modified since the last sync; the cursor is stored in `WooCommerce Settings`; products are pulled through the orders) and `daily_long` (full price and stock push, as a safety net). `sync_woocommerce_products_modified_since` is no longer scheduled; run it by hand for a bulk import.
  - Webhook: `order_created` in [endpoint.py](woocommerce_conduit/endpoint.py) takes both the "Order created" and "Order updated" webhooks. It is guest-whitelisted and validated by an HMAC-SHA256 signature against the server's `secret`. It then runs as the server's `creation_user`.

### Config doctypes

`WooCommerce Server` holds credentials (consumer key/secret are Password fields), the webhook secret, and mapping child tables (item fields, order statuses, payment methods, shipping rules). Call the shop from the server form with `get_api()`: it adds the URL scheme and reads the stored password when the form sends it masked, so it works before the server is saved. The "Add Missing ..." buttons get `{"rows", "error"}` from `fetch_names`. `Item WooCommerce Server` is a child table on `Item` (added via `fixtures/custom_field.json`) that links an Item to its WC ID per server. `WooCommerce Settings` is a Single doctype for global sync options.

## Gotchas

- `patches.txt` is currently empty. Add a patch there for data migrations, not an inline `migrate` hack.
- Doctype Python files have an auto-generated `DF` types block. Don't hand-edit inside it. It regenerates from the JSON on `bench migrate` in developer mode, or without it: `TypeExporter(frappe.get_doc("DocType", name)).export_types()` from `frappe.types.exporter` in `bench console`.
- A doctype test module makes the test records of every doctype its doctype links to, which clashes with ERPNext's (duplicate "Standard Buying"). Tests that only build docs in memory set `IGNORE_TEST_RECORD_DEPENDENCIES`.
- Whitelisted methods check argument types against their annotations in tests too, so pass ids as `str` where the signature says `str`.
