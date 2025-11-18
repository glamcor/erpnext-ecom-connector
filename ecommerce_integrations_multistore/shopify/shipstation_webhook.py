"""ShipStation Webhook Handler for receiving tracking updates."""

import frappe
from frappe.utils import flt, cstr
import json

from ecommerce_integrations_multistore.shopify.constants import STORE_LINK_FIELD


def fetch_shipment_from_url(resource_url):
	"""Fetch shipment data from ShipStation resource_url.
	
	Args:
		resource_url: URL to fetch shipment data from
		
	Returns:
		dict: Shipment data or None if fetch fails
	"""
	try:
		# Get an enabled Shopify Store with ShipStation configured
		stores = frappe.get_all(
			"Shopify Store",
			filters={"enabled": 1, "shipstation_enabled": 1},
			limit=1
		)
		
		if not stores:
			frappe.log_error(
				message="No enabled store with ShipStation configured to fetch API key",
				title="ShipStation Webhook - No Store"
			)
			return None
		
		setting = frappe.get_doc("Shopify Store", stores[0].name)
		api_key = setting.get_password("shipstation_api_key")
		
		if not api_key:
			frappe.log_error(
				message=f"No API key configured for store {setting.name}",
				title="ShipStation Webhook - No API Key"
			)
			return None
		
		# Fetch shipment data from resource_url
		import requests
		
		headers = {
			"API-Key": api_key,
			"Accept": "application/json"
		}
		
		response = requests.get(resource_url, headers=headers, timeout=10)
		response.raise_for_status()
		
		data = response.json()
		
		frappe.log_error(
			message=f"Fetched shipment data from {resource_url}:\n{frappe.as_json(data, indent=2)}",
			title="ShipStation Webhook - Fetched Data"
		)
		
		# ShipStation returns {"shipments": [...]} or single shipment
		if "shipments" in data and data["shipments"]:
			return data["shipments"][0]
		else:
			return data
			
	except Exception as e:
		frappe.log_error(
			message=f"Error fetching shipment from {resource_url}: {str(e)}\n{frappe.get_traceback()}",
			title="ShipStation Webhook - Fetch Error"
		)
		return None


@frappe.whitelist(allow_guest=True)
def handle_shipstation_webhook():
	"""Handle incoming webhooks from ShipStation.
	
	ShipStation V2 sends webhooks for events like:
	- (V2) On Fulfillment Shipped: When label is created and shipment is shipped
	- (V2) On Fulfillment Delivered: When shipment is delivered
	
	V2 Webhook payload contains:
	- fulfillment object with shipment details
	- tracking_number
	- carrier_id and service_code
	- shipping_cost (label cost)
	- shipment_id
	- external_shipment_id (our Delivery Note number)
	"""
	try:
		# Get webhook data from request
		webhook_data = frappe.local.form_dict
		
		# Log the raw webhook for debugging
		frappe.log_error(
			message=f"ShipStation V2 Webhook Received:\n{frappe.as_json(webhook_data, indent=2)}",
			title="ShipStation V2 Webhook - Raw Data"
		)
		
		# Check if this is a V1 or V2 webhook
		resource_type = webhook_data.get("resource_type")
		resource_url = webhook_data.get("resource_url")
		
		if resource_type and resource_url:
			# Standard webhook format - fetch shipment data from resource_url
			frappe.log_error(
				message=f"Webhook sent resource_url: {resource_url}. Fetching shipment data...",
				title="ShipStation Webhook - Fetching Data"
			)
			
			# Fetch shipment data from the resource_url
			# We need the API key to fetch, so we'll look up a store that has ShipStation enabled
			shipment_data = fetch_shipment_from_url(resource_url)
			
			if shipment_data:
				handle_shipment_shipped(shipment_data)
			else:
				frappe.log_error(
					message=f"Failed to fetch shipment data from {resource_url}",
					title="ShipStation Webhook - Fetch Failed"
				)
			
			return {"status": "success"}
		
		# V2 webhook - contains data directly
		# Check for nested data structures
		fulfillment = webhook_data.get("fulfillment")
		shipment = webhook_data.get("shipment")
		resource = webhook_data.get("resource")
		
		# Process the most specific object available, or the root data
		data_to_process = fulfillment or shipment or resource or webhook_data
		
		handle_shipment_shipped(data_to_process)
		
		return {"status": "success"}
		
	except Exception as e:
		frappe.log_error(
			message=f"Error processing ShipStation webhook: {str(e)}\n{frappe.get_traceback()}",
			title="ShipStation Webhook Error"
		)
		return {"status": "error", "message": str(e)}


def handle_shipment_by_id(shipment_id):
	"""Handle shipment notification when we only have the ShipStation shipment ID.
	
	This is used when ShipStation sends V1-style webhooks with just a URL.
	We look up the Delivery Note by the shipment ID we stored earlier.
	
	Args:
		shipment_id: ShipStation shipment ID (numeric, from V1 API)
	"""
	try:
		# Convert to se- format if needed
		if not shipment_id.startswith("se-"):
			shipment_id = f"se-{shipment_id}"
		
		frappe.log_error(
			message=f"Looking up Delivery Note by shipment ID: {shipment_id}",
			title="ShipStation Webhook - DN Lookup"
		)
		
		# Find Delivery Note by shipstation_shipment_id
		delivery_note = frappe.db.get_value(
			"Delivery Note",
			{"custom_shipstation_shipment_id": shipment_id},
			"name"
		)
		
		if not delivery_note:
			# Try without custom_ prefix in case field was created differently
			delivery_note = frappe.db.get_value(
				"Delivery Note",
				{"shipstation_shipment_id": shipment_id},
				"name"
			)
		
		if delivery_note:
			dn = frappe.get_doc("Delivery Note", delivery_note)
			
			# Since we don't have tracking/carrier from V1 webhook,
			# just mark that ShipStation notified us of shipment
			dn.add_comment(
				comment_type="Info",
				text=f"ShipStation notified shipment shipped. Shipment ID: {shipment_id}"
			)
			
			frappe.log_error(
				message=f"Updated Delivery Note {delivery_note} - shipment {shipment_id} was shipped (V1 webhook - no tracking data)",
				title="ShipStation Shipment Notification"
			)
			
			frappe.db.commit()
		else:
			frappe.log_error(
				message=f"No Delivery Note found with shipstation_shipment_id: {shipment_id}",
				title="ShipStation Webhook - DN Not Found"
			)
			
	except Exception as e:
		frappe.log_error(
			message=f"Error handling shipment by ID {shipment_id}: {str(e)}\n{frappe.get_traceback()}",
			title="ShipStation Webhook ID Lookup Error"
		)


def handle_shipment_shipped(webhook_data):
	"""Process shipment shipped event from ShipStation.
	
	Updates the Delivery Note with tracking information.
	
	Args:
		webhook_data: Webhook payload from ShipStation
	"""
	try:
		# Extract shipment data - handle both V1/V2 API response formats
		# ShipStation API returns camelCase fields
		
		# Shipment ID (se-XXXXX or numeric)
		shipment_id = (
			webhook_data.get("shipment_id") or
			webhook_data.get("shipmentId")
		)
		
		# Tracking number
		tracking_number = (
			webhook_data.get("tracking_number") or
			webhook_data.get("trackingNumber")
		)
		
		# Carrier code
		carrier_code = (
			webhook_data.get("carrier_code") or
			webhook_data.get("carrierCode") or
			webhook_data.get("serviceCode")
		)
		
		# Shipping cost
		shipping_cost = (
			webhook_data.get("shipping_cost") or
			webhook_data.get("shipment_cost") or
			webhook_data.get("shipmentCost")
		)
		
		# External shipment ID (our DN number)
		# Can be in advancedOptions or at root level
		external_shipment_id = webhook_data.get("external_shipment_id")
		if not external_shipment_id:
			advanced_options = webhook_data.get("advancedOptions", {})
			external_shipment_id = advanced_options.get("customField1") or advanced_options.get("customField2") or advanced_options.get("customField3")
		
		frappe.log_error(
			message=f"Shipment Shipped:\nShipment ID: {shipment_id}\nExternal ID: {external_shipment_id}\nTracking: {tracking_number}\nCarrier: {carrier_code}\nCost: {shipping_cost}",
			title="ShipStation Shipment Shipped"
		)
		
		# Find the Delivery Note by shipstation_shipment_id or external_shipment_id
		delivery_note = None
		
		if shipment_id:
			delivery_note = frappe.db.get_value(
				"Delivery Note",
				{"shipstation_shipment_id": shipment_id},
				"name"
			)
		
		if not delivery_note and external_shipment_id:
			# Fallback to external ID (our Delivery Note name)
			if frappe.db.exists("Delivery Note", external_shipment_id):
				delivery_note = external_shipment_id
		
		if not delivery_note:
			frappe.log_error(
				message=f"Delivery Note not found for ShipStation shipment {shipment_id} / {external_shipment_id}",
				title="ShipStation Webhook - DN Not Found"
			)
			return
		
		# Update the Delivery Note with tracking information
		dn = frappe.get_doc("Delivery Note", delivery_note)
		
		# Set tracking fields (using custom_ prefix as they're created via Custom Field)
		if tracking_number:
			dn.db_set("custom_shipstation_tracking_number", tracking_number, update_modified=False)
		
		if carrier_code:
			dn.db_set("custom_shipstation_carrier", carrier_code, update_modified=False)
		
		if shipping_cost:
			dn.db_set("custom_shipstation_shipping_cost", flt(shipping_cost), update_modified=False)
		
		# Add comment with tracking info
		dn.add_comment(
			comment_type="Info",
			text=f"Shipped via {carrier_code or 'carrier'}. Tracking: {tracking_number or 'N/A'}. Cost: ${flt(shipping_cost) if shipping_cost else 'N/A'}"
		)
		
		frappe.log_error(
			message=f"Updated Delivery Note {delivery_note} with tracking:\nTracking: {tracking_number}\nCarrier: {carrier_code}\nCost: ${shipping_cost}",
			title="ShipStation Tracking Updated"
		)
		
		# Optional: Update Shopify with tracking info
		update_shopify_with_tracking(dn, tracking_number, carrier_code)
		
		frappe.db.commit()
		
	except Exception as e:
		frappe.log_error(
			message=f"Error handling shipment shipped webhook: {str(e)}\n{frappe.get_traceback()}",
			title="ShipStation Webhook Processing Error"
		)
		raise


def update_shopify_with_tracking(delivery_note, tracking_number, carrier_code):
	"""Update Shopify order with tracking information.
	
	Args:
		delivery_note: Delivery Note document
		tracking_number: Tracking number from ShipStation
		carrier_code: Carrier code from ShipStation
	"""
	try:
		# Get Shopify order ID from Delivery Note
		shopify_order_id = delivery_note.get("shopify_order_id")
		store_name = delivery_note.get(STORE_LINK_FIELD)
		
		if not shopify_order_id or not store_name:
			frappe.log_error(
				message=f"Missing Shopify order ID or store name on DN {delivery_note.name}",
				title="Shopify Tracking Update - Missing Data"
			)
			return
		
		# Get store settings
		setting = frappe.get_doc("Shopify Store", store_name)
		
		# Map ShipStation carrier codes to Shopify carrier names
		carrier_mapping = {
			"ups": "UPS",
			"usps": "USPS",
			"fedex": "FedEx",
			"dhl": "DHL",
			"ups_ground_saver": "UPS"
		}
		
		shopify_carrier = carrier_mapping.get(carrier_code.lower() if carrier_code else "", carrier_code or "Other")
		
		# Create fulfillment in Shopify
		from ecommerce_integrations_multistore.shopify.connection import temp_shopify_session
		
		@temp_shopify_session
		def create_shopify_fulfillment():
			import shopify
			
			# Get the Shopify order
			order = shopify.Order.find(shopify_order_id)
			
			if not order:
				frappe.log_error(
					message=f"Shopify order {shopify_order_id} not found",
					title="Shopify Tracking Update - Order Not Found"
				)
				return
			
			# Create fulfillment with tracking
			fulfillment_data = {
				"notify_customer": True,
				"tracking_info": {
					"number": tracking_number,
					"company": shopify_carrier
				}
			}
			
			# Get line items from order
			line_items = []
			for line_item in order.line_items:
				line_items.append({
					"id": line_item.id,
					"quantity": line_item.quantity
				})
			
			fulfillment_data["line_items"] = line_items
			
			# Create the fulfillment
			fulfillment = shopify.Fulfillment(fulfillment_data)
			fulfillment.order_id = order.id
			
			if fulfillment.save():
				frappe.log_error(
					message=f"Created Shopify fulfillment for order {order.order_number} with tracking {tracking_number}",
					title="Shopify Fulfillment Created"
				)
			else:
				frappe.log_error(
					message=f"Failed to create Shopify fulfillment: {fulfillment.errors}",
					title="Shopify Fulfillment Error"
				)
		
		# Execute with Shopify session
		create_shopify_fulfillment(setting)
		
	except Exception as e:
		frappe.log_error(
			message=f"Error updating Shopify with tracking: {str(e)}\n{frappe.get_traceback()}",
			title="Shopify Tracking Update Error"
		)

