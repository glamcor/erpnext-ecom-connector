import base64
import functools
import hashlib
import hmac
import json

import frappe
from frappe import _
from shopify.resources import Webhook
from shopify.session import Session

from ecommerce_integrations_multistore.shopify.constants import (
	API_VERSION,
	EVENT_MAPPER,
	SETTING_DOCTYPE,
	STORE_DOCTYPE,
	WEBHOOK_EVENTS,
)
from ecommerce_integrations_multistore.shopify.utils import create_shopify_log


def temp_shopify_session(func):
	"""Any function that needs to access shopify api needs this decorator. 
	The decorator starts a temp session that's destroyed when function returns.
	
	For multi-store support, pass store_name as a kwarg to the decorated function.
	If store_name is not provided, falls back to singleton (backward compatibility).
	"""

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		# no auth in testing
		if frappe.flags.in_test:
			return func(*args, **kwargs)

		store_name = kwargs.get("store_name")
		
		if store_name:
			# Multi-store mode: use specified store
			store = frappe.get_doc(STORE_DOCTYPE, store_name)
			if not store.is_enabled():
				frappe.throw(_("Shopify Store {0} is not enabled").format(store_name))
			auth_details = (store.shopify_url, API_VERSION, store.get_password("password"))
		else:
			# Backward compatibility: fall back to singleton
			if frappe.db.exists("DocType", SETTING_DOCTYPE):
				setting = frappe.get_doc(SETTING_DOCTYPE)
				if setting.is_enabled():
					auth_details = (setting.shopify_url, API_VERSION, setting.get_password("password"))
				else:
					return
			else:
				return

		with Session.temp(*auth_details):
			return func(*args, **kwargs)

	return wrapper


def register_webhooks(shopify_url: str, password: str, store_name: str = None) -> list[Webhook]:
	"""Register required webhooks with shopify and return registered webhooks."""
	new_webhooks = []

	# clear all stale webhooks matching current site url before registering new ones
	unregister_webhooks(shopify_url, password)

	with Session.temp(shopify_url, API_VERSION, password):
		for topic in WEBHOOK_EVENTS:
			webhook = Webhook.create({"topic": topic, "address": get_callback_url(), "format": "json"})

			if webhook.is_valid():
				new_webhooks.append(webhook)
			else:
				create_shopify_log(
					status="Error",
					response_data=webhook.to_dict(),
					exception=webhook.errors.full_messages(),
					store_name=store_name,
				)

	return new_webhooks


def unregister_webhooks(shopify_url: str, password: str) -> None:
	"""Unregister all webhooks from shopify that correspond to current site url."""
	url = get_current_domain_name()

	with Session.temp(shopify_url, API_VERSION, password):
		for webhook in Webhook.find():
			if url in webhook.address:
				webhook.destroy()


def get_current_domain_name() -> str:
	"""Get current site domain name. E.g. test.erpnext.com

	If developer_mode is enabled and localtunnel_url is set in site config then domain  is set to localtunnel_url.
	"""
	if frappe.conf.developer_mode and frappe.conf.localtunnel_url:
		return frappe.conf.localtunnel_url
	else:
		return frappe.request.host


def get_callback_url() -> str:
	"""Shopify calls this url when new events occur to subscribed webhooks.

	If developer_mode is enabled and localtunnel_url is set in site config then callback url is set to localtunnel_url.
	"""
	url = get_current_domain_name()

	return f"https://{url}/api/method/ecommerce_integrations_multistore.shopify.connection.store_request_data"


@frappe.whitelist(allow_guest=True)
def store_request_data() -> None:
	"""Multi-store webhook endpoint. Routes requests to correct store based on X-Shopify-Shop-Domain header."""
	if frappe.request:
		shop_domain = frappe.get_request_header("X-Shopify-Shop-Domain")
		hmac_header = frappe.get_request_header("X-Shopify-Hmac-Sha256")
		event = frappe.request.headers.get("X-Shopify-Topic")
		
		# Log every webhook attempt for debugging
		frappe.log_error(
			message=f"Webhook received:\nShop Domain: {shop_domain}\nEvent: {event}\nHMAC present: {bool(hmac_header)}",
			title="Shopify Webhook Received"
		)

		# Find the store by domain
		store = get_store_by_domain(shop_domain)
		
		frappe.log_error(
			message=f"Store lookup for domain '{shop_domain}': {'Found: ' + store.name if store else 'NOT FOUND'}",
			title="Shopify Webhook - Store Lookup"
		)
		if not store:
			# Try to decode request data for logging
			try:
				request_data = json.loads(frappe.request.data) if frappe.request.data else None
			except:
				request_data = frappe.request.data.decode('utf-8') if isinstance(frappe.request.data, bytes) else str(frappe.request.data)
			
			create_shopify_log(
				status="Error",
				message=f"No enabled Shopify Store found for domain: {shop_domain}",
				request_data=request_data,
			)
			frappe.throw(_("Store not found for domain {0}").format(shop_domain))

		# Validate HMAC with store-specific secret
		try:
			_validate_request(frappe.request, hmac_header, store)
			frappe.log_error(
				message=f"HMAC validation passed for store {store.name}",
				title="Shopify Webhook - HMAC Valid"
			)
		except Exception as hmac_error:
			frappe.log_error(
				message=f"HMAC validation FAILED for store {store.name}: {str(hmac_error)}",
				title="Shopify Webhook - HMAC Failed"
			)
			raise

		data = json.loads(frappe.request.data)
		event = frappe.request.headers.get("X-Shopify-Topic")
		
		frappe.log_error(
			message=f"Processing event '{event}' for store {store.name}",
			title="Shopify Webhook - Processing"
		)

		# Process with store context
		process_request(data, event, store_name=store.name)


def get_store_by_domain(domain: str):
	"""Find enabled Shopify Store by domain."""
	if not domain:
		return None
	
	# Clean domain (remove https://, etc.)
	domain = domain.replace("https://", "").replace("http://", "").strip()
	
	store_name = frappe.db.get_value(STORE_DOCTYPE, {"shopify_url": domain, "enabled": 1}, "name")
	if store_name:
		return frappe.get_doc(STORE_DOCTYPE, store_name)
	return None


def update_store_locations(store):
	"""Fetch locations from Shopify and populate warehouse mapping table."""
	with Session.temp(store.shopify_url, API_VERSION, store.get_password("password")):
		store.shopify_warehouse_mapping = []
		for locations in PaginatedIterator(Location.find()):
			for location in locations:
				store.append(
					"shopify_warehouse_mapping",
					{"shopify_location_id": location.id, "shopify_location_name": location.name},
				)


def process_request(data, event, store_name=None):
	"""Process webhook request and enqueue background job."""
	# create log
	log = create_shopify_log(method=EVENT_MAPPER[event], request_data=data, store_name=store_name)

	# enqueue background job
	frappe.enqueue(
		method=EVENT_MAPPER[event],
		queue="short",
		timeout=300,
		is_async=True,
		**{"payload": data, "request_id": log.name, "store_name": store_name},
	)


def _validate_request(req, hmac_header, store=None):
	"""Validate HMAC signature with store-specific or singleton secret."""
	if store:
		secret_key = store.shared_secret
	else:
		# Backward compatibility: use singleton
		settings = frappe.get_doc(SETTING_DOCTYPE)
		secret_key = settings.shared_secret

	sig = base64.b64encode(hmac.new(secret_key.encode("utf8"), req.data, hashlib.sha256).digest())

	if sig != bytes(hmac_header.encode()):
		create_shopify_log(
			status="Error", 
			request_data=req.data,
			store_name=store.name if store else None
		)
		frappe.throw(_("Unverified Webhook Data"))
