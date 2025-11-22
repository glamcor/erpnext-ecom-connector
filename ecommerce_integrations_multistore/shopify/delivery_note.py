"""Delivery Note hooks for Shopify integration."""

import frappe
from frappe import _


def on_delivery_note_update_after_submit(doc, method=None):
	"""Handle Delivery Note updates after submission.
	
	When workflow_state changes to 'Shipped' and tracking is entered manually,
	update the Shopify order with tracking information.
	
	Args:
		doc: Delivery Note document
		method: Hook method name
	"""
	# Check if workflow state changed to "Shipped"
	if doc.workflow_state != "Shipped":
		return
	
	# Check if this is a Shopify order
	shopify_order_id = doc.get("shopify_order_id")
	shopify_store = doc.get("shopify_store")
	
	if not shopify_order_id or not shopify_store:
		# Not a Shopify order - skip
		frappe.log_error(
			message=f"DN {doc.name} marked as Shipped but not a Shopify order. Skipping Shopify update.",
			title="Manual Shipping - Non-Shopify Order"
		)
		return
	
	# Check if tracking info is present
	tracking_number = doc.get("custom_shipstation_tracking_number")
	carrier = doc.get("custom_shipstation_carrier")
	
	if not tracking_number:
		frappe.log_error(
			message=f"DN {doc.name} marked as Shipped but no tracking number. Cannot update Shopify.",
			title="Manual Shipping - Missing Tracking"
		)
		return
	
	frappe.log_error(
		message=f"DN {doc.name} marked as Shipped with tracking {tracking_number}. Updating Shopify order {shopify_order_id} in store {shopify_store}.",
		title="Manual Shipping - Updating Shopify"
	)
	
	# Check if order is already fulfilled in Shopify to avoid duplicate
	if is_shopify_order_fulfilled(shopify_order_id, shopify_store):
		frappe.log_error(
			message=f"Shopify order {shopify_order_id} is already fulfilled. Skipping update.",
			title="Manual Shipping - Already Fulfilled"
		)
		return
	
	# Update Shopify with tracking
	# Use the same function as ShipStation webhook
	from ecommerce_integrations_multistore.shopify.shipstation_webhook import update_shopify_with_tracking_direct
	
	try:
		result = update_shopify_with_tracking_direct(
			shopify_order_id=shopify_order_id,
			shopify_store=shopify_store,
			tracking_number=tracking_number,
			carrier=carrier
		)
		
		if result:
			frappe.log_error(
				message=f"Successfully updated Shopify order {shopify_order_id} with tracking for manual shipment",
				title="Manual Shipping - Shopify Updated"
			)
	except Exception as e:
		frappe.log_error(
			message=f"Failed to update Shopify for manual shipment: {str(e)}\n{frappe.get_traceback()}",
			title="Manual Shipping - Shopify Update Error"
		)


def is_shopify_order_fulfilled(order_id, store_name):
	"""Check if a Shopify order is already fulfilled.
	
	Args:
		order_id: Shopify order ID
		store_name: Shopify Store name
		
	Returns:
		bool: True if order is fulfilled, False otherwise
	"""
	try:
		from ecommerce_integrations_multistore.shopify.connection import temp_shopify_session
		import shopify
		
		setting = frappe.get_doc("Shopify Store", store_name)
		
		@temp_shopify_session
		def check_fulfillment_status(store_name=None):
			order = shopify.Order.find(order_id)
			if order:
				return order.fulfillment_status == "fulfilled"
			return False
		
		return check_fulfillment_status(store_name=store_name)
		
	except Exception as e:
		frappe.log_error(
			message=f"Error checking Shopify fulfillment status: {str(e)}",
			title="Shopify Fulfillment Status Check Error"
		)
		# If we can't check, assume not fulfilled (safer to attempt update)
		return False


