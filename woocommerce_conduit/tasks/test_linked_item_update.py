# Copyright (c) 2026, Karol Parzonka and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_conduit.tasks import linked_item_update
from woocommerce_conduit.tasks.field_map import LOGGED_PROBLEMS_FLAG
from woocommerce_conduit.tasks.linked_item_update import (
	get_fields_to_read,
	get_primary_links,
	make_review,
	update_linked_items,
)
from woocommerce_conduit.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)

SERVER = "one.example.com"
REAL_GET_ALL = frappe.get_all
WEIGHT_ROW = _dict(
	name="row-weight",
	erpnext_field_name="weight_per_unit | Weight Per Unit",
	woocommerce_field_name="$.weight",
	transform=None,
	idx=1,
)
NAME_ROW = _dict(
	name="row-name",
	erpnext_field_name="item_name | Item Name",
	woocommerce_field_name="$.woocommerce_name",
	transform=None,
	idx=2,
)


def make_product(product_id, **values) -> WooCommerceProduct:
	with patch.object(WooCommerceProduct, "_init_api", return_value=[]):
		return frappe.get_doc(
			{
				"doctype": "WooCommerce Product",
				"name": f"{SERVER}~{product_id}",
				"woocommerce_server": SERVER,
				"woocommerce_id": str(product_id),
				"woocommerce_date_modified": "2026-10-02 10:00:00",
				"date_modified_gmt": "2026-10-02T08:00:00",
				**values,
			}
		)  # type: ignore


def item(item_code, **values) -> _dict:
	"""
	An Item's fields as a review reads them
	"""
	return _dict(
		{
			"name": item_code,
			"item_name": item_code,
			"has_variants": 0,
			"variant_of": None,
			"disabled": 0,
			"modified": "2026-10-01 10:00:00",
			"weight_per_unit": 0.0,
			**values,
		}
	)


def family(template_weight, variant_weights):
	"""
	A template made in ERPNext with a variant of each weight, MOWER-1, MOWER-2, ...
	"""
	template = item("MOWER", has_variants=1, weight_per_unit=template_weight)
	variants = [
		item(f"MOWER-{index}", variant_of="MOWER", weight_per_unit=weight)
		for index, weight in enumerate(variant_weights, start=1)
	]
	return template, variants


class TestPrimaryLinks(IntegrationTestCase):
	def test_an_item_whose_primary_connection_is_another_shop_is_counted_not_listed(self):
		def get_all(doctype, filters=None, fields=None, pluck=None, order_by=None, **kwargs):
			if doctype != "Item WooCommerce Server":
				return REAL_GET_ALL(
					doctype, filters=filters, fields=fields, pluck=pluck, order_by=order_by, **kwargs
				)
			if pluck:
				return ["MOWER-1", "MOWER-2", "MOWER-3"]
			return [
				_dict(parent="MOWER-1", woocommerce_server=SERVER, woocommerce_id="1", is_primary=0, idx=1),
				_dict(
					parent="MOWER-2",
					woocommerce_server="two.example.com",
					woocommerce_id="7",
					is_primary=1,
					idx=1,
				),
				_dict(parent="MOWER-2", woocommerce_server=SERVER, woocommerce_id="2", is_primary=0, idx=2),
				# Without a mark, the first connection is the primary one
				_dict(
					parent="MOWER-3",
					woocommerce_server="two.example.com",
					woocommerce_id="8",
					is_primary=0,
					idx=1,
				),
				_dict(parent="MOWER-3", woocommerce_server=SERVER, woocommerce_id="3", is_primary=0, idx=2),
			]

		with patch("frappe.get_all", side_effect=get_all):
			links, elsewhere = get_primary_links(SERVER)

		self.assertEqual((links, elsewhere), ({"MOWER-1": "1"}, 2))


class TestReview(IntegrationTestCase):
	def review(self, items: dict, products: dict, links: dict, elsewhere=0, variants=(), rows=(WEIGHT_ROW,)):
		"""
		Review the `links` (Item code → product id) among these Items, whose products are `products` (by Item
		code), with `variants` the enabled variants of their templates
		"""

		def load_products(server, woocommerce_ids, error_title, fields, variations_alone=False):
			self.read = _dict(ids=woocommerce_ids, fields=fields, variations_alone=variations_alone)
			return {links[code]: product for code, product in products.items()}

		frappe.flags.pop(LOGGED_PROBLEMS_FLAG, None)
		# Before frappe.get_all is patched, which loading it would use
		frappe.get_meta("Item")
		with (
			patch("frappe.get_cached_doc", return_value=_dict(item_field_map=list(rows))),
			patch.object(linked_item_update, "get_primary_links", return_value=(links, elsewhere)),
			patch.object(
				linked_item_update,
				"get_item_values",
				side_effect=lambda codes, targets: {code: items[code] for code in codes if code in items},
			),
			patch.object(linked_item_update, "load_products", side_effect=load_products),
			patch(
				"frappe.get_all",
				side_effect=lambda doctype, *args, **kwargs: (
					list(variants) if doctype == "Item" else REAL_GET_ALL(doctype, *args, **kwargs)
				),
			) as get_all,
			patch.object(linked_item_update, "publish_review_progress") as progress,
		):
			review = make_review(SERVER, [row.name for row in rows])
		self.get_all, self.progress = get_all, progress
		return review

	@staticmethod
	def listed(review) -> list[str]:
		return [entry["item_code"] for entry in review["items"]]

	def test_only_items_whose_fields_would_change_are_listed(self):
		items = {"MOWER-1": item("MOWER-1", weight_per_unit=1.5), "MOWER-2": item("MOWER-2")}
		products = {"MOWER-1": make_product(1, weight="1.50"), "MOWER-2": make_product(2, weight="2.5")}

		review = self.review(items, products, links={"MOWER-1": "1", "MOWER-2": "2"})

		self.assertEqual(self.listed(review), ["MOWER-2"])
		(entry,) = review["items"]
		self.assertEqual(
			[(change["fieldname"], change["old"], change["new"]) for change in entry["changes"]],
			[("weight_per_unit", 0.0, "2.5")],
		)
		self.assertEqual(entry["woocommerce_id"], "2")
		self.assertFalse(entry["edited_since"])
		# Only the fields the rows' paths start at, and a variation on its own, as the sync reads it
		self.assertEqual((self.read.fields, self.read.variations_alone), (["weight"], True))
		self.assertEqual(review["checked"], 2)
		self.assertEqual(self.progress.call_args.args, (SERVER, 2, 2))

	def test_products_that_can_not_be_read_and_items_of_another_shop_are_counted(self):
		items = {"MOWER-1": item("MOWER-1"), "MOWER-2": item("MOWER-2")}

		review = self.review(
			items,
			{"MOWER-1": make_product(1, weight="2")},
			links={"MOWER-1": "1", "MOWER-2": "2"},
			elsewhere=3,
		)

		self.assertEqual((review["unread"], review["elsewhere"]), (1, 3))

	def test_an_item_edited_in_erpnext_since_its_product_changed_says_so(self):
		items = {"MOWER-1": item("MOWER-1", modified="2026-10-03 10:00:00")}

		review = self.review(items, {"MOWER-1": make_product(1, weight="2")}, links={"MOWER-1": "1"})

		self.assertTrue(review["items"][0]["edited_since"])

	def test_a_row_with_a_transform_reads_the_whole_product(self):
		gtin = _dict(woocommerce_field_name="$.meta_data[?key=='_gtin'].value")

		self.assertEqual(get_fields_to_read([WEIGHT_ROW, gtin]), ["meta_data", "weight"])
		# A transform gets the whole record
		self.assertEqual(get_fields_to_read([WEIGHT_ROW, _dict(NAME_ROW, transform="Decimal comma")]), ["*"])

	def review_family(self, template, variants, products, links=None):
		items = {entry.name: entry for entry in [template, *variants]}
		return self.review(
			items,
			products,
			links=links or {code: str(index) for index, code in enumerate(products, start=1)},
			variants=variants,
		)

	def test_a_template_follows_the_value_all_its_enabled_variants_would_share(self):
		# A family made in ERPNext: each variant linked to its own product, the template to nothing. MOWER-3
		# has the weight already.
		template, variants = family(10.0, [0.0, 10.0, 12.0])
		products = {
			"MOWER-1": make_product(1, weight="12"),
			"MOWER-2": make_product(2, weight="12"),
			"MOWER-3": make_product(3, weight="12"),
		}

		review = self.review_family(template, variants, products)

		self.assertEqual(self.listed(review), ["MOWER", "MOWER-1", "MOWER-2"])
		entry = review["items"][0]
		self.assertEqual(
			[(change["old"], change["new"], change["follows"]) for change in entry["changes"]],
			[(10.0, "12", True)],
		)
		# It waits for the variants whose weight changes, and has no product to be linked to
		self.assertEqual((entry["follows"], entry["variants"]), (["MOWER-1", "MOWER-2"], 3))
		self.assertNotIn("woocommerce_id", entry)
		# Only the enabled variants count
		(query,) = [call for call in self.get_all.call_args_list if call.args[0] == "Item"]
		self.assertEqual(query.kwargs["filters"]["disabled"], 0)

	def test_an_enabled_variant_with_another_value_keeps_the_template_as_it_is(self):
		# MOWER-3 isn't linked to this shop, and weighs 14
		template, variants = family(10.0, [0.0, 10.0, 14.0])
		products = {"MOWER-1": make_product(1, weight="12"), "MOWER-2": make_product(2, weight="12")}

		review = self.review_family(template, variants, products)

		self.assertEqual(self.listed(review), ["MOWER-1", "MOWER-2"])

	def test_variants_without_a_value_propose_nothing_for_their_template(self):
		template, variants = family(10.0, [0.0, 0.0])
		products = {"MOWER-1": make_product(1, weight=""), "MOWER-2": make_product(2, weight="")}

		self.assertEqual(self.review_family(template, variants, products)["items"], [])

	def test_the_shared_value_replaces_the_templates_own_products(self):
		template, variants = family(10.0, [0.0, 0.0])
		products = {
			"MOWER": make_product(9, weight="15"),
			"MOWER-1": make_product(1, weight="12"),
			"MOWER-2": make_product(2, weight="12"),
		}

		review = self.review_family(
			template, variants, products, links={"MOWER": "9", "MOWER-1": "1", "MOWER-2": "2"}
		)

		entry = review["items"][0]
		self.assertEqual([(change["new"], change["follows"]) for change in entry["changes"]], [("12", True)])
		# None of its own product's values is left, so its link to that product isn't checked
		self.assertNotIn("woocommerce_id", entry)

	def test_a_template_with_the_shared_value_drops_its_own_products(self):
		template, variants = family(12.0, [0.0, 0.0])
		products = {
			"MOWER": make_product(9, weight="15"),
			"MOWER-1": make_product(1, weight="12"),
			"MOWER-2": make_product(2, weight="12"),
		}

		review = self.review_family(
			template, variants, products, links={"MOWER": "9", "MOWER-1": "1", "MOWER-2": "2"}
		)

		self.assertEqual(self.listed(review), ["MOWER-1", "MOWER-2"])


class FakeItem(_dict):
	"""
	An Item that keeps what its saves store in `stored`, by Item code
	"""

	def set(self, fieldname, value):
		self[fieldname] = value

	def save(self):
		if self.fails:
			raise frappe.ValidationError("<b>Weight Per Unit</b> can't be negative")
		self.stored[self.name] = {"weight_per_unit": self.weight_per_unit}
		self.saves.append(self.name)


def change(old, new, follows=False) -> list[dict]:
	return [
		{
			"fieldname": "weight_per_unit",
			"label": "Weight Per Unit",
			"old": old,
			"new": new,
			"follows": follows,
		}
	]


class TestUpdate(IntegrationTestCase):
	def setUp(self):
		self.stored, self.saves = {}, []
		# A family made in ERPNext, and an Item of its own
		self.items = {
			"MOWER": self.make_item("MOWER", 10.0, has_variants=1, linked=False),
			"MOWER-1": self.make_item("MOWER-1", 0.0, variant_of="MOWER", woocommerce_id="1"),
			"MOWER-2": self.make_item("MOWER-2", 10.0, variant_of="MOWER", woocommerce_id="2"),
			"SAW": self.make_item("SAW", 0.0, woocommerce_id="3"),
		}
		self.review_items = [
			{
				"item_code": "MOWER",
				"template": True,
				"changes": change(10.0, "12", True),
				"follows": ["MOWER-1", "MOWER-2"],
			},
			{"item_code": "MOWER-1", "template": False, "woocommerce_id": "1", "changes": change(0.0, "12")},
			{"item_code": "MOWER-2", "template": False, "woocommerce_id": "2", "changes": change(10.0, "12")},
			{"item_code": "SAW", "template": False, "woocommerce_id": "3", "changes": change(0.0, "5")},
		]

	def make_item(self, item_code, weight, linked=True, woocommerce_id=None, **values) -> FakeItem:
		self.stored[item_code] = {"weight_per_unit": weight}
		connections = [_dict(woocommerce_server=SERVER, woocommerce_id=woocommerce_id, is_primary=1, idx=1)]
		return FakeItem(
			name=item_code,
			weight_per_unit=weight,
			woocommerce_servers=connections if linked else [],
			flags=_dict(),
			fails=False,
			stored=self.stored,
			saves=self.saves,
			**values,
		)

	def update(self, item_codes, review=True, review_id=None):
		entries = {entry["item_code"]: entry for entry in self.review_items}

		def get_all(doctype, filters=None, fields=None, **kwargs):
			if doctype != "Item":
				return REAL_GET_ALL(doctype, filters=filters, fields=fields, **kwargs)
			# The variants of the template as the database has them, saves included
			return [
				_dict(name=code, **self.stored[code])
				for code, entry in self.items.items()
				if entry.get("variant_of") == filters["variant_of"]
			]

		with (
			patch("frappe.has_permission", return_value=True),
			patch.object(
				linked_item_update,
				"get_review_summary",
				return_value={"review_id": "first"} if review else None,
			),
			patch.object(
				linked_item_update,
				"get_review_entries",
				side_effect=lambda server, codes: {code: entries[code] for code in codes if code in entries},
			),
			patch.object(linked_item_update, "drop_from_review") as drop_from_review,
			patch("frappe.get_doc", side_effect=lambda doctype, name: self.items[name]),
			patch("frappe.get_all", side_effect=get_all),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe, "log_error") as log_error,
		):
			results = update_linked_items(SERVER, item_codes, review_id)
		self.dropped = drop_from_review.call_args.args[1]
		self.rollback, self.log_error = rollback, log_error
		return {code: result["status"] for code, result in results.items()}, results

	def test_the_ticked_items_get_their_reviewed_values_templates_last(self):
		statuses, _results = self.update(["MOWER", "MOWER-1", "MOWER-2"])

		self.assertEqual(statuses, {"MOWER-1": "updated", "MOWER-2": "updated", "MOWER": "updated"})
		self.assertEqual(self.saves, ["MOWER-1", "MOWER-2", "MOWER"])
		self.assertEqual(self.stored["MOWER"], {"weight_per_unit": "12"})
		# Saving the template copies nothing to its variants
		self.assertTrue(self.items["MOWER"].flags.dont_update_variants)
		self.assertTrue(self.items["MOWER"].flags.created_by_sync)
		# They are done with, and leave the review
		self.assertEqual(self.dropped, ["MOWER-1", "MOWER-2", "MOWER"])

	def test_a_template_whose_variants_do_not_all_have_the_value_is_left_alone(self):
		statuses, _results = self.update(["MOWER", "MOWER-1"])

		self.assertEqual(statuses, {"MOWER-1": "updated", "MOWER": "variants"})
		self.assertEqual(self.saves, ["MOWER-1"])
		# The template can be updated once its other variant is
		self.assertEqual(self.dropped, ["MOWER-1"])

	def test_an_item_changed_or_relinked_since_the_review_is_left_alone(self):
		self.items["MOWER-1"].weight_per_unit = 3.0
		self.items["MOWER-2"].woocommerce_servers[0].woocommerce_id = "22"

		statuses, _results = self.update(["MOWER-1", "MOWER-2"])

		self.assertEqual(statuses, {"MOWER-1": "changed", "MOWER-2": "relinked"})
		self.assertEqual(self.saves, [])
		# The review doesn't hold for them any more
		self.assertEqual(self.dropped, ["MOWER-1", "MOWER-2"])

	def test_a_failure_is_rolled_back_and_logged_and_the_others_are_kept(self):
		self.items["MOWER-1"].fails = True

		statuses, results = self.update(["MOWER-1", "SAW"])

		self.assertEqual(statuses, {"MOWER-1": "failed", "SAW": "updated"})
		self.assertEqual(results["MOWER-1"]["error"], "Weight Per Unit can't be negative")
		self.rollback.assert_called_once_with(save_point="woocommerce_linked_item")
		self.log_error.assert_called_once()
		# It can be tried again
		self.assertEqual(self.dropped, ["SAW"])

	def test_more_than_fifty_items_at_a_time_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.update([f"ITEM-{i}" for i in range(51)])

	def test_an_expired_review_can_not_be_updated(self):
		with self.assertRaises(frappe.ValidationError):
			self.update(["SAW"], review=False)

	def test_the_items_of_a_review_made_since_are_not_updated(self):
		# The person reviewed the shop again, in another tab, while this one still showed the first review
		with self.assertRaises(frappe.ValidationError):
			self.update(["SAW"], review_id="older")
		self.assertEqual(self.saves, [])

		self.update(["SAW"], review_id="first")
		self.assertEqual(self.saves, ["SAW"])


class TestReviewJob(IntegrationTestCase):
	def start(self, enabled=1, rows=None, enqueued=True):
		server = MagicMock(enabled=enabled, item_field_map=[WEIGHT_ROW, NAME_ROW])
		server.name = SERVER
		with (
			patch("frappe.get_doc", return_value=server),
			patch("frappe.has_permission", return_value=True),
			patch("frappe.enqueue", return_value=MagicMock() if enqueued else None) as enqueue,
		):
			started = linked_item_update.start_linked_item_review(SERVER, rows)
		return started, enqueue

	def test_a_review_runs_once_a_shop_and_person_for_the_rows_picked(self):
		# As the desk sends a list
		started, enqueue = self.start(rows='["row-name"]')

		self.assertTrue(started)
		job = enqueue.call_args.kwargs
		self.assertEqual(job["job_id"], f"woocommerce_linked_item_review::{SERVER}::Administrator")
		self.assertTrue(job["deduplicate"])
		self.assertEqual(job["rows"], ["row-name"])
		self.assertEqual(self.start()[1].call_args.kwargs["rows"], ["row-weight", "row-name"])
		# Frappe doesn't enqueue the job while one with its id is queued or running
		self.assertFalse(self.start(enqueued=False)[0])

	def test_a_disabled_shop_is_not_reviewed(self):
		with self.assertRaises(frappe.ValidationError):
			self.start(enabled=0)

	def test_a_running_review_says_so(self):
		with (
			patch("frappe.has_permission", return_value=True),
			patch.object(linked_item_update, "is_job_enqueued", return_value=True),
		):
			self.assertEqual(linked_item_update.get_linked_item_review(SERVER), {"running": True})

	def test_a_review_that_fails_is_logged_and_said(self):
		with (
			patch.object(linked_item_update, "make_review", side_effect=ConnectionError("down")),
			patch.object(linked_item_update, "keep_review") as keep_review,
			patch("frappe.publish_realtime") as publish_realtime,
			patch("frappe.msgprint") as msgprint,
			patch.object(frappe, "log_error") as log_error,
		):
			linked_item_update.review_linked_items(SERVER, ["row-weight"])

		keep_review.assert_not_called()
		log_error.assert_called_once()
		self.assertTrue(publish_realtime.call_args.args[1]["error"])
		self.assertEqual(publish_realtime.call_args.kwargs["user"], "Administrator")
		self.assertEqual(msgprint.call_args.kwargs["indicator"], "red")

	def test_a_review_is_kept_for_the_person_who_made_it_and_loses_the_items_done(self):
		server = "review-test.example.com"
		items = [{"item_code": code, "changes": change(0.0, "1")} for code in ("SAW", "MOWER", "AXE")]
		linked_item_update.keep_review(server, {"review_id": "first", "checked": 3, "items": items})
		try:
			linked_item_update.drop_from_review(server, ["MOWER"])
			review = linked_item_update.get_review(server)
			self.assertEqual(
				(review["review_id"], [entry["item_code"] for entry in review["items"]]),
				("first", ["SAW", "AXE"]),
			)
			self.assertEqual(list(linked_item_update.get_review_entries(server, ["AXE", "MOWER"])), ["AXE"])
			with self.set_user("Guest"):
				self.assertIsNone(linked_item_update.get_review(server))
		finally:
			frappe.cache.delete_value(
				[linked_item_update.get_review_key(server), linked_item_update.get_entries_key(server)]
			)
