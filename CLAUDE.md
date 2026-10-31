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
- `WooCommerce Product.get_list` also fetches variations. Its behaviour (variation fetching, batch size, max variations, cache timeout) is driven by `WooCommerce Settings`.

### Sync layer (`tasks/`)

- `tasks/sync.py` has `SynchroniseWooCommerce`, the base class. It loads all `WooCommerce Server` docs.
- `sync_items.py` has `SynchroniseItem`, which syncs WC products **into** ERPNext Items, one way. A product is pulled **once**: a missing Item is created (with its regular and sale price as Item Prices) when an order needs it or on a manual sync, and a linked Item is only updated again with `force_update` (the Sync buttons). One Item can be linked to several shops, and each shop keeps its own name, description, etc., so Item fields are never written back to WooCommerce. Only prices (`sync_item_prices.py`) and stock (`stock_update.py`) go from ERPNext to WooCommerce, each switched per `WooCommerce Server` (`enable_price_list_sync`, `enable_stock_level_synchronisation`) and per Item connection (`Item WooCommerce Server.enable_sync`, "Sync Prices and Stock", default on). Stock is the sum of the server's `warehouses`. ERPNext has no notion of supplier stock, so backorders and the like are left to other apps: they register a method under the `woocommerce_stock_values` hook (see `apply_stock_value_hooks` and the comment in `hooks.py`) and call `enqueue_stock_update(item_code)` to push again. Keep this app free of company-specific stock rules. Only the **primary** connection (`Item WooCommerce Server.is_primary`, the first row when none is marked, see `get_primary_connection`) updates an existing Item. The other shops only record their sync hash. An Item `validate` hook allows one primary and marks the first row when none is. `sync_sales_orders.py` and `sync_item_prices.py` follow the same pattern.
- Change detection uses a **sync hash**: `Item WooCommerce Server.woocommerce_last_sync_hash` stores the WC `date_modified` from the last sync. Changing how it is written affects loop prevention. It is a Datetime field, so compare it with `get_datetime` on both sides. A product is only pulled when its `date_modified_gmt` (converted to the system timezone) is newer than `Item.modified`, so edits made in ERPNext since aren't overwritten.
- `sync_sales_orders.py` has `SynchroniseSalesOrder`, which pulls WC orders into Sales Orders, built from mixins: `order_status.py` (what a status does, the status push), `order_payment.py` (Sales Invoice and Payment Entry) and `order_lines.py` (lines, taxes and charges).
  - The server's **Order Status Map** (`WooCommerce Server Order Status`, one row per WC status slug) decides what an order means: its `erpnext_action` (skip, submit, submit + invoice, hold, cancel, close, status only), and which Sales Order status is pushed as that WC status. Defaults are in `DEFAULT_STATUSES`. Only `CREATING_ACTIONS` create a Sales Order, and with it a Customer, Address and Contact. An action runs once, when the WC status changes.
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

`WooCommerce Server` holds credentials (consumer key/secret are Password fields, read with `get_decrypted_password`), the webhook secret, and mapping child tables (item fields, order statuses, shipping rules). `Item WooCommerce Server` is a child table on `Item` (added via `fixtures/custom_field.json`) that links an Item to its WC ID per server. `WooCommerce Settings` is a Single doctype for global sync options.

## Gotchas

- `patches.txt` is currently empty. Add a patch there for data migrations, not an inline `migrate` hack.
- Doctype Python files have an auto-generated `DF` types block. Don't hand-edit inside it. It regenerates from the JSON on `bench migrate` in developer mode.
