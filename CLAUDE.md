# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Frappe/ERPNext app (`woocommerce_conduit`, requires `erpnext`) that syncs WooCommerce items, item prices and orders with ERPNext. It is a slimmer rewrite related to the `woocommerce_fusion` app that sits beside it in `apps/`. Fusion has features Conduit does not: batch/queue API, stock-level sync, Item Price hook, field transforms, warehouse mapping, UI JS. Do not assume Fusion code can be copied as-is, because the sync classes differ.

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

`WooCommerce Product` and `WooCommerce Order` are **virtual doctypes**. They have no DB tables. They subclass `WooCommerceDocument` in [woocommerce/woocommerce_api.py](woocommerce_conduit/woocommerce/woocommerce_api.py), which implements `load_from_db`, `get_list_of_records`, `get_count_of_records`, and no-op `db_insert`/`db_update`/`delete`.

- A record's `name` is `"{server_domain}~{wc_id}"` (`WC_RESOURCE_DELIMITER = "~"`). Use `generate_woocommerce_record_name_from_domain_and_id` and `get_domain_and_id_from_woocommerce_record_name` rather than building or parsing it by hand.
- `WooCommerceDocument.__init__` builds one `WooCommerceAPI` client per **enabled** `WooCommerce Server` and throws `SyncDisabledError` if none exist. Queries fan out across all of those servers.
- Each subclass declares `resource`, `field_setter_map`, `field_getter_map` (Frappe fieldname → WC fields it needs), `orderby_map` and `mandatory_wc_fields`. Only the WC fields needed for the requested Frappe fields are requested (`_fields`). When you add a field to a virtual doctype, update these maps so it is actually fetched.
- `WooCommerceAPI` wraps the `woocommerce` client and enqueues a `WooCommerce Request Log` entry for every request (skipped under `frappe.flags.in_test`).
- Complex values (dicts/lists) are JSON-serialised into JSON fields. `after_load_from_db` on each subclass flattens or prettifies them for the form.
- `WooCommerce Product.get_list` also fetches variations. Its behaviour (variation fetching, batch size, max variations, cache timeout) is driven by `WooCommerce Settings`.

### Sync layer (`tasks/`)

- `tasks/sync.py` has `SynchroniseWooCommerce`, the base class. It loads all `WooCommerce Server` docs.
- `sync_items.py` has `SynchroniseItem`, which does a two-way ERPNext Item ↔ WC product sync. `sync_sales_orders.py` and `sync_item_prices.py` follow the same pattern.
- Sync direction and conflict handling use a **sync hash**: `Item WooCommerce Server.woocommerce_last_sync_hash` stores the WC `date_modified` from the last sync. Changing how it is written affects loop prevention.
- Documents created or updated by the sync set `doc.flags.created_by_sync` so the doc-event hooks don't re-trigger a sync. Keep that guard on any new hook.
- The sync entry points are:
  - `doc_events` in [hooks.py](woocommerce_conduit/hooks.py): `Item` on_update/after_insert, `Sales Order` on_submit. Each enqueues a background job per document. There is no queue or batching layer.
  - Scheduler: `hourly_long` (orders and products modified since the last sync; the cursor is stored in `WooCommerce Settings`) and `daily_long` (item prices).
  - Webhook: `order_created` in [endpoint.py](woocommerce_conduit/endpoint.py) is guest-whitelisted and validated by an HMAC-SHA256 signature against the server's `secret`. It then runs as the server's `creation_user`.

### Config doctypes

`WooCommerce Server` holds credentials (consumer key/secret are Password fields, read with `get_decrypted_password`), the webhook secret, and mapping child tables (item fields, order statuses, shipping rules). `Item WooCommerce Server` is a child table on `Item` (added via `fixtures/custom_field.json`) that links an Item to its WC ID per server. `WooCommerce Settings` is a Single doctype for global sync options.

## Gotchas

- `patches.txt` is currently empty. Add a patch there for data migrations, not an inline `migrate` hack.
- Doctype Python files have an auto-generated `DF` types block. Don't hand-edit inside it. It regenerates from the JSON on `bench migrate` in developer mode.
