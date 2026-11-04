# Copyright (c) 2025, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.woocommerce.doctype.woocommerce_server import woocommerce_server
from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	normalise_server_url,
)

# The servers are only made in memory, so the records of what they link to aren't needed
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"User",
	"UOM",
	"Item Group",
	"Price List",
	"Company",
	"Warehouse",
	"Account",
	"Sales Taxes and Charges Template",
	"Item Tax Template",
	"Shipping Rule",
	"Mode of Payment",
	"Bank Account",
]


def new_server(**fields):
	return frappe.get_doc(
		{
			"doctype": "WooCommerce Server",
			"woocommerce_server_url": "shop.example.com",
			"api_consumer_key": "ck_typed",
			"api_consumer_secret": "cs_typed",
			**fields,
		}
	)


def shop_api(responses: dict):
	"""
	A client of a shop that answers each endpoint with its status code and records
	"""

	def get(endpoint, *args, **kwargs):
		status_code, records = responses[endpoint]
		response = MagicMock(status_code=status_code)
		response.json.return_value = records
		return response

	api = MagicMock()
	api.get.side_effect = get
	return api


# A shop with the zone for the rest of the world, which has no methods, and one for Poland
SHIPPING_ZONES = {
	"shipping/zones": (200, [{"id": 0, "name": "Pozostałe lokalizacje"}, {"id": 1, "name": "Polska"}]),
	"shipping/zones/0/methods": (200, []),
	"shipping/zones/1/methods": (
		200,
		[
			{"instance_id": 3, "method_id": "flat_rate", "title": "Kurier DPD", "enabled": True},
			{"instance_id": 4, "method_id": "flat_rate", "title": "Kurier DPD - pobranie", "enabled": True},
			{
				"instance_id": 5,
				"method_id": "local_pickup",
				"method_title": "Odbiór osobisty",
				"enabled": False,
			},
		],
	),
}


class TestWooCommerceServer(IntegrationTestCase):
	def test_url_without_scheme_gets_https(self):
		self.assertEqual(normalise_server_url("shop.example.com"), "https://shop.example.com")
		self.assertEqual(normalise_server_url("http://shop.example.com/shop/"), "http://shop.example.com")
		self.assertIsNone(normalise_server_url(""))

	def test_api_of_an_unsaved_server_uses_the_typed_values(self):
		with patch(
			"woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server.WooCommerceAPI"
		) as api:
			new_server().get_api()

		kwargs = api.call_args.kwargs
		self.assertEqual(kwargs["url"], "https://shop.example.com")
		self.assertEqual(kwargs["consumer_key"], "ck_typed")
		self.assertEqual(kwargs["consumer_secret"], "cs_typed")

	def test_masked_password_is_read_back(self):
		server = new_server(api_consumer_key="********")
		with patch.object(type(server), "get_password", return_value="ck_stored") as get_password:
			self.assertEqual(server._credential("api_consumer_key"), "ck_stored")
			self.assertEqual(server._credential("api_consumer_secret"), "cs_typed")
		get_password.assert_called_once()

	def test_missing_statuses_come_with_the_fetch_error(self):
		server = new_server()
		api = MagicMock()
		api.get.side_effect = ConnectionError("unreachable")
		with patch.object(type(server), "get_api", return_value=api), patch.object(frappe, "log_error"):
			result = server.get_missing_order_status_rows()

		self.assertEqual(result["error"], "unreachable")
		self.assertIn("pending", [row["woocommerce_status"] for row in result["rows"]])

	def test_missing_statuses_include_the_shops_own(self):
		server = new_server()
		response = MagicMock(status_code=200)
		response.json.return_value = [
			{"slug": "pending", "name": "Oczekujące na płatność"},
			{"slug": "ready-pickup", "name": "Do odbioru"},
		]
		api = MagicMock()
		api.get.return_value = response
		with patch.object(type(server), "get_api", return_value=api):
			result = server.get_missing_order_status_rows()

		self.assertIsNone(result["error"])
		labels = {row["woocommerce_status"]: row["woocommerce_status_label"] for row in result["rows"]}
		self.assertEqual(labels["pending"], "Oczekujące na płatność")
		self.assertEqual(labels["ready-pickup"], "Do odbioru")

	def test_missing_payment_methods_leave_the_mapped_ones(self):
		server = new_server(
			payment_method_map=[{"woocommerce_payment_method": "bacs", "mode_of_payment": "Wire Transfer"}]
		)
		response = MagicMock(status_code=200)
		response.json.return_value = [
			{"id": "bacs", "title": "Przelew bankowy"},
			{"id": "cod", "title": ""},
		]
		api = MagicMock()
		api.get.return_value = response
		with patch.object(type(server), "get_api", return_value=api):
			result = server.get_missing_payment_method_rows()

		api.get.assert_called_once_with("payment_gateways")
		self.assertEqual(
			result["rows"], [{"woocommerce_payment_method": "cod", "woocommerce_payment_method_title": "cod"}]
		)
		self.assertEqual(server.get_payment_method_row("bacs").mode_of_payment, "Wire Transfer")
		self.assertIsNone(server.get_payment_method_row("paypal"))

	def test_payment_method_is_mapped_once(self):
		server = new_server(
			payment_method_map=[
				{"woocommerce_payment_method": "bacs"},
				{"woocommerce_payment_method": "bacs"},
			]
		)
		with self.assertRaises(frappe.ValidationError):
			server.validate_payment_method_map()

	def test_missing_shipping_methods_come_from_every_zone(self):
		server = new_server(
			shipping_rule_map=[{"wc_shipping_method_id": "flat_rate:3", "shipping_rule": "DPD"}]
		)
		api = shop_api(SHIPPING_ZONES)
		with patch.object(type(server), "get_api", return_value=api):
			result = server.get_missing_shipping_method_rows()

		self.assertEqual(
			[call.args[0] for call in api.get.call_args_list],
			["shipping/zones", "shipping/zones/0/methods", "shipping/zones/1/methods"],
		)
		self.assertIsNone(result["error"])
		self.assertEqual(
			result["rows"],
			[
				{
					"wc_shipping_method_id": "flat_rate:4",
					"wc_shipping_method_title": "Kurier DPD - pobranie",
					"wc_shipping_zone": "Polska",
				},
				# A disabled method too, by the title of its kind when it has none of its own
				{
					"wc_shipping_method_id": "local_pickup:5",
					"wc_shipping_method_title": "Odbiór osobisty",
					"wc_shipping_zone": "Polska",
				},
			],
		)
		self.assertEqual(result["gone"], [])

	def test_shipping_methods_the_shop_no_longer_has_are_named(self):
		server = new_server(
			shipping_rule_map=[
				{
					"wc_shipping_method_id": "flat_rate:7",
					"wc_shipping_method_title": "Paczkomaty",
					"shipping_rule": "InPost",
				},
				{"wc_shipping_method_id": "flat_rate:8"},
				# For every flat rate, which no zone has to have
				{"wc_shipping_method_id": "flat_rate", "shipping_rule": "Courier"},
			]
		)
		with patch.object(type(server), "get_api", return_value=shop_api(SHIPPING_ZONES)):
			result = server.get_missing_shipping_method_rows()

		self.assertEqual(result["gone"], ["flat_rate:7 (Paczkomaty)", "flat_rate:8"])

	def test_a_zone_that_cant_be_read_adds_no_shipping_methods(self):
		server = new_server(shipping_rule_map=[{"wc_shipping_method_id": "flat_rate:7"}])
		api = shop_api({**SHIPPING_ZONES, "shipping/zones/1/methods": (500, None)})
		with patch.object(type(server), "get_api", return_value=api):
			result = server.get_missing_shipping_method_rows()

		# The rest of the world's methods alone would show every method of Poland as gone
		self.assertEqual(result, {"rows": [], "gone": [], "error": "WooCommerce returned 500"})

	def test_shipping_rule_of_a_shipping_line(self):
		server = new_server(
			shipping_rule_map=[
				{"wc_shipping_method_id": "flat_rate:3", "shipping_rule": "DPD"},
				{"wc_shipping_method_id": "flat_rate", "shipping_rule": "Courier"},
				{"wc_shipping_method_id": "local_pickup:5"},
			]
		)

		def rule(method_id, instance_id):
			return server.get_shipping_rule({"method_id": method_id, "instance_id": instance_id})

		self.assertEqual(rule("flat_rate", "3"), "DPD")
		# Another flat rate, and one without an instance, get the rule of every flat rate
		self.assertEqual(rule("flat_rate", "4"), "Courier")
		self.assertEqual(rule("flat_rate", "0"), "Courier")
		# A row without a rule doesn't count
		self.assertIsNone(rule("local_pickup", "5"))
		self.assertIsNone(rule("free_shipping", "6"))

	def test_shipping_method_is_mapped_once(self):
		server = new_server(
			shipping_rule_map=[
				{"wc_shipping_method_id": "flat_rate:3"},
				{"wc_shipping_method_id": " flat_rate:3 "},
			]
		)
		with self.assertRaises(frappe.ValidationError):
			server.validate_shipping_rule_map()

	def test_a_shipping_rule_must_fit_a_sales_order_of_the_company(self):
		def rule(**values):
			return frappe._dict(
				{
					"shipping_rule_type": "Selling",
					"company": "Prestige Garden",
					"disabled": 0,
					"account": "Freight - PG",
					**values,
				}
			)

		rules = {
			"DPD": rule(),
			"Purchases": rule(shipping_rule_type="Buying"),
			"Other company": rule(company="Other", account="Freight - OT"),
			"Old": rule(disabled=1),
			"On fees": rule(account="Fees - PG"),
		}
		servers = {
			name: new_server(
				company="Prestige Garden",
				freight_and_forwarding_account="Freight - PG",
				fee_account="Fees - PG",
				shipping_rule_map=[{"wc_shipping_method_id": "flat_rate:3", "shipping_rule": name}],
			)
			for name in rules
		}
		# Without a Fee Account the fees are booked on the freight account, as the rule's charge is
		without_fee_account = new_server(
			company="Prestige Garden",
			freight_and_forwarding_account="Freight - PG",
			shipping_rule_map=[{"wc_shipping_method_id": "flat_rate:3", "shipping_rule": "DPD"}],
		)

		with patch.object(
			frappe, "get_cached_value", side_effect=lambda doctype, name, *a, **kw: rules[name]
		):
			servers["DPD"].validate_shipping_rule_map()
			for name in ("Purchases", "Other company", "Old", "On fees"):
				with self.subTest(rule=name), self.assertRaises(frappe.ValidationError):
					servers[name].validate_shipping_rule_map()
			with self.assertRaises(frappe.ValidationError):
				without_fee_account.validate_shipping_rule_map()

	def test_missing_tax_classes_are_read_from_the_shop(self):
		server = new_server(
			tax_class_map=[{"woocommerce_tax_class": "standard", "item_tax_template": "VAT 23%"}]
		)
		response = MagicMock(status_code=200)
		response.json.return_value = [
			{"slug": "standard", "name": "Standardowa stawka"},
			{"slug": "zerowa-stawka", "name": "Zerowa stawka"},
		]
		api = MagicMock()
		api.get.return_value = response
		with patch.object(type(server), "get_api", return_value=api):
			result = server.get_missing_tax_class_rows()

		api.get.assert_called_once_with("taxes/classes")
		self.assertEqual(
			result["rows"],
			[{"woocommerce_tax_class": "zerowa-stawka", "woocommerce_tax_class_title": "Zerowa stawka"}],
		)

	def test_tax_class_is_mapped_once(self):
		# WooCommerce has the standard class empty on a product, and as "standard" in the list
		server = new_server(
			tax_class_map=[{"woocommerce_tax_class": ""}, {"woocommerce_tax_class": "standard"}]
		)
		with self.assertRaises(frappe.ValidationError):
			server.validate_tax_class_map()

	def test_item_tax_template_of_a_tax_class(self):
		server = new_server(
			tax_class_map=[
				{"woocommerce_tax_class": "standard", "item_tax_template": "VAT 23%"},
				{"woocommerce_tax_class": "zerowa-stawka", "item_tax_template": "VAT 0%"},
				{"woocommerce_tax_class": "obnizona-stawka"},
			],
		)
		self.assertEqual(server.get_item_tax_template(""), "VAT 23%")
		self.assertEqual(server.get_item_tax_template("zerowa-stawka"), "VAT 0%")
		self.assertIsNone(server.get_item_tax_template("obnizona-stawka"))

		# The default is for the classes without a template
		server.default_item_tax_template = "VAT 8%"
		self.assertEqual(server.get_item_tax_template("obnizona-stawka"), "VAT 8%")
		self.assertEqual(server.get_item_tax_template("zerowa-stawka"), "VAT 0%")

	def test_item_tax_template_that_leaves_out_a_vat_row_is_warned_about(self):
		server = new_server(
			default_item_tax_template="VAT 23%", sales_taxes_and_charges_template="Poland VAT"
		)
		rows = [frappe._dict(account_head="VAT 23% - PG"), frappe._dict(account_head="VAT 8% - PG")]
		templates = {
			"VAT 23%": frappe._dict(taxes=[frappe._dict(tax_type="VAT 23% - PG")]),
			"Both": frappe._dict(
				taxes=[frappe._dict(tax_type="VAT 23% - PG"), frappe._dict(tax_type="VAT 8% - PG")]
			),
		}
		with (
			patch.object(woocommerce_server, "get_vat_rows", return_value=rows),
			patch.object(frappe, "get_cached_doc", side_effect=lambda doctype, name: templates[name]),
			patch.object(frappe, "msgprint") as msgprint,
		):
			server.validate_tax_templates()
			self.assertIn("VAT 8% - PG", msgprint.call_args.args[0])

			msgprint.reset_mock()
			server.default_item_tax_template = "Both"
			server.validate_tax_templates()
			msgprint.assert_not_called()

	def test_new_server_starts_paused(self):
		server = new_server(pause_order_sync=1)
		with patch.object(frappe, "enqueue") as enqueue:
			server.track_order_sync_pause()
		self.assertTrue(server.order_sync_paused_since)
		enqueue.assert_not_called()

	def test_unpausing_syncs_what_changed_meanwhile(self):
		server = new_server(pause_order_sync=0, order_sync_paused_since="2026-10-30 09:00:00")
		server.name = "shop.example.com"
		with (
			patch.object(type(server), "is_new", return_value=False),
			patch.object(type(server), "has_value_changed", return_value=True),
			patch.object(frappe, "enqueue") as enqueue,
		):
			server.track_order_sync_pause()

		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["woocommerce_server"], "shop.example.com")
		self.assertEqual(kwargs["since"], "2026-10-30 09:00:00")
		self.assertIsNone(server.order_sync_paused_since)

	def test_unpausing_without_a_pause_date_syncs_nothing(self):
		server = new_server(pause_order_sync=0)
		with (
			patch.object(type(server), "is_new", return_value=False),
			patch.object(type(server), "has_value_changed", return_value=True),
			patch.object(frappe, "enqueue") as enqueue,
			patch.object(frappe, "msgprint") as msgprint,
		):
			server.track_order_sync_pause()
		enqueue.assert_not_called()
		msgprint.assert_called_once()

	def test_saving_an_unpaused_server_syncs_nothing(self):
		server = new_server(pause_order_sync=0)
		with (
			patch.object(type(server), "is_new", return_value=False),
			patch.object(type(server), "has_value_changed", return_value=False),
			patch.object(frappe, "enqueue") as enqueue,
		):
			server.track_order_sync_pause()
		enqueue.assert_not_called()

	def test_an_item_field_map_row_must_name_a_field_of_item(self):
		for erpnext_field_name in ("not_a_field | Not a field", "attributes | Attributes"):
			server = new_server(
				item_field_map=[{"erpnext_field_name": erpnext_field_name, "woocommerce_field_name": "$.sku"}]
			)
			with self.assertRaises(frappe.ValidationError):
				server.validate_item_map()

		server = new_server(
			item_field_map=[
				{
					"erpnext_field_name": "description | Description",
					"woocommerce_field_name": "$.short_description",
				}
			]
		)
		server.validate_item_map()

	def validate_item_path(self, path, erpnext_field_name="description | Description", **fields):
		new_server(
			item_field_map=[{"erpnext_field_name": erpnext_field_name, "woocommerce_field_name": path}],
			**fields,
		).validate_item_map()

	def test_an_item_field_map_path_starts_at_a_field_of_the_product(self):
		for path in (
			"$.meta_data[?key=='_gtin'].value",
			"$.categories[0].name",
			"tags[*].name",
			"$.global_unique_id",
			"$.woocommerce_name",
		):
			self.validate_item_path(path)

		# Not fields of the doctype, so they would find nothing, and paths that start nowhere in particular
		for path in ("$.dimensions.length", "$.not_a_field", "$..name", "$.*", "$"):
			with self.subTest(path=path), self.assertRaises(frappe.ValidationError):
				self.validate_item_path(path)

	def test_attributes_and_synced_images_are_refused_by_their_field(self):
		with self.assertRaises(frappe.ValidationError):
			self.validate_item_path("$.attributes[0].name", enable_image_sync=0)
		with self.assertRaises(frappe.ValidationError):
			self.validate_item_path("$.images[0].src", enable_image_sync=1)
		self.validate_item_path("$.images[0].src", enable_image_sync=0)
		# A meta key that holds the word is no reason to refuse the path
		self.validate_item_path("$.meta_data[?key=='_images_alt'].value", enable_image_sync=1)

	def test_item_fields_the_sync_owns_are_refused(self):
		for erpnext_field_name in ("item_code | Item Code", "stock_uom | Default Unit of Measure"):
			with self.subTest(field=erpnext_field_name), self.assertRaises(frappe.ValidationError):
				self.validate_item_path("$.sku", erpnext_field_name=erpnext_field_name)

	def test_the_order_line_fields_map_writes_only_what_it_may(self):
		def validate(erpnext_field_name):
			new_server(
				order_item_field_map=[
					{
						"erpnext_field_name": erpnext_field_name,
						"woocommerce_field_name": "$.meta_data[0].value",
					}
				]
			).validate_order_item_map()

		for erpnext_field_name in ("item_name | Item Name", "additional_notes | Additional Notes"):
			validate(erpnext_field_name)
		# A field the sync sets, one that changes what the line costs or how it is delivered, and one ERPNext
		# computes
		for erpnext_field_name in (
			"woocommerce_line_item_id | WooCommerce Line Item ID",
			"rate | Rate",
			"supplier | Supplier",
			"amount | Amount",
		):
			with self.subTest(field=erpnext_field_name), self.assertRaises(frappe.ValidationError):
				validate(erpnext_field_name)

	def test_the_customer_field_map_writes_only_what_it_may_from_the_order(self):
		def validate(erpnext_field_name, path="$.meta_data[?key=='_billing_nip'].value"):
			new_server(
				customer_field_map=[
					{"erpnext_field_name": erpnext_field_name, "woocommerce_field_name": path}
				]
			).validate_customer_map()

		validate("tax_id | Tax ID")
		validate("tax_id | Tax ID", "$.billing_company")
		# Who the Customer is, which the sync decides, and how it is billed
		for erpnext_field_name in (
			"customer_name | Customer Name",
			"woocommerce_identifier | Woocommerce Identifier",
			"default_price_list | Default Price List",
		):
			with self.subTest(field=erpnext_field_name), self.assertRaises(frappe.ValidationError):
				validate(erpnext_field_name)
		# The order has its addresses as billing_… fields, and a path must start at one of its fields
		for path in ("$.billing.company", "$..value"):
			with self.subTest(path=path), self.assertRaises(frappe.ValidationError):
				validate("tax_id | Tax ID", path)

	def validate_prefix(self, prefix, other_prefixes=()):
		server = new_server(name_sales_orders_by_number=1, sales_order_prefix=prefix)
		others = [
			frappe._dict(name="other.example.com", sales_order_prefix=other) for other in other_prefixes
		]
		with patch.object(frappe, "get_all", return_value=others):
			server.validate_sales_order_prefix()
		return server

	def test_each_shop_names_its_sales_orders_with_its_own_prefix(self):
		self.assertEqual(self.validate_prefix(" WEB- ", ["KL-"]).sales_order_prefix, "WEB-")
		# Order numbers repeat across shops
		for prefix, other in (("WEB-", " WEB- "), ("", None)):
			with self.subTest(prefix=prefix), self.assertRaises(frappe.ValidationError):
				self.validate_prefix(prefix, [other])

	def test_a_sales_order_prefix_frappe_cant_name_with_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.validate_prefix("<WEB>")

	def test_the_prefix_only_counts_when_sales_orders_are_named_by_number(self):
		with patch.object(frappe, "get_all") as get_all:
			new_server(sales_order_prefix="<WEB>").validate_sales_order_prefix()
		get_all.assert_not_called()
