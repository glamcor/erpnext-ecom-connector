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
		
		# Extract identifiers from resource_url
		import re
		
		# Check if URL already points to labels endpoint with filters
		if "/v2/labels?" in resource_url:
			# V2 format: https://api.shipstation.com/v2/labels?batch_id=se-XXX&store_id=se-XXX
			# Just use the URL as-is to fetch labels
			frappe.log_error(
				message=f"V2 Labels URL detected: {resource_url}. Fetching directly...",
				title="ShipStation Webhook - V2 Labels URL"
			)
			labels_url = resource_url
		else:
			# V1 format: https://ssapi.shipstation.com/shipments?shipmentId=123456
			# Extract shipmentId and convert to V2 labels URL
			shipment_id_match = re.search(r'shipmentId=(\d+)', resource_url)
			if not shipment_id_match:
				frappe.log_error(
					message=f"Could not extract shipment ID from resource_url: {resource_url}",
					title="ShipStation Webhook - Invalid URL"
				)
				return None
			
			shipment_id = f"se-{shipment_id_match.group(1)}"
			labels_url = f"https://api.shipstation.com/v2/labels?shipment_id={shipment_id}"
		
		frappe.log_error(
			message=f"Fetching label data from: {labels_url}",
			title="ShipStation Webhook - Fetching Labels"
		)
		
		headers = {
			"API-Key": api_key,
			"Accept": "application/json"
		}
		
		response = requests.get(labels_url, headers=headers, timeout=10)
		response.raise_for_status()
		
		data = response.json()
		
		frappe.log_error(
			message=f"Fetched label data from {labels_url}:\n{frappe.as_json(data, indent=2)}",
			title="ShipStation Webhook - Label Data"
		)
		
		# V2 Labels API returns {"labels": [...]}
		if "labels" in data and data["labels"]:
			# Return the first label (most shipments have one label)
			return data["labels"][0]
		else:
			frappe.log_error(
				message=f"No labels found in response for shipment {shipment_id}",
				title="ShipStation Webhook - No Labels"
			)
			return None
			
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
		# Extract label data - V2 Labels API response structure
		# Label object has: tracking_number, shipment_id, external_shipment_id, shipment_cost
		
		# Shipment ID
		shipment_id = webhook_data.get("shipment_id")
		
		# Tracking number (from label)
		tracking_number = webhook_data.get("tracking_number")
		
		# Carrier and service code (from label)
		carrier_id = webhook_data.get("carrier_id")
		service_code = webhook_data.get("service_code")
		carrier_code = service_code or carrier_id  # Use service_code preferentially
		
		# Shipping cost (from label)
		shipment_cost = webhook_data.get("shipment_cost", {})
		if isinstance(shipment_cost, dict):
			shipping_cost = shipment_cost.get("amount")
		else:
			shipping_cost = shipment_cost
		
		# External shipment ID (our DN number)
		external_shipment_id = webhook_data.get("external_shipment_id")
		
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
		
		# Set tracking fields (use existing shipstation field names)
		if tracking_number:
			dn.db_set("custom_shipstation_tracking_number", tracking_number, update_modified=False)
		
		if carrier_code:
			dn.db_set("custom_shipstation_carrier", carrier_code, update_modified=False)
		
		if shipping_cost:
			dn.db_set("custom_shipstation_shipping_cost", flt(shipping_cost), update_modified=False)
		
		# Set workflow state to "Shipped" (ShipStation shipments are shipped when label created)
		dn.db_set("workflow_state", "Shipped", update_modified=False)
		
		# Add comment with tracking info
		dn.add_comment(
			comment_type="Info",
			text=f"Shipped via {carrier_code or 'carrier'}. Tracking: {tracking_number or 'N/A'}. Cost: ${flt(shipping_cost) if shipping_cost else 'N/A'}"
		)
		
		frappe.log_error(
			message=f"Updated Delivery Note {delivery_note} with tracking:\nTracking: {tracking_number}\nCarrier: {carrier_code}\nCost: ${shipping_cost}",
			title="ShipStation Tracking Updated"
		)
		
		# Update Shopify with tracking info
		shopify_order_id = dn.get("shopify_order_id")
		shopify_store = dn.get(STORE_LINK_FIELD)
		
		if shopify_order_id and shopify_store:
			update_shopify_with_tracking_direct(
				shopify_order_id=shopify_order_id,
				shopify_store=shopify_store,
				tracking_number=tracking_number,
				carrier=carrier_code
			)
		
		frappe.db.commit()
		
	except Exception as e:
		frappe.log_error(
			message=f"Error handling shipment shipped webhook: {str(e)}\n{frappe.get_traceback()}",
			title="ShipStation Webhook Processing Error"
		)
		raise


def update_shopify_with_tracking_direct(shopify_order_id, shopify_store, tracking_number, carrier):
	"""Update Shopify order with tracking information (direct parameters).
	
	Args:
		shopify_order_id: Shopify order ID
		shopify_store: Shopify Store name
		tracking_number: Tracking number
		carrier: Carrier code/name
	
	Returns:
		bool: True if successful, False otherwise
	"""
	try:
		frappe.log_error(
			message=f"Updating Shopify order {shopify_order_id} in store {shopify_store} with tracking {tracking_number}",
			title="Shopify Update - Direct Call"
		)
		
		# Get store settings
		setting = frappe.get_doc("Shopify Store", shopify_store)
		
		# Map carrier codes to Shopify carrier names
		carrier_mapping = {
			"ups": "UPS",
			"usps": "USPS",
			"usps_ground_advantage": "USPS",
			"fedex": "FedEx",
			"dhl": "DHL",
			"dhl_express": "DHL Express",
			"ups_ground_saver": "UPS"
		}
		
		shopify_carrier = carrier_mapping.get(carrier.lower() if carrier else "", carrier or "Other")
		
		# Update Shopify via fulfillment API
		result = create_shopify_fulfillment_v2(
			setting=setting,
			order_id=shopify_order_id,
			tracking_number=tracking_number,
			carrier=shopify_carrier
		)
		
		return result
		
	except Exception as e:
		frappe.log_error(
			message=f"Error in update_shopify_with_tracking_direct: {str(e)}\n{frappe.get_traceback()}",
			title="Shopify Update Direct - Error"
		)
		return False


def create_shopify_fulfillment_v2(setting, order_id, tracking_number, carrier):
	"""Create Shopify fulfillment using 2025-01 API format.
	
	Args:
		setting: Shopify Store document
		order_id: Shopify order ID
		tracking_number: Tracking number
		carrier: Carrier name for Shopify
		
	Returns:
		bool: True if successful, False otherwise
	"""
	try:
		frappe.log_error(
			message=f"Creating Shopify fulfillment for order {order_id} in store {setting.name}",
			title="Shopify Fulfillment - Start"
		)
		
		# Create fulfillment in Shopify
		from ecommerce_integrations_multistore.shopify.connection import temp_shopify_session
		
		@temp_shopify_session
		def create_fulfillment(store_name=None):
			try:
				frappe.log_error(
					message=f"Inside Shopify session, about to import shopify module",
					title="Shopify Update - In Session"
				)
				
				import shopify
				
				frappe.log_error(
					message=f"Shopify module imported, finding order {order_id}",
					title="Shopify Update - Finding Order"
				)
				
				# Get the Shopify order
				order = shopify.Order.find(order_id)
				
				frappe.log_error(
					message=f"Order found: {order.order_number if order else 'None'}",
					title="Shopify Update - Order Found"
				)
				
				if not order:
					frappe.log_error(
						message=f"Shopify order {shopify_order_id} not found",
						title="Shopify Tracking Update - Order Not Found"
					)
					return
			except Exception as session_error:
				frappe.log_error(
					message=f"Error in Shopify session: {str(session_error)}\n{frappe.get_traceback()}",
					title="Shopify Update - Session Error"
				)
				raise
			
			# Create fulfillment using shopify library's built-in method
			try:
				frappe.log_error(
					message=f"Creating fulfillment for order {order.id} with tracking {tracking_number}",
					title="Shopify Update - Creating Fulfillment"
				)
				
				# Check if order is already fulfilled
				if order.fulfillment_status == "fulfilled":
					frappe.log_error(
						message=f"Order {order.order_number} is already fulfilled. Skipping.",
						title="Shopify Update - Already Fulfilled"
					)
					return
				
				# Step 1: Get Fulfillment Orders for this order (required for 2025-01 API)
				frappe.log_error(
					message=f"Fetching fulfillment orders for order {order.id}",
					title="Shopify Update - Getting FO"
				)
				
				# Use REST API to get fulfillment orders (shopify library may not support this)
				import requests
				shop_url = setting.shopify_url
				headers = {
					"X-Shopify-Access-Token": setting.get_password("password"),
					"Content-Type": "application/json"
				}
				
				fo_url = f"https://{shop_url}/admin/api/2025-01/orders/{order.id}/fulfillment_orders.json"
				fo_response = requests.get(fo_url, headers=headers, timeout=10)
				
				frappe.log_error(
					message=f"Fulfillment Orders API response: {fo_response.status_code} - {fo_response.text[:500]}",
					title="Shopify Update - FO Response"
				)
				
				if fo_response.status_code != 200:
					frappe.log_error(
						message=f"Failed to get fulfillment orders: {fo_response.status_code}",
						title="Shopify Update - FO Failed"
					)
					return
				
				fo_data = fo_response.json()
				fulfillment_orders = fo_data.get("fulfillment_orders", [])
				
				if not fulfillment_orders:
					frappe.log_error(
						message=f"No fulfillment orders found for order {order.order_number}",
						title="Shopify Update - No FO"
					)
					return
				
				# Get the first fulfillment order (typical case: single FO per order)
				fo_id = fulfillment_orders[0].get("id")
				
				frappe.log_error(
					message=f"Using fulfillment order ID: {fo_id}",
					title="Shopify Update - FO ID"
				)
				
				# Step 2: Create fulfillment using 2025-01 format
				# Use line_items_by_fulfillment_order (not line_items)
				fulfillment_payload = {
					"fulfillment": {
						"line_items_by_fulfillment_order": [
							{
								"fulfillment_order_id": fo_id
								# Omitting fulfillment_order_line_items = fulfill all items
							}
						],
						"tracking_info": {
							"number": tracking_number,
							"company": carrier
						},
						"notify_customer": True
					}
				}
				
				# Log the exact 2025-01 format payload
				frappe.log_error(
					message=f"Fulfillment payload (2025-01 format):\n{frappe.as_json(fulfillment_payload, indent=2)}\n\nEndpoint: POST /admin/api/2025-01/fulfillments.json",
					title="Shopify Update - Exact Payload"
				)
				
				# Step 3: POST to the modern fulfillments endpoint (not orders/{id}/fulfillments)
				fulfillment_url = f"https://{shop_url}/admin/api/2025-01/fulfillments.json"
				
				fulfillment_response = requests.post(
					fulfillment_url,
					headers=headers,
					json=fulfillment_payload,
					timeout=10
				)
				
				frappe.log_error(
					message=f"Shopify fulfillment response: {fulfillment_response.status_code} - {fulfillment_response.text}",
					title="Shopify Update - Fulfillment Response"
				)
				
				if fulfillment_response.status_code in [200, 201]:
					result_data = fulfillment_response.json()
					frappe.log_error(
						message=f"Successfully created Shopify fulfillment for order {order.order_number} with tracking {tracking_number}\n\nResponse: {frappe.as_json(result_data, indent=2)}",
						title="Shopify Fulfillment Created"
					)
				else:
					frappe.log_error(
						message=f"Failed to create fulfillment. Status: {fulfillment_response.status_code}, Response: {fulfillment_response.text}",
						title="Shopify Fulfillment Failed"
					)
					
			except Exception as fulfillment_error:
				frappe.log_error(
					message=f"Exception creating fulfillment: {str(fulfillment_error)}\n{frappe.get_traceback()}",
					title="Shopify Fulfillment Exception"
				)
			
			return
			
			if fulfillment and not hasattr(fulfillment, 'errors'):
				frappe.log_error(
					message=f"Created Shopify fulfillment for order {order.order_number} with tracking {tracking_number}",
					title="Shopify Fulfillment Created"
				)
			else:
				error_msg = fulfillment.errors.full_messages() if hasattr(fulfillment, 'errors') else "Unknown error"
				frappe.log_error(
					message=f"Failed to create Shopify fulfillment: {error_msg}",
					title="Shopify Fulfillment Error"
				)
		
		# Execute with Shopify session (must pass store_name as kwarg)
		frappe.log_error(
			message=f"Calling create_fulfillment with store_name={setting.name}",
			title="Shopify Update - Calling Function"
		)
		return create_fulfillment(store_name=setting.name)
		
	except Exception as e:
		frappe.log_error(
			message=f"Error updating Shopify with tracking: {str(e)}\n{frappe.get_traceback()}",
			title="Shopify Tracking Update Error"
		)

