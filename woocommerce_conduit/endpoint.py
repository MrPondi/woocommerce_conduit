import base64
import hashlib
import hmac
import json
import re
from http import HTTPStatus

import frappe
from frappe import _
from werkzeug.wrappers import Response

from woocommerce_conduit.tasks.sync_sales_orders import enqueue_sales_order_sync
from woocommerce_conduit.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_conduit.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
	parse_domain_from_url,
)

# The body of the ping WooCommerce sends when a webhook is saved, without a signature or source header
PING = re.compile(rb"^webhook_id=\d+$")


def validate_request() -> tuple[WooCommerceServer | None, Response | None]:
	"""
	The WooCommerce Server a webhook comes from, once its signature checks out against the server's secret,
	or the response that refuses the webhook
	"""
	try:
		webhook_source_url: str = frappe.get_request_header("x-wc-webhook-source", "")  # type: ignore
		wc_server: WooCommerceServer = frappe.get_doc(
			"WooCommerce Server", parse_domain_from_url(webhook_source_url)
		)  # type: ignore
	except Exception:
		return None, Response(response=_("Missing Header"), status=HTTPStatus.BAD_REQUEST)

	expected = base64.b64encode(
		hmac.new((wc_server.secret or "").encode("utf8"), frappe.request.data or b"", hashlib.sha256).digest()
	)
	received = (frappe.get_request_header("x-wc-webhook-signature", "") or "").encode()  # type: ignore
	# In constant time, so the signature can't be guessed from how long a refusal takes
	if not hmac.compare_digest(expected, received):
		return None, Response(response=_("Unauthorized"), status=HTTPStatus.UNAUTHORIZED)

	return wc_server, None


@frappe.whitelist(allow_guest=True, methods=["POST"])
def order_created(*args, **kwargs):
	"""
	Accepts payload data from the WooCommerce "Order created" and "Order updated" webhooks. The name is
	that of the first, and part of the delivery URL of the webhooks that are set up already.
	"""
	data = frappe.request.data if frappe.request else None
	if not data:
		return Response(response=_("Missing Header"), status=HTTPStatus.BAD_REQUEST)
	# Answered, so WooCommerce saves the webhook without an error. Nothing is done for it.
	if PING.match(data):
		return Response(status=HTTPStatus.OK)

	wc_server, refusal = validate_request()
	if refusal:
		return refusal
	# Acknowledged, so WooCommerce doesn't retry, and left alone: the shop's sync is off
	if not wc_server.enabled:
		return Response(status=HTTPStatus.OK)

	event = frappe.get_request_header("x-wc-webhook-event")
	if event not in ("created", "updated"):
		return Response(response=_("Event not supported"), status=HTTPStatus.BAD_REQUEST)

	try:
		order = json.loads(data)
	except ValueError:
		order = None
	if not isinstance(order, dict) or not order.get("id"):
		return Response(response=_("Invalid payload"), status=HTTPStatus.BAD_REQUEST)

	frappe.set_user(wc_server.creation_user)
	enqueue_sales_order_sync(
		woocommerce_order_name=generate_woocommerce_record_name_from_domain_and_id(
			wc_server.name, order["id"]
		)
	)
	return Response(status=HTTPStatus.OK)
