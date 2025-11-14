from copy import deepcopy

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_delivery_note
from frappe.utils import cint, cstr, getdate

from ecommerce_integrations_multistore.shopify.constants import (
	FULLFILLMENT_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
	STORE_DOCTYPE,
	STORE_LINK_FIELD,
)
from ecommerce_integrations_multistore.shopify.utils import create_shopify_log


def prepare_delivery_note(payload, request_id=None, store_name=None):
	"""Prepare delivery note from Shopify webhook.
	
	Args:
	    payload: Shopify order data
	    request_id: Integration log ID
	    store_name: Shopify Store name (multi-store support)
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	order = payload

	try:
		# Look for Sales Invoice instead of Sales Order
		sales_invoice = frappe.db.get_value(
			"Sales Invoice",
			{ORDER_ID_FIELD: cstr(order["id"])},
			["name", "docstatus", STORE_LINK_FIELD],
			as_dict=True
		)
		if sales_invoice:
			# Get store from Sales Invoice or use provided store_name
			store_name = store_name or sales_invoice.get(STORE_LINK_FIELD)
			
			# Get store-specific settings
			if store_name:
				setting = frappe.get_doc(STORE_DOCTYPE, store_name)
			else:
				# Backward compatibility
				setting = frappe.get_doc(SETTING_DOCTYPE)
			
			# Get the full Sales Invoice doc
			si = frappe.get_doc("Sales Invoice", sales_invoice.name)
			create_delivery_note(order, setting, si, store_name=store_name)
			create_shopify_log(status="Success", store_name=store_name)
		else:
			create_shopify_log(
				status="Invalid",
				message="Sales Invoice not found for syncing delivery note.",
				store_name=store_name
			)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True, store_name=store_name)


def create_delivery_note(shopify_order, setting, si, store_name=None):
	"""Create Delivery Note from Shopify order.
	
	Args:
	    shopify_order: Shopify order data
	    setting: Store or Setting doc
	    si: Sales Invoice doc
	    store_name: Shopify Store name for multi-store support
	"""
	if not cint(setting.sync_delivery_note):
		return

	for fulfillment in shopify_order.get("fulfillments"):
		if (
			not frappe.db.get_value("Delivery Note", {FULLFILLMENT_ID_FIELD: fulfillment.get("id")}, "name")
			and si.docstatus == 1
		):
			dn = make_delivery_note(si.name)
			setattr(dn, ORDER_ID_FIELD, fulfillment.get("order_id"))
			setattr(dn, ORDER_NUMBER_FIELD, shopify_order.get("name"))
			setattr(dn, FULLFILLMENT_ID_FIELD, fulfillment.get("id"))
			
			# Set store reference for multi-store
			if store_name:
				setattr(dn, STORE_LINK_FIELD, store_name)
			
			dn.set_posting_time = 1
			dn.posting_date = getdate(fulfillment.get("created_at"))
			dn.naming_series = setting.delivery_note_series or "DN-Shopify-"
			dn.items = get_fulfillment_items(
				dn.items, fulfillment.get("line_items"), fulfillment.get("location_id"), setting, store_name
			)
			dn.flags.ignore_mandatory = True
			dn.save()
			dn.submit()

			if shopify_order.get("note"):
				dn.add_comment(text=f"Order Note: {shopify_order.get('note')}")
			
			# Send to ShipStation if integration is installed
			send_to_shipstation(dn, setting)


def get_fulfillment_items(dn_items, fulfillment_items, location_id=None, setting=None, store_name=None):
	"""Get fulfillment items for Delivery Note.
	
	Args:
	    dn_items: Delivery Note items
	    fulfillment_items: Shopify fulfillment line items
	    location_id: Shopify location ID
	    setting: Store or Setting doc
	    store_name: Shopify Store name for multi-store support
	"""
	# local import to avoid circular imports
	from ecommerce_integrations_multistore.shopify.product import get_item_code

	fulfillment_items = deepcopy(fulfillment_items)

	# Get setting if not provided
	if not setting:
		if store_name:
			setting = frappe.get_cached_doc(STORE_DOCTYPE, store_name)
		else:
			setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	
	wh_map = setting.get_integration_to_erpnext_wh_mapping()
	warehouse = wh_map.get(str(location_id)) or setting.warehouse

	final_items = []

	def find_matching_fullfilement_item(dn_item):
		nonlocal fulfillment_items

		for item in fulfillment_items:
			if get_item_code(item, store_name=store_name) == dn_item.item_code:
				fulfillment_items.remove(item)
				return item

	for dn_item in dn_items:
		if shopify_item := find_matching_fullfilement_item(dn_item):
			final_items.append(dn_item.update({"qty": shopify_item.get("quantity"), "warehouse": warehouse}))

	return final_items


def send_to_shipstation(delivery_note, setting):
	"""Send Delivery Note to ShipStation if integration is installed.
	
	Args:
	    delivery_note: Delivery Note document
	    setting: Shopify Store settings
	"""
	try:
		# First try V2 integration
		from ecommerce_integrations_multistore.shopify.shipstation_v2 import update_shipstation_integration_for_v2
		
		# Use V2 API
		result = update_shipstation_integration_for_v2(delivery_note, setting)
		
		if result.get("success"):
			frappe.log_error(
				message=f"Successfully sent {delivery_note.name} to ShipStation V2. Order ID: {result.get('order_id')}",
				title="ShipStation V2 Success"
			)
			return
		
		# If V2 fails, log the error but don't try V1 (it's broken)
		if not result.get("success"):
			frappe.log_error(
				message=f"ShipStation V2 failed: {result.get('error')}",
				title="ShipStation V2 Error"
			)
		
		# Skip V1 fallback - it has enum errors and requires api_secret
		return
		
		# V1 code below is disabled
		if False and frappe.db.exists("Module Def", "ShipStation Integration"):
			# Import ShipStation functions if available
			from shipstation_integration.api import send_order_to_shipstation
			
			# Check if ShipStation is configured for this store
			if hasattr(setting, 'shipstation_api_key') and setting.shipstation_api_key:
				# Send the delivery note to ShipStation
				result = send_order_to_shipstation(delivery_note.name)
				
				if result.get("success"):
					delivery_note.add_comment(
						comment_type="Info",
						text=f"Order sent to ShipStation (V1). Order ID: {result.get('order_id')}"
					)
					frappe.log_error(
						message=f"Delivery Note {delivery_note.name} sent to ShipStation V1",
						title="ShipStation V1 Success (Fallback)"
					)
				else:
					frappe.log_error(
						message=f"Failed to send {delivery_note.name} to ShipStation V1: {result.get('error')}",
						title="ShipStation V1 Error"
					)
			else:
				frappe.log_error(
					message="ShipStation API key not configured for this store",
					title="ShipStation Configuration Missing"
				)
	except ImportError as e:
		# Module not found
		frappe.log_error(
			message=f"ShipStation module import error: {str(e)}",
			title="ShipStation Import Error"
		)
	except Exception as e:
		# Log any other errors but don't fail the delivery note creation
		frappe.log_error(
			message=f"Error sending to ShipStation: {str(e)}",
			title="ShipStation Integration Error"
		)
