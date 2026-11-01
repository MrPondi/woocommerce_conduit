### Woocommerce Conduit

Connect your WooCommerce site to Frappe

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app woocommerce_conduit
```

### TODO:

- [x] Stock level sync to WooCommerce (on every Stock Ledger Entry, and daily)
- [x] Warehouse mapping on WooCommerce Server, to choose which warehouses count towards WooCommerce stock
- [x] Item Price hook, to push price changes immediately instead of waiting for the daily job
- [ ] Migrate the WooCommerce Server webhook secret to a Password field
- [ ] More webhooks: only orders ("Order created" and "Order updated") are handled so far
- [ ] Field value transforms and order item field mapping
- [x] Sales Order and Item form buttons
- [ ] Batch API: bulk-read 100 products and send only the changes to WooCommerce's batch endpoints (see woocommerce_fusion's `tasks/batch`; the pushes already compute, compare and write the differences)
- [x] Unit tests for item, price and order sync
- [ ] Integration tests against a real WooCommerce (WP Playground, as woocommerce_fusion's CI)
- [ ] Setup and usage documentation

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/woocommerce_conduit
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

gpl-3.0
