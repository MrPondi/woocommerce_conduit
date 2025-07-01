import base64
import hashlib
import hmac
import json
from http import HTTPStatus

import frappe
from frappe import _
from werkzeug.wrappers import Response

from woocommerce_conduit.tasks.sync_sales_orders import run_sales_order_sync
from woocommerce_conduit.woocommerce_conduit.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce_conduit.woocommerce_api import (
	WC_RESOURCE_DELIMITER,
	parse_domain_from_url,
)


def validate_request() -> tuple[bool, Response | None]:
	# Get relevant WooCommerce Server
	try:
		webhook_source_url: str = frappe.get_request_header("x-wc-webhook-source", "")  # type: ignore
		wc_server: WooCommerceServer = frappe.get_doc(
			"WooCommerce Server", parse_domain_from_url(webhook_source_url)
		)  # type: ignore
	except Exception:
		return False, Response(response=_("Missing Header"), status=HTTPStatus.BAD_REQUEST)

	# Validate secret
	sig = base64.b64encode(
		hmac.new(wc_server.secret.encode("utf8"), frappe.request.data, hashlib.sha256).digest()
	)
	if frappe.request.data and not sig == frappe.get_request_header("x-wc-webhook-signature", "").encode():  # type: ignore
		return False, Response(response=_("Unauthorized"), status=HTTPStatus.UNAUTHORIZED)

	frappe.set_user(wc_server.creation_user)
	return True, None


@frappe.whitelist(allow_guest=True, methods=["POST"])
def order_created(*args, **kwargs):
	"""
	Accepts payload data from WooCommerce "Order Created" webhook
	"""
	valid, response = validate_request()
	if not valid:
		return response

	if frappe.request and frappe.request.data:
		try:
			order = json.loads(frappe.request.data)
		except ValueError:
			# woocommerce returns 'webhook_id=value' for the first request which is not JSON
			order = frappe.request.data
		event = frappe.get_request_header("x-wc-webhook-event")
	else:
		return Response(response=_("Missing Header"), status=HTTPStatus.BAD_REQUEST)

	if event == "created":
		webhook_source_url: str = frappe.get_request_header("x-wc-webhook-source", "")  # type: ignore
		woocommerce_order_name = (
			f"{parse_domain_from_url(webhook_source_url)}{WC_RESOURCE_DELIMITER}{order['id']}"  # type: ignore
		)
		frappe.enqueue(run_sales_order_sync, queue="long", woocommerce_order_name=woocommerce_order_name)
		return Response(status=HTTPStatus.OK)
	else:
		return Response(response=_("Event not supported"), status=HTTPStatus.BAD_REQUEST)
