# Copyright (c) 2025, Karol Parzonka and contributors
# For license information, please see license.txt

from urllib.parse import urlparse, urlunparse

import frappe
from frappe import _
from frappe.model.document import Document

from woocommerce_conduit.woocommerce_conduit.woocommerce_api import WooCommerceAPI


class WooCommerceServer(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_consumer_key: DF.Data
		api_consumer_secret: DF.Data
		creation_user: DF.Link
		enabled: DF.Check
		last_sync_time: DF.Datetime | None
		woocommerce_server_url: DF.Data

	# end: auto-generated types
	def autoname(self):
		"""
		Set name from woocommerce_server_url field
		"""
		self.name = urlparse(self.woocommerce_server_url).netloc

	def validate(self):
		parsed_url = urlparse(self.woocommerce_server_url)

		if not parsed_url.netloc:
			if not parsed_url.scheme:
				parsed_url = urlparse(f"https://{self.woocommerce_server_url}")

			if not parsed_url.netloc:
				frappe.throw(_("Please enter a valid WooCommerce Server URL"))

		url_scheme = parsed_url.scheme if parsed_url.scheme in ["http", "https"] else "https"
		self.woocommerce_server_url = urlunparse((url_scheme, parsed_url.netloc, "", "", "", ""))

		if not self.test_api_credentials():
			frappe.throw(_("WooCommerce API credentials are not valid"))

	def test_api_credentials(self) -> bool:
		wcapi = WooCommerceAPI(
			url=self.woocommerce_server_url,
			consumer_key=self.api_consumer_key,
			consumer_secret=self.api_consumer_secret,
			version="wc/v3",
		)
		res = wcapi.get("system_status", params={"_fields": "environment"})

		if res.status_code == 200:
			return True
		else:
			return False
