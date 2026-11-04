# Copyright (c) 2026, Karol Parzonka and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, getdate
from frappe.utils.background_jobs import is_job_enqueued

from woocommerce_conduit.tasks.order_import_serials import check_serial_numbers

# The fields of a row of the Payment Method Map, which a new import copies from the shop's last one
PAYMENT_METHOD_FIELDS = (
	"woocommerce_payment_method",
	"woocommerce_payment_method_title",
	"mode_of_payment",
	"bank_account",
)


def get_mode_of_payment_account(mode_of_payment: str, company: str) -> str | None:
	"""
	The account a payment with this Mode of Payment is booked on for the company
	"""
	return frappe.db.get_value(
		"Mode of Payment Account", {"parent": mode_of_payment, "company": company}, "default_account"
	)


def get_import_job_id(woocommerce_server: str) -> str:
	"""
	The id of the job that imports the orders of a shop: one per shop, so two of its imports never run at once
	"""
	return f"woocommerce_order_import::{woocommerce_server}"


class WooCommerceOrderImport(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from woocommerce_conduit.woocommerce.doctype.woocommerce_order_import_payment_method.woocommerce_order_import_payment_method import (
			WooCommerceOrderImportPaymentMethod,
		)

		company: DF.Link | None
		created_from: DF.Date | None
		created_to: DF.Date | None
		deliver: DF.Check
		failed: DF.Int
		needs_review: DF.Int
		paid: DF.Int
		payment_method_map: DF.Table[WooCommerceOrderImportPaymentMethod]
		serial_number_summary: DF.SmallText | None
		serial_numbers: DF.Attach | None
		skipped: DF.Int
		status: DF.Literal["Pending", "In Progress", "Success", "Partial Success", "Timed Out", "Error"]
		woocommerce_server: DF.Link
	# end: auto-generated types

	def before_insert(self):
		# A patch brings the rows it moves, and tests build their own without calling the shop
		if self.payment_method_map or frappe.in_test or frappe.flags.in_patch:
			return
		self.add_payment_methods()

	def validate(self):
		self.company = (
			frappe.get_cached_value("WooCommerce Server", self.woocommerce_server, "company")
			if self.woocommerce_server
			else None
		)
		self.validate_dates()
		self.validate_payment_method_map()
		self.validate_serial_numbers()

	def validate_serial_numbers(self):
		"""
		Check the serial number file when it, or what it is checked against, changes. A file is refused whole,
		listing every row that is wrong.
		"""
		if not self.serial_numbers:
			self.serial_number_summary = None
			return
		if not (
			self.is_new() or any(self.has_value_changed(field) for field in ("serial_numbers", "deliver"))
		):
			return
		check_serial_numbers(self)

	def onload(self):
		self.set_onload("running", self.is_running())

	def on_trash(self):
		frappe.db.delete("WooCommerce Order Import Log", {"order_import": self.name})

	def is_running(self) -> bool:
		"""
		Whether an import of this shop runs, this one or another
		"""
		return bool(self.woocommerce_server) and is_job_enqueued(get_import_job_id(self.woocommerce_server))

	def validate_dates(self):
		if self.created_from and self.created_to and getdate(self.created_from) > getdate(self.created_to):
			frappe.throw(_("Created From can't be after Created To"))

	def validate_payment_method_map(self):
		"""
		One row per payment method, with a Mode of Payment that a Payment Entry of the company can be booked
		on, and a Bank Account of the company on that same account
		"""
		seen = set()
		for row in self.payment_method_map:
			row.woocommerce_payment_method = cstr(row.woocommerce_payment_method).strip()
			if row.woocommerce_payment_method in seen:
				frappe.throw(
					_("Row {0}: Payment method {1} is in the Payment Method Map more than once").format(
						row.idx, frappe.bold(row.woocommerce_payment_method)
					)
				)
			seen.add(row.woocommerce_payment_method)

			if not row.mode_of_payment:
				if row.bank_account:
					frappe.throw(_("Row {0}: a Bank Account needs a Mode of Payment").format(row.idx))
				continue
			if not self.company:
				continue
			account = get_mode_of_payment_account(row.mode_of_payment, self.company)
			if not account:
				frappe.throw(
					_("Row {0}: Mode of Payment {1} has no default account for company {2}").format(
						row.idx, frappe.bold(row.mode_of_payment), frappe.bold(self.company)
					)
				)
			if row.bank_account:
				self.validate_bank_account(row, account)

	def validate_bank_account(self, row, account: str):
		"""
		The Bank Account of a row is one of the company's, on the account its Mode of Payment books on. ERPNext
		doesn't compare the two, so the money would be booked on one account while the bank reconciliation
		looks for it on the other.
		"""
		bank_account = frappe.db.get_value(
			"Bank Account",
			row.bank_account,
			["company", "is_company_account", "disabled", "account"],
			as_dict=True,
		)
		if (
			not bank_account
			or not bank_account.is_company_account
			or bank_account.disabled
			or bank_account.company != self.company
		):
			frappe.throw(
				_("Row {0}: Bank Account {1} isn't an enabled bank account of company {2}").format(
					row.idx, frappe.bold(row.bank_account), frappe.bold(self.company)
				)
			)
		if bank_account.account != account:
			frappe.throw(
				_(
					"Row {0}: Bank Account {1} is on account {2}, while Mode of Payment {3} books on {4}. They"
					" must be the same, or the bank reconciliation won't find the payments."
				).format(
					row.idx,
					frappe.bold(row.bank_account),
					frappe.bold(bank_account.account or _("none")),
					frappe.bold(row.mode_of_payment),
					frappe.bold(account),
				)
			)

	def get_payment_method_row(self, payment_method: str | None):
		"""
		The row of the Payment Method Map for a WooCommerce payment method, or None
		"""
		if not payment_method:
			return None
		return next(
			(row for row in self.payment_method_map if row.woocommerce_payment_method == payment_method),
			None,
		)

	def add_payment_methods(self) -> None:
		"""
		The map of the shop's last import, so it isn't typed again, else a row without a Mode of Payment for
		each payment gateway of the shop
		"""
		if not self.woocommerce_server:
			return
		last_import = frappe.get_all(
			"WooCommerce Order Import",
			filters={"woocommerce_server": self.woocommerce_server},
			order_by="creation desc",
			limit=1,
			pluck="name",
		)
		if last_import:
			for row in frappe.get_doc("WooCommerce Order Import", last_import[0]).payment_method_map:
				self.append("payment_method_map", {field: row.get(field) for field in PAYMENT_METHOD_FIELDS})
			return
		for row in self.get_payment_method_rows(self.fetch_payment_methods()[0]):
			self.append("payment_method_map", row)

	def fetch_payment_methods(self) -> tuple[dict[str, str], str | None]:
		"""
		The id and title of every payment gateway of the shop, and why they couldn't be read
		"""
		server = frappe.get_doc("WooCommerce Server", self.woocommerce_server)
		return server.fetch_payment_methods()

	def get_payment_method_rows(self, live_methods: dict[str, str]) -> list[dict]:
		existing = {row.woocommerce_payment_method for row in self.payment_method_map}
		return [
			{"woocommerce_payment_method": method, "woocommerce_payment_method_title": title or method}
			for method, title in live_methods.items()
			if method and method not in existing
		]

	@frappe.whitelist()
	def get_missing_payment_method_rows(self) -> dict:
		"""
		The rows the Payment Method Map lacks, for the form to add, and why the shop couldn't be read if it
		couldn't
		"""
		if not self.woocommerce_server:
			return {"rows": [], "error": _("Pick a WooCommerce Server first")}
		live_methods, error = self.fetch_payment_methods()
		return {"rows": self.get_payment_method_rows(live_methods), "error": error}
