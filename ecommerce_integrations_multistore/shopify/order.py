import json
from typing import Literal, Optional

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate
from shopify.collection import PaginatedIterator
from shopify.resources import Order

from ecommerce_integrations_multistore.shopify.connection import temp_shopify_session
from ecommerce_integrations_multistore.shopify.constants import (
	CUSTOMER_ID_FIELD,
	EVENT_MAPPER,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	PAYMENT_CAPTURE_DATE_FIELD,
	PAYMENT_GATEWAY_FIELD,
	SETTING_DOCTYPE,
	SOURCE_NAME_FIELD,
	STORE_DOCTYPE,
	STORE_LINK_FIELD,
)
from ecommerce_integrations_multistore.shopify.customer import ShopifyCustomer
from ecommerce_integrations_multistore.shopify.product import create_items_if_not_exist, get_item_code
from ecommerce_integrations_multistore.shopify.utils import create_shopify_log
from ecommerce_integrations_multistore.utils.price_list import get_dummy_price_list
from ecommerce_integrations_multistore.utils.taxation import get_dummy_tax_category

DEFAULT_TAX_FIELDS = {
	"sales_tax": "default_sales_tax_account",
	"shipping": "default_shipping_charges_account",
}


def is_complete_order(shopify_order):
	"""Check if order has complete customer information.
	
	For TikTok orders: Must have real shipping name and address.
	For other orders: Must have address.
	"""
	# Check shipping address exists and has street address
	shipping = shopify_order.get("shipping_address")
	if not shipping or not shipping.get("address1"):
		return False
	
	# Check if this is a TikTok order
	customer = shopify_order.get("customer", {})
	email = customer.get("email", "")
	is_tiktok_order = "@tiktokw.us" in email.lower() if email else False
	
	if is_tiktok_order:
		# For TikTok orders, check if we have a real shipping name
		shipping_first = shipping.get("first_name", "").strip()
		shipping_last = shipping.get("last_name", "").strip()
		
		# Check if name is not masked (contains asterisks) or empty
		if not shipping_first or not shipping_last:
			return False
		if "*" in shipping_first or "*" in shipping_last:
			return False
		
		# Check if the shipping name is different from email prefix
		shipping_name = f"{shipping_first} {shipping_last}"
		email_prefix = email.split('@')[0] if email else ""
		if shipping_name.lower() == email_prefix.lower():
			return False
	
	# Order is complete
	return True


def sync_sales_order(payload, request_id=None, store_name=None):
	"""Sync sales order from Shopify webhook to ERPNext.
	
	Creates a draft Sales Invoice instead of Sales Order.
	
	Args:
	    payload: Shopify order data
	    request_id: Integration log ID
	    store_name: Shopify Store name (multi-store support)
	"""
	order = payload
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	# Check order cutoff date - ignore orders created before the cutoff
	if store_name:
		cutoff_date = frappe.db.get_value(STORE_DOCTYPE, store_name, "order_cutoff_date")
		if cutoff_date:
			order_created_at = get_datetime(order.get("created_at"))
			# Strip timezone info for comparison (Shopify sends timezone-aware, DB stores naive)
			if order_created_at and hasattr(order_created_at, 'replace'):
				order_created_at = order_created_at.replace(tzinfo=None)
			if order_created_at and order_created_at < cutoff_date:
				create_shopify_log(
					status="Skipped",
					message=f"Order {order.get('name')} created at {order_created_at} is before cutoff date {cutoff_date}. Skipping.",
					store_name=store_name
				)
				return

	# Check if invoice already exists for this store
	existing_invoice_filters = {ORDER_ID_FIELD: cstr(order["id"])}
	if store_name:
		existing_invoice_filters[STORE_LINK_FIELD] = store_name
	
	if frappe.db.get_value("Sales Invoice", filters=existing_invoice_filters):
		create_shopify_log(
			status="Invalid", 
			message=f"Sales invoice already exists for order {order.get('name')} in store {store_name}, not synced",
			store_name=store_name
		)
		return
	
	# Check if order is complete
	if not is_complete_order(order):
		frappe.log_error(
			message=f"Order marked as incomplete: {order.get('name')}\nCustomer email: {order.get('customer', {}).get('email')}\nHas shipping address: {bool(order.get('shipping_address'))}",
			title="Incomplete Order Detection"
		)
		# Update the log status to Incomplete Order
		if frappe.flags.request_id:
			try:
				log_doc = frappe.get_doc("Ecommerce Integration Log", frappe.flags.request_id)
				log_doc.status = "Incomplete Order"
				log_doc.message = f"Order {order.get('name')} is incomplete (missing address or customer info). Waiting for update."
				log_doc.save(ignore_permissions=True)
				frappe.db.commit()
			except Exception as log_error:
				frappe.log_error(
					message=f"Failed to update incomplete order log status: {str(log_error)}",
					title="Log Update Error"
		)
		return
	try:
		# Get store-specific settings
		if store_name:
			store = frappe.get_doc(STORE_DOCTYPE, store_name)
		else:
			# Backward compatibility: fall back to singleton
			store = frappe.get_doc(SETTING_DOCTYPE)
		
		# Sync customer with store context
		shopify_customer = order.get("customer") if order.get("customer") is not None else {}
		shopify_customer["billing_address"] = order.get("billing_address", "")
		shopify_customer["shipping_address"] = order.get("shipping_address", "")
		customer_id = shopify_customer.get("id")
		if customer_id:
			customer = ShopifyCustomer(customer_id=customer_id, store_name=store_name)
			if not customer.is_synced():
				customer.sync_customer(customer=shopify_customer)
			else:
				customer.update_existing_addresses(shopify_customer)

		# Sync items with store context
		create_items_if_not_exist(order, store_name=store_name)

		# Create invoice and verify it was actually created
		result = create_sales_invoice(order, store)
		
		# Debug logging
		frappe.log_error(
			message=f"create_sales_invoice result: {result}, Order ID: {order.get('id')}, Order Name: {order.get('name')}",
			title=f"Shopify Invoice Creation Debug"
		)
		
		# CRITICAL: Verify invoice was created
		if not result:
			raise Exception(f"Invoice creation failed - no result returned for Shopify order {order.get('name')}")
		
	except Exception as e:
		# Log the error first
		frappe.log_error(
			message=f"Error processing order {order.get('name', 'Unknown')}: {str(e)}\nOrder ID: {order.get('id')}\nStore: {store_name}\nTraceback: {frappe.get_traceback()}",
			title="Shopify Order Sync Error"
		)
		
		# Update the log status without rollback first
		if frappe.flags.request_id:
			try:
				# Commit any pending changes first
				frappe.db.commit()
				# Now update the log status
				log_doc = frappe.get_doc("Ecommerce Integration Log", frappe.flags.request_id)
				log_doc.status = "Error"
				log_doc.message = str(e)
				log_doc.traceback = frappe.get_traceback()
				log_doc.save(ignore_permissions=True)
				frappe.db.commit()
			except Exception as log_error:
				frappe.log_error(
					message=f"Failed to update log status: {str(log_error)}",
					title="Log Update Error"
				)
		return
	
	# Success case - update the log status
	if frappe.flags.request_id:
		try:
			log_doc = frappe.get_doc("Ecommerce Integration Log", frappe.flags.request_id)
			log_doc.status = "Success"
			log_doc.message = f"Sales Invoice created for order {order.get('name')}"
			log_doc.save(ignore_permissions=True)
			frappe.db.commit()
		except Exception as log_error:
			frappe.log_error(
				message=f"Failed to update success log status: {str(log_error)}",
				title="Log Update Error"
			)


def update_draft_invoice(invoice_name, shopify_order, store_name, retry_count=0):
	"""Update a draft Sales Invoice with new data from Shopify order.
	
	Args:
	    invoice_name: Name of the Sales Invoice to update
	    shopify_order: Updated Shopify order data
	    store_name: Store name for multi-store support
	    retry_count: Number of retries for handling concurrent modifications
	"""
	# Handle concurrent modifications with retries
	max_retries = 5  # Increased from 3 to 5
	if retry_count >= max_retries:
		frappe.log_error(
			message=f"Failed to update invoice {invoice_name} after {max_retries} retries due to concurrent modifications. Invoice may be locked by another process.",
			title="Invoice Update Failed - Max Retries"
		)
		# Don't fail completely - the invoice exists, just couldn't update it
		return frappe.get_doc("Sales Invoice", invoice_name)
	
	try:
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
		
		# Only update if it's still a draft
		if invoice.docstatus != 0:
			frappe.throw("Cannot update submitted or cancelled invoice")
		
		# Get the store settings
		setting = frappe.get_doc(STORE_DOCTYPE, store_name) if store_name else None
		if not setting:
			frappe.throw(f"Store settings not found for {store_name}")
		
		# Preserve original dates before clearing items
		original_posting_date = invoice.posting_date
		original_due_date = invoice.due_date
		
		# Clear existing items and taxes
		invoice.items = []
		invoice.taxes = []
		
		# Re-process items with updated quantities and prices
		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
			store_name=store_name,
			shopify_order=shopify_order,
		)
		
		if not items:
			frappe.throw("No items found in updated order")
		
		# Get all channel-specific settings
		channel_settings = _get_channel_settings(shopify_order, setting)
		cost_center = channel_settings.get("cost_center") or setting.cost_center
		
		# Re-process taxes with channel settings (includes tax/shipping accounts and accounting classes)
		taxes = get_order_taxes(shopify_order, setting, items, store_name=store_name, channel_settings=channel_settings)
		
		# Add items and taxes using append method to create proper child documents
		for item in items:
			# Apply channel-specific cost center to items (accounting_class already set from Item master in get_order_items)
			if cost_center:
				item["cost_center"] = cost_center
			invoice.append("items", item)
		
		for tax in taxes:
			# Tax accounting classes are already set in get_order_taxes based on channel_settings
			invoice.append("taxes", tax)
		
		# Update financial status
		invoice.set(ORDER_STATUS_FIELD, shopify_order.get("financial_status"))
		
		# Update remarks if note changed
		invoice.remarks = shopify_order.get("note") or ""
		
		# Restore original dates (clearing items might have reset them)
		invoice.posting_date = original_posting_date
		invoice.due_date = original_due_date
		
		# Ensure due date is not before posting date
		if getdate(invoice.due_date) < getdate(invoice.posting_date):
			invoice.due_date = invoice.posting_date
			frappe.log_error(
				message=f"Adjusted due date to match posting date: {invoice.posting_date}",
				title="Due Date Adjustment"
			)
		
		# Update totals
		invoice.run_method("calculate_taxes_and_totals")
		
		# Final date check after calculate_taxes_and_totals (it might change dates)
		if getdate(invoice.due_date) < getdate(invoice.posting_date):
			invoice.due_date = invoice.posting_date
		
		# Update tags
		if shopify_order.get("tags"):
			_sync_order_tags(invoice, shopify_order.get("tags"))
		
		# Save the updated invoice with validation bypass for date issues
		try:
			invoice.save(ignore_permissions=True)
		except frappe.ValidationError as e:
			if "Due Date cannot be before" in str(e):
				# Force set due date to posting date and try again
				invoice.due_date = invoice.posting_date
				invoice.flags.ignore_validate = True
				invoice.flags.ignore_mandatory = True
				invoice.save(ignore_permissions=True)
			else:
				raise
		
		# Log the update details
		frappe.log_error(
			message=(
				f"Updated draft invoice {invoice_name}:\n"
				f"Items: {len(items)}\n"
				f"Total: {invoice.grand_total}\n"
				f"Shopify Total: {shopify_order.get('total_price')}"
			),
			title="Draft Invoice Updated"
		)
		
		return invoice
	
	except frappe.TimestampMismatchError:
		# Document was modified by another process, retry
		frappe.log_error(
			message=f"Invoice {invoice_name} was modified by another process, retrying... (attempt {retry_count + 1})",
			title="Concurrent Modification - Retrying"
		)
		# Longer delay before retry to reduce contention
		import time
		time.sleep(1.0 * (retry_count + 1))  # Exponential backoff: 1s, 2s, 3s, 4s, 5s
		return update_draft_invoice(invoice_name, shopify_order, store_name, retry_count + 1)
	except Exception as e:
		frappe.log_error(
			message=f"Failed to update draft invoice {invoice_name}: {str(e)}\n{frappe.get_traceback()}",
			title="Draft Invoice Update Error"
		)
		raise


def analyze_order_changes(shopify_order, invoice_name, store_name):
	"""Analyze what changed in the Shopify order compared to existing invoice.
	
	Returns list of change descriptions.
	"""
	changes = []
	
	try:
		# Get the existing invoice
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
		
		# Check financial status
		if invoice.get(ORDER_STATUS_FIELD) != shopify_order.get("financial_status"):
			changes.append(f"Financial status: {invoice.get(ORDER_STATUS_FIELD)} → {shopify_order.get('financial_status')}")
		
		# Check fulfillment status
		current_fulfillment = shopify_order.get("fulfillment_status") or "unfulfilled"
		if invoice.get("shopify_fulfillment_status") != current_fulfillment:
			changes.append(f"Fulfillment status → {current_fulfillment}")
		
		# Check if cancelled
		if shopify_order.get("cancelled_at") and not invoice.get("shopify_cancelled_at"):
			changes.append("Order cancelled")
		
		# Check tags
		current_tags = shopify_order.get("tags", "")
		if invoice.get("shopify_tags") != current_tags:
			changes.append(f"Tags updated")
		
		# Check note
		current_note = shopify_order.get("note") or ""
		if invoice.get("shopify_note") != current_note:
			changes.append("Note updated")
		
		# Store updated_at separately (not a "change" that requires update)
		shopify_updated = shopify_order.get("updated_at", "")
		
		# Check for refunds
		if shopify_order.get("refunds") and len(shopify_order.get("refunds", [])) > 0:
			changes.append(f"Has {len(shopify_order.get('refunds', []))} refund(s)")
		
		# Check total price changes
		shopify_total = float(shopify_order.get("total_price", 0))
		if abs(invoice.grand_total - shopify_total) > 0.01:
			changes.append(f"Total changed: {invoice.grand_total} → {shopify_total}")
		
		# Check line items for quantity/price changes
		# Build a map of Shopify items by variant_id for accurate matching
		shopify_items_by_variant = {str(item.get("variant_id")): item for item in shopify_order.get("line_items", []) if item.get("variant_id")}
		shopify_items_by_sku = {item.get("sku"): item for item in shopify_order.get("line_items", []) if item.get("sku")}
		
		# Build invoice items map with shopify variant ID from description or other fields
		invoice_items = {}
		for item in invoice.items:
			invoice_items[item.item_code] = item
			# Also check if we stored the variant_id in description or elsewhere
			if hasattr(item, 'shopify_variant_id') and item.shopify_variant_id:
				invoice_items[str(item.shopify_variant_id)] = item
		
		# Debug log the items comparison
		frappe.log_error(
			message=(
				f"Analyzing items - Shopify variants: {list(shopify_items_by_variant.keys())}, "
				f"Shopify SKUs: {list(shopify_items_by_sku.keys())}, "
				f"Invoice items: {list(invoice_items.keys())}"
			),
			title="Order Change Analysis Debug"
		)
		
		# Check for quantity/price changes by matching invoice items to Shopify items
		matched_items = set()
		for invoice_item in invoice.items:
			shopify_item = None
			item_identifier = None
			
			# Try to extract Shopify identifiers from description
			sku_from_desc = None
			variant_from_desc = None
			if invoice_item.description:
				# Parse description like "SKU: ABC123 | Variant: 12345678"
				import re
				sku_match = re.search(r'SKU:\s*([^\s|]+)', invoice_item.description)
				variant_match = re.search(r'Variant:\s*(\d+)', invoice_item.description)
				if sku_match:
					sku_from_desc = sku_match.group(1)
				if variant_match:
					variant_from_desc = variant_match.group(1)
			
			# Try to find matching Shopify item
			# First try by variant_id from description
			if variant_from_desc and variant_from_desc in shopify_items_by_variant:
				shopify_item = shopify_items_by_variant[variant_from_desc]
				item_identifier = f"variant {variant_from_desc}"
				matched_items.add(variant_from_desc)
			
			# If not found, try by SKU from description
			if not shopify_item and sku_from_desc and sku_from_desc in shopify_items_by_sku:
				shopify_item = shopify_items_by_sku[sku_from_desc]
				item_identifier = f"SKU {sku_from_desc}"
				matched_items.add(sku_from_desc)
			
			# If still not found, try by item_code (might be the SKU)
			if not shopify_item and invoice_item.item_code in shopify_items_by_sku:
				shopify_item = shopify_items_by_sku[invoice_item.item_code]
				item_identifier = f"SKU {invoice_item.item_code}"
				matched_items.add(invoice_item.item_code)
			
			# If we found a match, compare quantities and prices
			if shopify_item:
				frappe.log_error(
					message=f"Comparing {item_identifier} - Invoice qty: {invoice_item.qty}, Shopify qty: {shopify_item.get('quantity')}",
					title="Item Quantity Comparison"
				)
				
				if invoice_item.qty != shopify_item.get("quantity"):
					changes.append(f"Quantity changed for {invoice_item.item_name or invoice_item.item_code}: {invoice_item.qty} → {shopify_item.get('quantity')}")
				
				shopify_rate = float(shopify_item.get("price", 0))
				if abs(invoice_item.rate - shopify_rate) > 0.01:
					changes.append(f"Price changed for {invoice_item.item_name or invoice_item.item_code}: {invoice_item.rate} → {shopify_rate}")
			else:
				# Invoice item not found in Shopify order (item was removed)
				changes.append(f"Item removed: {invoice_item.item_name or invoice_item.item_code}")
		
		# Check for new items in Shopify that aren't in the invoice
		all_shopify_identifiers = set(shopify_items_by_variant.keys()) | set(shopify_items_by_sku.keys())
		new_items = all_shopify_identifiers - matched_items
		if new_items:
			new_item_names = []
			for identifier in new_items:
				if identifier in shopify_items_by_variant:
					item = shopify_items_by_variant[identifier]
				else:
					item = shopify_items_by_sku.get(identifier)
				if item:
					new_item_names.append(item.get("title") or item.get("sku") or identifier)
			changes.append(f"New items added: {', '.join(new_item_names)}")
			
	except Exception as e:
		changes.append(f"Error analyzing changes: {str(e)}")
	
	return changes


def handle_order_update(payload, request_id=None, store_name=None):
	"""Handle order update webhook from Shopify.
	
	Check if previously incomplete orders are now complete and process them.
	
	Args:
	    payload: Shopify order data
	    request_id: Integration log ID
	    store_name: Shopify Store name (multi-store support)
	"""
	order = payload
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id
	
	order_id = cstr(order["id"])
	
	# Check order cutoff date - ignore orders created before the cutoff
	if store_name:
		cutoff_date = frappe.db.get_value(STORE_DOCTYPE, store_name, "order_cutoff_date")
		if cutoff_date:
			order_created_at = get_datetime(order.get("created_at"))
			# Strip timezone info for comparison (Shopify sends timezone-aware, DB stores naive)
			if order_created_at and hasattr(order_created_at, 'replace'):
				order_created_at = order_created_at.replace(tzinfo=None)
			if order_created_at and order_created_at < cutoff_date:
				create_shopify_log(
					status="Skipped",
					message=f"Order update for {order.get('name')} ignored - order created at {order_created_at} is before cutoff date {cutoff_date}",
					store_name=store_name
				)
				return
	
	# Initial debug log
	frappe.log_error(
		message=f"Order update webhook received - Order: {order.get('name')}, ID: {order_id}, Store: {store_name}",
		title="Order Update Webhook"
	)
	
	# Check if we already have a Sales Invoice for this order in this store
	existing_invoice_filters = {ORDER_ID_FIELD: order_id}
	if store_name:
		existing_invoice_filters[STORE_LINK_FIELD] = store_name
		
	existing_invoice = frappe.db.get_value("Sales Invoice", filters=existing_invoice_filters, fieldname=["name", "docstatus"], as_dict=True)
	if existing_invoice:
		# Order already processed, check what changed
		changes = analyze_order_changes(order, existing_invoice.name, store_name)
		
		# Debug log
		frappe.log_error(
			message=f"Order update check - Invoice: {existing_invoice.name}, Status: {existing_invoice.docstatus}, Changes detected: {len(changes) if changes else 0}, Changes: {changes}",
			title="Order Update Debug"
		)
		
		# If invoice is still draft and there are changes, update it
		if existing_invoice.docstatus == 0 and changes:
			try:
				# Update the draft invoice with new order data
				update_draft_invoice(existing_invoice.name, order, store_name)
				create_shopify_log(
					status="Success",
					message=f"Updated draft invoice for order {order.get('name')}. Changes: {', '.join(changes)}",
					store_name=store_name
				)
			except Exception as e:
				create_shopify_log(
					status="Error",
					message=f"Failed to update draft invoice: {str(e)}",
					exception=e,
					store_name=store_name
				)
		elif existing_invoice.docstatus == 1:
			# Invoice is submitted, just log the changes
			if changes:
				create_shopify_log(
					status="Info",
					message=f"Submitted invoice exists for order {order.get('name')}. Cannot update. Changes: {', '.join(changes)}",
					store_name=store_name
				)
		else:
			# No significant changes or invoice is cancelled
			create_shopify_log(
				status="Info",
				message=f"Sales invoice exists for order {order.get('name')}. No action needed.",
				store_name=store_name
			)
		return
	
	# Check if we have this order marked as incomplete
	incomplete_log = frappe.db.get_value(
		"Ecommerce Integration Log",
		filters={
			"method": "ecommerce_integrations_multistore.shopify.order.sync_sales_order",
			"status": "Incomplete Order",
			"request_data": ["like", f'%"id": {order_id}%']
		},
		fieldname="name"
	)
	
	if incomplete_log and is_complete_order(order):
		# Order is now complete! Process it
		frappe.log_error(
			message=f"Order {order.get('name')} is now complete. Processing...",
			title="Shopify Order Now Complete"
		)
		sync_sales_order(order, request_id, store_name)
	elif not incomplete_log:
		# This might be a regular order update, check if we need to sync
		sync_sales_order(order, request_id, store_name)
	else:
		# Still incomplete
		create_shopify_log(
			status="Incomplete Order",
			message=f"Order {order.get('name')} is still incomplete. Waiting for complete customer info.",
			store_name=store_name
		)


def create_order(order, setting, company=None):
	# local import to avoid circular dependencies
	from ecommerce_integrations_multistore.shopify.fulfillment import create_delivery_note
	from ecommerce_integrations_multistore.shopify.invoice import create_sales_invoice

	so = create_sales_order(order, setting, company)
	if so:
		if order.get("financial_status") == "paid":
			create_sales_invoice(order, setting, so)

		if order.get("fulfillments"):
			create_delivery_note(order, setting, so)
	
	return so  # Return the sales order for verification


def create_sales_order(shopify_order, setting, company=None):
	"""Create Sales Order from Shopify order data.
	
	Args:
	    shopify_order: Shopify order data
	    setting: Store or Setting doc (backward compatible)
	    company: Optional company override
	"""
	customer = setting.default_customer
	store_name = setting.name if setting.doctype == STORE_DOCTYPE else None
	
	# Multi-store customer lookup
	if shopify_order.get("customer", {}):
		if customer_id := shopify_order.get("customer", {}).get("id"):
			if store_name:
				# Look up customer using multi-store child table
				customer_name = frappe.db.sql(
					"""
					SELECT parent 
					FROM `tabShopify Customer Store Link`
					WHERE store = %s AND shopify_customer_id = %s
					LIMIT 1
					""",
					(store_name, customer_id),
					as_dict=True,
				)
				if customer_name:
					customer = customer_name[0].parent
			else:
				# Backward compatibility: single-store lookup
				customer = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")

	# Check if sales order already exists for this store
	order_filters = {ORDER_ID_FIELD: shopify_order.get("id")}
	if store_name:
		order_filters[STORE_LINK_FIELD] = store_name
	so = frappe.db.get_value("Sales Order", order_filters, "name")

	if not so:
		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
			store_name=store_name,
			shopify_order=shopify_order,
		)
		
		# Debug logging for items
		frappe.log_error(
			message=f"Items found: {len(items) if items else 0}, Order: {shopify_order.get('name')}, Line items: {len(shopify_order.get('line_items', []))}",
			title="Shopify Order Items Debug"
		)

		if not items:
			message = (
				"No items could be matched in ERPNext for Shopify order {}. "
				"Line items: {}".format(
					shopify_order.get('name'),
					[{"sku": item.get('sku'), "product_id": item.get('product_id'), "title": item.get('title')} 
					 for item in shopify_order.get('line_items', [])]
				)
			)

			create_shopify_log(status="Error", exception=message, rollback=True, store_name=store_name)
			# Raise exception instead of returning empty string
			raise Exception(message)

		# Get all channel-specific settings (cost center, bank account, tax/shipping accounts, accounting classes)
		channel_settings = _get_channel_settings(shopify_order, setting)
		cost_center = channel_settings.get("cost_center") or setting.cost_center
		
		taxes = get_order_taxes(shopify_order, setting, items, store_name=store_name, channel_settings=channel_settings)
		
		# Get billing and shipping addresses
		customer_address = None
		shipping_address_name = None
		
		# Look up addresses by Shopify address ID or create address title
		if shopify_order.get("billing_address"):
			billing_addr = shopify_order.get("billing_address")
			# Try to find by Shopify address ID first
			if billing_addr.get("id"):
				customer_address = frappe.db.get_value(
					"Address",
					{"shopify_address_id": billing_addr.get("id")},
					"name"
				)
			# If not found, try by address title
			if not customer_address:
				# Create expected address title format
				address_title = f"{customer}-Billing"
				customer_address = frappe.db.get_value(
					"Address",
					{"address_title": address_title},
					"name"
				)
		
		if shopify_order.get("shipping_address"):
			shipping_addr = shopify_order.get("shipping_address")
			# Try to find by Shopify address ID first
			if shipping_addr.get("id"):
				shipping_address_name = frappe.db.get_value(
					"Address",
					{"shopify_address_id": shipping_addr.get("id")},
					"name"
				)
			# If not found, try by address title
			if not shipping_address_name:
				# Create expected address title format
				address_title = f"{customer}-Shipping"
				shipping_address_name = frappe.db.get_value(
					"Address",
					{"address_title": address_title},
					"name"
				)
		
		so_dict = {
			"doctype": "Sales Order",
			"naming_series": setting.sales_order_series or "SO-Shopify-",
			ORDER_ID_FIELD: str(shopify_order.get("id")),
			ORDER_NUMBER_FIELD: shopify_order.get("name"),
			"customer": customer,
			"customer_address": customer_address,
			"shipping_address_name": shipping_address_name,
			"transaction_date": getdate(shopify_order.get("created_at")) or nowdate(),
			"delivery_date": getdate(shopify_order.get("created_at")) or nowdate(),
			"company": setting.company,
			"selling_price_list": get_dummy_price_list(),
			"ignore_pricing_rule": 1,
			"items": items,
			"taxes": taxes,
			"tax_category": get_dummy_tax_category(),
		}
		
		# Add store reference for multi-store
		if store_name:
			so_dict[STORE_LINK_FIELD] = store_name
		
		so = frappe.get_doc(so_dict)

		if company:
			so.update({"company": company, "status": "Draft"})
		
		so.flags.ignore_mandatory = True
		so.flags.ignore_validate = True
		so.flags.ignore_validate_update_after_submit = True
		
		# Apply channel-specific cost center to all line items
		# Note: accounting_class for items comes from Item master (set in get_order_items)
		# Note: accounting_class for taxes comes from channel_settings (set in get_order_taxes)
		for item in so.items:
			if cost_center:
				item.cost_center = cost_center
		
		# Apply cost center to all tax lines (accounting_class already set in get_order_taxes)
		for tax in so.taxes:
			if cost_center:
				tax.cost_center = cost_center
		
		# Note: Bank account from channel mapping is used for financial reporting/reconciliation
		# It's tracked at the mapping level for identifying which account money flows to
		so.flags.shopiy_order_json = json.dumps(shopify_order)
		
		# Set UOM conversion factor to 1 for all items to avoid validation errors
		for item in so.items:
			if not item.conversion_factor or item.conversion_factor == 0:
				item.conversion_factor = 1.0
		
		# Calculate taxes and totals before saving
		so.calculate_taxes_and_totals()
		
		so.save(ignore_permissions=True)
		so.submit()
		
		# Verify the order was saved
		if not frappe.db.exists("Sales Order", so.name):
			raise Exception(f"Sales Order {so.name} was not saved to database")
		
		# Reload to get calculated totals
		so.reload()
		
		# Debug grand total comparison
		shopify_total = flt(shopify_order.get("current_total_price") or shopify_order.get("total_price"))
		erpnext_total = flt(so.grand_total)
		
		frappe.log_error(
			message=(
				f"Sales Order {so.name} created successfully\n"
				f"Shopify order: {shopify_order.get('name')}\n"
				f"Shopify total: {shopify_total}\n"
				f"ERPNext grand total: {erpnext_total}\n"
				f"Difference: {abs(shopify_total - erpnext_total)}\n"
				f"Taxes included: {shopify_order.get('taxes_included')}\n"
				f"Total tax: {shopify_order.get('total_tax')}\n"
				f"Total discounts: {shopify_order.get('total_discounts')}\n"
				f"Shipping: {shopify_order.get('total_shipping_price_set', {}).get('shop_money', {}).get('amount')}"
			),
			title="Order Total Comparison"
		)

		if shopify_order.get("note"):
			so.add_comment(text=f"Order Note: {shopify_order.get('note')}")
		
		# Sync Shopify tags to ERPNext native tagging system
		if shopify_order.get("tags"):
			_sync_order_tags(so, shopify_order.get("tags"))

	else:
		so = frappe.get_doc("Sales Order", so)

	return so


def create_sales_invoice(shopify_order, setting, company=None):
	"""Create draft Sales Invoice from Shopify order.
	
	Args:
	    shopify_order: Shopify order data
	    setting: Store or Setting doc (backward compatible)
	    company: Optional company override
	"""
	customer = setting.default_customer
	store_name = setting.name if setting.doctype == STORE_DOCTYPE else None
	
	# Multi-store customer lookup
	if shopify_order.get("customer", {}):
		if customer_id := shopify_order.get("customer", {}).get("id"):
			if store_name:
				# Look up customer using multi-store child table
				customer_name = frappe.db.sql(
					"""
					SELECT parent 
					FROM `tabShopify Customer Store Link`
					WHERE store = %s AND shopify_customer_id = %s
					LIMIT 1
					""",
					(store_name, customer_id),
					as_dict=True,
				)
				if customer_name:
					customer = customer_name[0].parent
			else:
				# Backward compatibility: single-store lookup
				customer = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")

	# Check if invoice already exists for this store
	invoice_filters = {ORDER_ID_FIELD: shopify_order.get("id")}
	if store_name:
		invoice_filters[STORE_LINK_FIELD] = store_name
	si = frappe.db.get_value("Sales Invoice", invoice_filters, "name")

	if not si:
		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
			store_name=store_name,
			shopify_order=shopify_order,
		)
		
		# Debug logging for items
		frappe.log_error(
			message=f"Items found: {len(items) if items else 0}, Order: {shopify_order.get('name')}, Line items: {len(shopify_order.get('line_items', []))}",
			title="Shopify Invoice Items Debug"
		)

		if not items:
			message = (
				"No items could be matched in ERPNext for Shopify order {}. "
				"Line items: {}".format(
					shopify_order.get('name'),
					[{"sku": item.get('sku'), "product_id": item.get('product_id'), "title": item.get('title')} 
					 for item in shopify_order.get('line_items', [])]
				)
			)

			create_shopify_log(status="Error", exception=message, rollback=True, store_name=store_name)
			# Raise exception instead of returning empty string
			raise Exception(message)

		# Get all channel-specific settings (cost center, bank account, tax/shipping accounts, accounting classes)
		channel_settings = _get_channel_settings(shopify_order, setting)
		cost_center = channel_settings.get("cost_center") or setting.cost_center
		
		taxes = get_order_taxes(shopify_order, setting, items, store_name=store_name, channel_settings=channel_settings)
		
		# Get billing and shipping addresses
		billing_address = None
		shipping_address = None
		
		# Look up addresses by customer link and address type (more reliable than title matching)
		if shopify_order.get("billing_address"):
			billing_addr = shopify_order.get("billing_address")
			frappe.log_error(
				message=f"Billing address data: {billing_addr}",
				title="Address Debug - Billing"
			)
			
			# Try to find by Shopify address ID first
			if billing_addr.get("id"):
				billing_address = frappe.db.get_value(
					"Address",
					{"shopify_address_id": billing_addr.get("id")},
					"name"
				)
			
			# If not found by ID, find by customer link and address_type
			if not billing_address:
				# Query the Dynamic Link child table
				billing_address = frappe.db.sql("""
					SELECT parent 
					FROM `tabDynamic Link`
					WHERE link_doctype = 'Customer' 
					AND link_name = %s
					AND parenttype = 'Address'
					AND parent IN (
						SELECT name FROM `tabAddress` WHERE address_type = 'Billing'
					)
					ORDER BY modified DESC
					LIMIT 1
				""", (customer,), as_dict=False)
				
				billing_address = billing_address[0][0] if billing_address else None
				frappe.log_error(
					message=f"Found billing address by customer link: {billing_address}",
					title="Address Lookup Debug"
				)
		
		if shopify_order.get("shipping_address"):
			shipping_addr = shopify_order.get("shipping_address")
			frappe.log_error(
				message=f"Shipping address data: {shipping_addr}",
				title="Address Debug - Shipping"
			)
			
			# Try to find by Shopify address ID first
			if shipping_addr.get("id"):
				shipping_address = frappe.db.get_value(
					"Address",
					{"shopify_address_id": shipping_addr.get("id")},
					"name"
				)
			
			# If not found by ID, find by customer link and address_type
			if not shipping_address:
				# Query the Dynamic Link child table
				shipping_address = frappe.db.sql("""
					SELECT parent 
					FROM `tabDynamic Link`
					WHERE link_doctype = 'Customer' 
					AND link_name = %s
					AND parenttype = 'Address'
					AND parent IN (
						SELECT name FROM `tabAddress` WHERE address_type = 'Shipping'
					)
					ORDER BY modified DESC
					LIMIT 1
				""", (customer,), as_dict=False)
				
				shipping_address = shipping_address[0][0] if shipping_address else None
				frappe.log_error(
					message=f"Found shipping address by customer link: {shipping_address}",
					title="Address Lookup Debug"
				)
		
		# Get default debit_to account for the company
		debit_to = frappe.get_cached_value("Company", setting.company, "default_receivable_account")
		
		# Get company currency
		currency = frappe.get_cached_value("Company", setting.company, "default_currency")
		
		# Extract payment gateway info for bank account mapping
		gateway_names = shopify_order.get("payment_gateway_names", [])
		payment_gateway = gateway_names[0] if gateway_names else shopify_order.get("gateway", "")
		source_name = shopify_order.get("source_name", "")
		
		# Get order date for posting (when order was placed)
		order_date = getdate(shopify_order.get("created_at")) or nowdate()
		
		# Get payment capture date (when money was actually captured)
		# This is important for orders with delayed capture (authorize then capture)
		from ecommerce_integrations_multistore.shopify.invoice import get_payment_capture_date
		capture_date = get_payment_capture_date(shopify_order)
		
		si_dict = {
			"doctype": "Sales Invoice",
			"naming_series": setting.sales_invoice_series or "SI-Shopify-",
			ORDER_ID_FIELD: str(shopify_order.get("id")),
			ORDER_NUMBER_FIELD: shopify_order.get("name"),
			ORDER_STATUS_FIELD: shopify_order.get("financial_status"),
			PAYMENT_GATEWAY_FIELD: payment_gateway,  # Store payment gateway for bank account mapping
			SOURCE_NAME_FIELD: source_name,  # Store sales channel for channel mapping
			PAYMENT_CAPTURE_DATE_FIELD: capture_date,  # Store capture date for payment entry
			"customer": customer,
			"customer_address": billing_address,
			"shipping_address_name": shipping_address,
			"set_posting_time": 1,  # CRITICAL: Allow manual posting date (not today's date)
			"posting_date": order_date,
			"due_date": order_date,
			"company": setting.company,
			"currency": currency or "USD",
			"price_list_currency": currency or "USD",  # Add price list currency
			"debit_to": debit_to,
			"selling_price_list": get_dummy_price_list(),
			"ignore_pricing_rule": 1,
			"items": items,
			"taxes": taxes,
			"tax_category": get_dummy_tax_category(),
			"is_pos": 0,  # Not point of sale
			"update_stock": 1,  # Update stock on submission
			"status": "Draft",  # Ensure draft status
			"remarks": shopify_order.get("note") or "",  # Add order note to remarks
		}
		
		# Add store reference for multi-store
		if store_name:
			si_dict[STORE_LINK_FIELD] = store_name
		
		si = frappe.get_doc(si_dict)

		if company:
			si.update({"company": company})
		
		si.flags.ignore_mandatory = True
		si.flags.ignore_validate = True
		si.flags.ignore_validate_update_after_submit = True
		
		# Apply channel-specific cost center to all line items
		# Note: accounting_class for items comes from Item master (set in get_order_items)
		# Note: accounting_class for taxes comes from channel_settings (set in get_order_taxes)
		for item in si.items:
			if cost_center:
				item.cost_center = cost_center
		
		# Apply cost center to all tax lines (accounting_class already set in get_order_taxes)
		for tax in si.taxes:
			if cost_center:
				tax.cost_center = cost_center
		
		# Note: Bank account from channel mapping is used for financial reporting/reconciliation
		# It's tracked at the mapping level for identifying which account money flows to
		si.flags.shopiy_order_json = json.dumps(shopify_order)
		
		# Set UOM conversion factor to 1 for all items to avoid validation errors
		for item in si.items:
			if not item.conversion_factor or item.conversion_factor == 0:
				item.conversion_factor = 1.0
		
		# Calculate taxes and totals before saving
		si.calculate_taxes_and_totals()
		
		# Save as draft - DO NOT SUBMIT
		si.save(ignore_permissions=True)
		
		# Verify the invoice was saved
		if not frappe.db.exists("Sales Invoice", si.name):
			raise Exception(f"Sales Invoice {si.name} was not saved to database")
		
		# Reload to get calculated totals
		si.reload()
		
		# Debug grand total comparison
		shopify_total = flt(shopify_order.get("current_total_price") or shopify_order.get("total_price"))
		erpnext_total = flt(si.grand_total)
		
		frappe.log_error(
			message=(
				f"Sales Invoice {si.name} created successfully (DRAFT)\n"
				f"Shopify order: {shopify_order.get('name')}\n"
				f"Shopify total: {shopify_total}\n"
				f"ERPNext grand total: {erpnext_total}\n"
				f"Difference: {abs(shopify_total - erpnext_total)}\n"
				f"Taxes included: {shopify_order.get('taxes_included')}\n"
				f"Total tax: {shopify_order.get('total_tax')}\n"
				f"Total discounts: {shopify_order.get('total_discounts')}\n"
				f"Shipping: {shopify_order.get('total_shipping_price_set', {}).get('shop_money', {}).get('amount')}"
			),
			title="Invoice Total Comparison"
		)

		# Order note is now in the remarks field, no need for separate comment
		
		# Sync Shopify tags to ERPNext native tagging system
		if shopify_order.get("tags"):
			_sync_order_tags(si, shopify_order.get("tags"))
		
		# Note: Bundle mapping now happens in get_order_items() based on shipping country
		# The assign_power_supplies() function is deprecated and no longer called
		
		# Set warehouse based on shipping destination
		set_item_warehouses(si, shopify_order, setting)
		
		# Recalculate totals
		si.calculate_taxes_and_totals()
		si.save(ignore_permissions=True)

	else:
		si = frappe.get_doc("Sales Invoice", si)

	return si


def assign_power_supplies(invoice, shopify_order):
	"""DEPRECATED: Bundle mapping now happens in get_order_items().
	
	This function previously added power supply items as separate line items.
	Now, the master SKU is replaced with the regional bundle directly in get_order_items()
	based on the shipping country, which is a cleaner approach that:
	- Prevents bundle loss on order updates
	- Uses ERPNext's native Product Bundle for component explosion
	- Keeps the invoice cleaner with just the bundle item
	
	This function is kept for backwards compatibility but does nothing.
	"""
	# DEPRECATED: Bundle mapping now happens in get_order_items()
	# This function is kept for backwards compatibility but no longer adds items
	pass


def set_item_warehouses(invoice, shopify_order, setting):
	"""Set warehouse for each item based on shipping destination and overrides.
	
	Args:
	    invoice: Sales Invoice document
	    shopify_order: Shopify order data
	    setting: Shopify Store settings
	"""
	shipping_address = shopify_order.get("shipping_address", {})
	country_code = shipping_address.get("country_code")
	
	# Default to international warehouse if no country specified
	is_us_shipment = country_code == "US"
	
	# Get default warehouses from settings
	default_warehouse = setting.us_warehouse if is_us_shipment else setting.international_warehouse
	
	if not default_warehouse:
		frappe.log_error(
			message=f"No {'US' if is_us_shipment else 'international'} warehouse configured for store {setting.name}",
			title="Warehouse Configuration Warning"
		)
		return
	
	# Get item overrides
	overrides = {}
	if hasattr(setting, 'item_warehouse_overrides'):
		for override in setting.item_warehouse_overrides:
			overrides[override.item_code] = override.warehouse
	
	# Set warehouse for each item
	for item in invoice.items:
		# Check for item-specific override first
		if item.item_code in overrides:
			item.warehouse = overrides[item.item_code]
			frappe.log_error(
				message=f"Using override warehouse {item.warehouse} for item {item.item_code}",
				title="Warehouse Override Applied"
			)
		else:
			# Use default based on shipping destination
			item.warehouse = default_warehouse
	
	# Log the warehouse assignment
	frappe.log_error(
		message=(
			f"Warehouse assignment for order {shopify_order.get('name')}:\n"
			f"Shipping to: {country_code}\n"
			f"Default warehouse: {default_warehouse}\n"
			f"Items with overrides: {list(overrides.keys())}"
		),
		title="Warehouse Assignment"
	)


def get_order_items(order_items, setting, delivery_date, taxes_inclusive, store_name=None, shopify_order=None):
	"""Get line items for Sales Order/Invoice.
	
	Args:
	    order_items: Shopify line items
	    setting: Store or Setting doc
	    delivery_date: Delivery date
	    taxes_inclusive: Whether taxes are included
	    store_name: Store name for multi-store item lookup
	    shopify_order: Full Shopify order data (needed for bundle mapping based on shipping country)
	"""
	items = []
	all_product_exists = True
	product_not_exists = []
	
	# Get power supply type for bundle mapping (based on shipping country)
	bundle_power_supply_type = None
	if shopify_order:
		shipping_address = shopify_order.get("shipping_address", {})
		country_code = shipping_address.get("country_code", "").upper()
		if country_code:
			# Check if Power Supply Mapping DocType exists
			if frappe.db.exists("DocType", "Power Supply Mapping"):
				try:
					from ecommerce_integrations_multistore.shopify.doctype.power_supply_mapping.power_supply_mapping import PowerSupplyMapping
					bundle_power_supply_type = PowerSupplyMapping.get_power_supply_for_country(country_code)
				except ImportError:
					pass

	for shopify_item in order_items:
		item_code = None
		
		# Try to get item code even if product_exists is false
		# For Duoplane and similar integrations, product_id might be null but SKU exists
		if shopify_item.get("product_exists"):
			item_code = get_item_code(shopify_item, store_name=store_name)
		
		# If standard lookup failed but we have a SKU, try direct ERPNext lookup
		if not item_code and shopify_item.get("sku"):
			# Product doesn't exist in Shopify catalog but has SKU - try to match by SKU
			# This handles Duoplane, draft orders, and custom line items
			sku = shopify_item.get("sku")
			
			# Debug log what we're searching for
			frappe.log_error(
				message=f"Direct SKU lookup: {sku}, product_exists: {shopify_item.get('product_exists')}, product_id: {shopify_item.get('product_id')}",
				title="Shopify SKU Lookup Debug"
			)
			
			# Try multiple fields where SKU might be stored
			# First try exact item_code match
			item_code = frappe.db.get_value("Item", {"item_code": sku, "disabled": 0})
			
			# Then try SKU field (for variants) - only if the field exists
			if not item_code and frappe.db.has_column("Item", "sku"):
				item_code = frappe.db.get_value("Item", {"sku": sku, "disabled": 0})
			
			# Then try barcode field
			if not item_code:
				item_code = frappe.db.get_value("Item Barcode", {"barcode": sku}, "parent")
			
			# Then try item name (less common)
			if not item_code:
				item_code = frappe.db.get_value("Item", {"item_name": sku, "disabled": 0})
			
			# Log result
			if item_code:
				frappe.log_error(
					message=f"SKU {sku} matched to item {item_code}",
					title="Shopify SKU Match Success"
				)
		
		if not item_code:
			# Item not found - track for error reporting
			frappe.log_error(
				message=f"Item not found - SKU: {shopify_item.get('sku')}, Product ID: {shopify_item.get('product_id')}, Title: {shopify_item.get('title')}",
				title="Shopify Item Match Failed"
			)
			all_product_exists = False
			product_not_exists.append(
				{"title": shopify_item.get("title"), "sku": shopify_item.get("sku"), ORDER_ID_FIELD: shopify_item.get("id")}
			)
			continue
		
		# Bundle Mapping: Check if this item has a regional bundle configured
		# If so, replace the master item with the appropriate bundle for the shipping country
		original_item_code = item_code  # Keep original for logging
		if bundle_power_supply_type and frappe.db.exists("DocType", "Product Power Supply Config"):
			bundle_config = frappe.db.get_value(
				"Product Power Supply Config",
				{"product": item_code, "enabled": 1},
				["us_power_supply", "uk_power_supply", "eu_power_supply", "au_power_supply"],
				as_dict=True
			)
			
			if bundle_config:
				# Map power supply type to field name (fields contain bundle item codes)
				bundle_field_map = {
					"US": "us_power_supply",
					"UK": "uk_power_supply", 
					"EU": "eu_power_supply",
					"AU": "au_power_supply"
				}
				
				bundle_field = bundle_field_map.get(bundle_power_supply_type)
				bundle_item = bundle_config.get(bundle_field) if bundle_field else None
				
				if bundle_item:
					# Verify the bundle item exists and is not disabled
					if frappe.db.get_value("Item", bundle_item, "disabled") == 0:
						item_code = bundle_item
						frappe.log_error(
							message=f"Bundle mapping: {original_item_code} → {bundle_item} (region: {bundle_power_supply_type})",
							title="Bundle Mapping Applied"
						)
					else:
						frappe.log_error(
							message=f"Bundle item {bundle_item} is disabled, using original item {item_code}",
							title="Bundle Mapping Warning"
						)

		# Get income account from Item, Item Group, or Company
		income_account = _get_income_account(item_code, setting.company)
		
		# Get UOM and accounting_class from item master
		item_doc = frappe.get_cached_doc("Item", item_code)
		uom = shopify_item.get("uom") or item_doc.stock_uom or "Nos"
		
		# Get accounting class from item (if the field exists and is set)
		item_accounting_class = getattr(item_doc, "accounting_class", None)
		
		# Build item dict
		item_dict = {
					"item_code": item_code,
			"item_name": shopify_item.get("name") or shopify_item.get("title"),
			"description": f"SKU: {shopify_item.get('sku')} | Variant: {shopify_item.get('variant_id')}",  # Store Shopify identifiers
					"rate": _get_item_price(shopify_item, taxes_inclusive),
					"delivery_date": delivery_date,
					"qty": shopify_item.get("quantity"),
			"stock_uom": uom,
			"uom": uom,  # Sales Invoice uses 'uom' field
					"warehouse": setting.warehouse,
					ORDER_ITEM_DISCOUNT_FIELD: (
						_get_total_discount(shopify_item) / cint(shopify_item.get("quantity"))
					),
				}
		
		# Only add income_account if we found one (optional for ignore_validate mode)
		if income_account:
			item_dict["income_account"] = income_account
		
		# Add accounting class from item master (for product-level class assignment)
		if item_accounting_class:
			item_dict["accounting_class"] = item_accounting_class
		
		items.append(item_dict)

	return items


def _get_item_price(line_item, taxes_inclusive: bool) -> float:
	price = flt(line_item.get("price"))
	qty = cint(line_item.get("quantity"))

	# remove line item level discounts
	total_discount = _get_total_discount(line_item)

	if not taxes_inclusive:
		return price - (total_discount / qty)

	total_taxes = 0.0
	for tax in line_item.get("tax_lines"):
		total_taxes += flt(tax.get("price"))

	return price - (total_taxes + total_discount) / qty


def _get_total_discount(line_item) -> float:
	discount_allocations = line_item.get("discount_allocations") or []
	return sum(flt(discount.get("amount")) for discount in discount_allocations)


def get_order_taxes(shopify_order, setting, items, store_name=None, channel_settings=None):
	"""Get tax lines for Sales Order/Invoice.
	
	Args:
	    shopify_order: Shopify order data
	    setting: Store or Setting doc
	    items: Sales Order/Invoice items
	    store_name: Store name for multi-store tax account lookup
	    channel_settings: Dict from _get_channel_settings() with channel-specific config
	"""
	taxes = []
	line_items = shopify_order.get("line_items")
	taxes_included = shopify_order.get("taxes_included", False)
	
	# Extract channel settings (or use empty dict)
	channel = channel_settings or {}
	
	# Use channel-specific cost center if provided, otherwise fall back to store default
	effective_cost_center = channel.get("cost_center") or setting.cost_center
	
	# Use channel-specific tax account if provided, otherwise use existing mapping logic
	channel_tax_account = channel.get("tax_account")
	
	# Get channel's tax accounting class
	tax_accounting_class = channel.get("tax_accounting_class")

	for line_item in line_items:
		item_code = get_item_code(line_item, store_name=store_name)
		for tax in line_item.get("tax_lines"):
			# Use channel tax account if set, otherwise fall back to store tax mapping
			if channel_tax_account:
				tax_account = channel_tax_account
			else:
				tax_account = get_tax_account_head(tax, charge_type="sales_tax", setting=setting)
			
			tax_line = {
				"charge_type": "Actual",
				"account_head": tax_account,
				"description": (
					get_tax_account_description(tax, setting=setting)
					or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
				),
				"tax_amount": tax.get("price"),
				"included_in_print_rate": 1 if taxes_included else 0,
				"cost_center": effective_cost_center,
				"item_wise_tax_detail": {item_code: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]},
				"dont_recompute_tax": 1,
			}
			
			# Add accounting class if configured for this channel
			if tax_accounting_class:
				tax_line["accounting_class"] = tax_accounting_class
			
			taxes.append(tax_line)

	update_taxes_with_shipping_lines(
		taxes,
		shopify_order.get("shipping_lines"),
		setting,
		items,
		taxes_inclusive=shopify_order.get("taxes_included"),
		store_name=store_name,
		channel_settings=channel,
	)

	if cint(setting.consolidate_taxes):
		taxes = consolidate_order_taxes(taxes)

	for row in taxes:
		tax_detail = row.get("item_wise_tax_detail")
		if isinstance(tax_detail, dict):
			row["item_wise_tax_detail"] = json.dumps(tax_detail)

	return taxes


def consolidate_order_taxes(taxes):
	tax_account_wise_data = {}
	for tax in taxes:
		account_head = tax["account_head"]
		tax_account_wise_data.setdefault(
			account_head,
			{
				"charge_type": "Actual",
				"account_head": account_head,
				"description": tax.get("description"),
				"cost_center": tax.get("cost_center"),
				"included_in_print_rate": 0,
				"dont_recompute_tax": 1,
				"tax_amount": 0,
				"item_wise_tax_detail": {},
			},
		)
		tax_account_wise_data[account_head]["tax_amount"] += flt(tax.get("tax_amount"))
		if tax.get("item_wise_tax_detail"):
			tax_account_wise_data[account_head]["item_wise_tax_detail"].update(tax["item_wise_tax_detail"])

	return tax_account_wise_data.values()


def get_tax_account_head(tax, charge_type: Literal["shipping", "sales_tax"] | None = None, setting=None):
	"""Get tax account head for a tax line.
	
	Args:
	    tax: Tax line data
	    charge_type: Type of charge ("shipping" or "sales_tax")
	    setting: Store or Setting doc for multi-store support
	"""
	tax_title = str(tax.get("title"))
	
	# Determine parent doctype for tax account lookup
	if setting:
		parent_doctype = setting.doctype
		parent_name = setting.name
	else:
		parent_doctype = SETTING_DOCTYPE
		parent_name = SETTING_DOCTYPE

	tax_account = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": parent_name, "parenttype": parent_doctype, "shopify_tax": tax_title},
		"tax_account",
	)

	if not tax_account and charge_type:
		# Try default tax account
		if parent_doctype == STORE_DOCTYPE:
			tax_account = setting.get(DEFAULT_TAX_FIELDS[charge_type])
		else:
			tax_account = frappe.db.get_single_value(SETTING_DOCTYPE, DEFAULT_TAX_FIELDS[charge_type])

	if not tax_account:
		frappe.throw(_("Tax Account not specified for Shopify Tax {0}").format(tax.get("title")))

	return tax_account


def get_tax_account_description(tax, setting=None):
	"""Get tax account description for a tax line.
	
	Args:
	    tax: Tax line data
	    setting: Store or Setting doc for multi-store support
	"""
	tax_title = tax.get("title")
	
	# Determine parent doctype for tax account lookup
	if setting:
		parent_doctype = setting.doctype
		parent_name = setting.name
	else:
		parent_doctype = SETTING_DOCTYPE
		parent_name = SETTING_DOCTYPE

	tax_description = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": parent_name, "parenttype": parent_doctype, "shopify_tax": tax_title},
		"tax_description",
	)

	return tax_description


def update_taxes_with_shipping_lines(taxes, shipping_lines, setting, items, taxes_inclusive=False, store_name=None, channel_settings=None):
	"""Shipping lines represents the shipping details,
	each such shipping detail consists of a list of tax_lines
	
	Args:
	    taxes: Tax lines list to update
	    shipping_lines: Shopify shipping lines
	    setting: Store or Setting doc
	    items: Sales Order items
	    taxes_inclusive: Whether taxes are included
	    store_name: Store name for multi-store support
	    channel_settings: Dict from _get_channel_settings() with channel-specific config
	"""
	# Extract channel settings (or use empty dict)
	channel = channel_settings or {}
	
	# Use channel-specific cost center if provided, otherwise fall back to store default
	effective_cost_center = channel.get("cost_center") or setting.cost_center
	
	# Use channel-specific shipping account if provided, otherwise use existing mapping logic
	channel_shipping_account = channel.get("shipping_account")
	
	# Use channel-specific tax account for shipping taxes
	channel_tax_account = channel.get("tax_account")
	
	# Get channel's accounting classes
	shipping_accounting_class = channel.get("shipping_accounting_class")
	tax_accounting_class = channel.get("tax_accounting_class")
	
	shipping_as_item = cint(setting.add_shipping_as_item) and setting.shipping_item
	for shipping_charge in shipping_lines:
		if shipping_charge.get("price"):
			shipping_discounts = shipping_charge.get("discount_allocations") or []
			total_discount = sum(flt(discount.get("amount")) for discount in shipping_discounts)

			shipping_taxes = shipping_charge.get("tax_lines") or []
			total_tax = sum(flt(discount.get("price")) for discount in shipping_taxes)

			shipping_charge_amount = flt(shipping_charge["price"]) - flt(total_discount)
			if bool(taxes_inclusive):
				shipping_charge_amount -= total_tax

			if shipping_as_item:
				items.append(
					{
						"item_code": setting.shipping_item,
						"rate": shipping_charge_amount,
						"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
						"qty": 1,
						"stock_uom": "Nos",
						"warehouse": setting.warehouse,
					}
				)
			else:
				# Use channel shipping account if set, otherwise fall back to store shipping mapping
				if channel_shipping_account:
					shipping_account = channel_shipping_account
				else:
					shipping_account = get_tax_account_head(shipping_charge, charge_type="shipping", setting=setting)
				
				shipping_line = {
					"charge_type": "Actual",
					"account_head": shipping_account,
					"description": get_tax_account_description(shipping_charge, setting=setting)
					or shipping_charge["title"],
					"tax_amount": shipping_charge_amount,
					"cost_center": effective_cost_center,
				}
				
				# Add accounting class if configured for this channel
				if shipping_accounting_class:
					shipping_line["accounting_class"] = shipping_accounting_class
				
				taxes.append(shipping_line)

		for tax in shipping_charge.get("tax_lines"):
			# Use channel tax account if set, otherwise fall back to store tax mapping
			if channel_tax_account:
				tax_account = channel_tax_account
			else:
				tax_account = get_tax_account_head(tax, charge_type="sales_tax", setting=setting)
			
			tax_line = {
				"charge_type": "Actual",
				"account_head": tax_account,
				"description": (
					get_tax_account_description(tax, setting=setting)
					or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
				),
				"tax_amount": tax["price"],
				"included_in_print_rate": 1 if taxes_inclusive else 0,
				"cost_center": effective_cost_center,
				"item_wise_tax_detail": {
					setting.shipping_item: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]
				}
				if shipping_as_item
				else {},
				"dont_recompute_tax": 1,
			}
			
			# Add accounting class if configured for this channel
			if tax_accounting_class:
				tax_line["accounting_class"] = tax_accounting_class
			
			taxes.append(tax_line)


def get_sales_order(order_id):
	"""Get ERPNext sales order using shopify order id."""
	sales_order = frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: order_id})
	if sales_order:
		return frappe.get_doc("Sales Order", sales_order)


def cancel_order(payload, request_id=None, store_name=None):
	"""Called by order/cancelled event.

	When shopify order is cancelled there could be many different ways someone handles it.

	Updates document with custom field showing order status.

	IF delivery notes are not generated against an invoice, then cancel it.
	
	Args:
	    payload: Shopify order data
	    request_id: Integration log ID
	    store_name: Shopify Store name
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	order = payload

	try:
		order_id = cstr(order["id"])  # Convert to string for matching
		order_status = order["financial_status"]
		
		frappe.log_error(
			message=f"Processing cancellation for order ID: {order_id}, Store: {store_name}, Financial Status: {order_status}",
			title="Cancel Order Debug"
		)

		# Look for Sales Invoice (filter by store for multi-store support)
		invoice_filters = {ORDER_ID_FIELD: order_id}
		if store_name:
			invoice_filters[STORE_LINK_FIELD] = store_name
			
		sales_invoice = frappe.db.get_value(
			"Sales Invoice", 
			filters=invoice_filters,
			fieldname=["name", "docstatus"],
			as_dict=True
		)

		if not sales_invoice:
			frappe.log_error(
				message=f"Sales Invoice does not exist for order {order_id} in store {store_name}",
				title="Cancel Order - Invoice Not Found"
			)
			create_shopify_log(status="Invalid", message="Sales Invoice does not exist", store_name=store_name)
			return

		frappe.log_error(
			message=f"Found Sales Invoice: {sales_invoice.name}, docstatus: {sales_invoice.docstatus}",
			title="Cancel Order - Invoice Found"
		)

		# Get delivery notes for this order (filter by store)
		dn_filters = {ORDER_ID_FIELD: order_id}
		if store_name:
			dn_filters[STORE_LINK_FIELD] = store_name
			
		delivery_notes = frappe.db.get_list("Delivery Note", filters=dn_filters)

		# Update status on invoice
		frappe.db.set_value("Sales Invoice", sales_invoice.name, ORDER_STATUS_FIELD, order_status)

		for dn in delivery_notes:
			frappe.db.set_value("Delivery Note", dn.name, ORDER_STATUS_FIELD, order_status)

		# Handle cancellation based on document status
		frappe.log_error(
			message=f"Checking docstatus for invoice {sales_invoice.name}: {sales_invoice.docstatus}",
			title="Cancel Order - Docstatus Check"
		)
		
		if sales_invoice.docstatus == 0:
			# Draft invoice - delete it
			frappe.log_error(
				message=f"Attempting to delete draft invoice {sales_invoice.name}",
				title="Cancel Order - Deleting Draft"
			)
			
			try:
				frappe.delete_doc("Sales Invoice", sales_invoice.name, force=True)
				frappe.log_error(
					message=f"Successfully deleted draft Sales Invoice {sales_invoice.name} for cancelled Shopify order {order_id}",
					title="Shopify Order Cancelled - Draft Deleted"
				)
			except Exception as delete_error:
				frappe.log_error(
					message=f"Failed to delete draft invoice {sales_invoice.name}: {str(delete_error)}\n{frappe.get_traceback()}",
					title="Cancel Order - Delete Failed"
				)
				raise
		elif sales_invoice.docstatus == 1:
			# Submitted invoice - need to cancel it and related documents
			si_doc = frappe.get_doc("Sales Invoice", sales_invoice.name)
			
			# 1. Cancel ShipStation shipment if it exists (only if Delivery Note was created)
			if delivery_notes:
				# Lazy import to avoid circular dependency
				from ecommerce_integrations_multistore.shopify.shipstation_v2 import cancel_shipstation_shipment
				
				for dn_ref in delivery_notes:
					dn = frappe.get_doc("Delivery Note", dn_ref.name)
					
					# Only cancel ShipStation if shipment exists
					if dn.get("shipstation_shipment_id"):
						cancel_shipstation_shipment(dn)
					else:
						frappe.log_error(
							message=f"No ShipStation shipment ID on Delivery Note {dn.name}, skipping ShipStation cancellation",
							title="Cancel Order - No ShipStation ID"
						)
					
					# Cancel Delivery Note if submitted
					if dn.docstatus == 1:
						dn.add_comment(
							comment_type="Info",
							text=f"Cancelling due to Shopify order cancellation. Status: {order_status}"
						)
						dn.cancel()
						frappe.log_error(
							message=f"Cancelled Delivery Note {dn.name} for cancelled Shopify order {order_id}",
							title="Shopify Order Cancelled - DN Cancelled"
						)
			
			# 2. Cancel Payment Entry if it exists
			payment_entries = frappe.get_all(
				"Payment Entry",
				filters={
					"reference_name": sales_invoice.name,
					"reference_doctype": "Sales Invoice",
					"docstatus": 1  # Only submitted payments
				},
				pluck="name"
			)
			
			for pe_name in payment_entries:
				pe = frappe.get_doc("Payment Entry", pe_name)
				pe.add_comment(
					comment_type="Info",
					text=f"Cancelling due to Shopify order cancellation and refund. Status: {order_status}"
				)
				pe.cancel()
				frappe.log_error(
					message=f"Cancelled Payment Entry {pe_name} for refunded Shopify order {order_id}",
					title="Shopify Order Cancelled - Payment Reversed"
				)
			
			# 3. Cancel Sales Invoice
			try:
				frappe.log_error(
					message=f"Attempting to cancel Sales Invoice {sales_invoice.name}, current docstatus: {si_doc.docstatus}",
					title="Cancel Order - Cancelling Invoice"
				)
				
				# Reload the invoice to get latest timestamp (Payment Entry cancellation modifies it)
				si_doc.reload()
				
				si_doc.add_comment(
					comment_type="Info",
					text=f"Order cancelled and refunded in Shopify. Status: {order_status}"
				)
				si_doc.cancel()
				
				frappe.log_error(
					message=f"Successfully cancelled Sales Invoice {sales_invoice.name} for cancelled Shopify order {order_id}",
					title="Shopify Order Cancelled - Invoice Cancelled"
				)
			except frappe.exceptions.TimestampMismatchError as ts_error:
				# If still timestamp error, try one more time with fresh reload
				frappe.log_error(
					message=f"Timestamp mismatch on first attempt, reloading and retrying for {sales_invoice.name}",
					title="Cancel Order - Timestamp Retry"
				)
				si_doc = frappe.get_doc("Sales Invoice", sales_invoice.name)
				si_doc.add_comment(
					comment_type="Info",
					text=f"Order cancelled and refunded in Shopify. Status: {order_status}"
				)
				si_doc.cancel()
				frappe.log_error(
					message=f"Successfully cancelled Sales Invoice {sales_invoice.name} on retry",
					title="Shopify Order Cancelled - Invoice Cancelled (Retry)"
				)
			except Exception as cancel_error:
				frappe.log_error(
					message=f"Failed to cancel Sales Invoice {sales_invoice.name}: {str(cancel_error)}\n{frappe.get_traceback()}",
					title="Cancel Order - Invoice Cancel Failed"
				)
				raise
		
		frappe.db.commit()

	except Exception as e:
		create_shopify_log(status="Error", exception=e, store_name=store_name)
	else:
		create_shopify_log(status="Success", store_name=store_name)


@temp_shopify_session
def sync_old_orders():
	"""Backward compatibility: sync old orders for singleton setting."""
	shopify_setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not cint(shopify_setting.sync_old_orders):
		return

	orders = _fetch_old_orders(shopify_setting.old_orders_from, shopify_setting.old_orders_to)

	for order in orders:
		log = create_shopify_log(
			method=EVENT_MAPPER["orders/create"], request_data=json.dumps(order), make_new=True
		)
		sync_sales_order(order, request_id=log.name)

	shopify_setting = frappe.get_doc(SETTING_DOCTYPE)
	shopify_setting.sync_old_orders = 0
	shopify_setting.save()


@frappe.whitelist()
@temp_shopify_session
def sync_old_orders_for_store(store_name: str):
	"""Per-store worker: sync old orders for a specific store.
	
	Args:
	    store_name: Shopify Store name
	"""
	store = frappe.get_doc(STORE_DOCTYPE, store_name)
	
	if not cint(store.sync_old_orders):
		return

	orders = _fetch_old_orders(store.old_orders_from, store.old_orders_to)

	for order in orders:
		log = create_shopify_log(
			method=EVENT_MAPPER["orders/create"],
			request_data=json.dumps(order),
			make_new=True,
			store_name=store_name,
		)
		sync_sales_order(order, request_id=log.name, store_name=store_name)

	# Mark sync as complete
	store = frappe.get_doc(STORE_DOCTYPE, store_name)
	store.sync_old_orders = 0
	store.save()


def _get_income_account(item_code: str, company: str) -> str:
	"""Get income account for an item, falling back through Item → Item Group → Company.
	
	Args:
	    item_code: ERPNext Item code
	    company: Company name
	
	Returns:
	    Income account name
	"""
	# Try to get from Item's company-specific account
	item_doc = frappe.get_cached_doc("Item", item_code)
	
	# Check item's income account for this company
	for account in item_doc.get("item_defaults", []):
		if account.company == company and account.income_account:
			return account.income_account
	
	# Fall back to Item Group's default income account
	if item_doc.item_group:
		item_group_doc = frappe.get_cached_doc("Item Group", item_doc.item_group)
		accounts = item_group_doc.get("accounts") or []
		for account in accounts:
			if account.company == company and account.income_account:
				return account.income_account
	
	# Fall back to Company's default income account
	company_doc = frappe.get_cached_doc("Company", company)
	if company_doc.default_income_account:
		return company_doc.default_income_account
	
	# Last resort - get any income account for this company
	income_account = frappe.db.get_value(
		"Account",
		{
			"company": company,
			"account_type": "Income Account",
			"is_group": 0
		},
		"name"
	)
	
	return income_account or None


def _get_channel_settings(shopify_order: dict, setting) -> dict:
	"""Get all channel-specific settings based on order's sales channel (source_name).
	
	This is the primary function for channel-based configuration. It returns all
	settings needed for processing an order from a specific sales channel.
	
	Args:
	    shopify_order: Shopify order data dict
	    setting: Shopify Store or Setting doc
	
	Returns:
	    dict with keys:
	        - cost_center: Channel's cost center (applied to all lines)
	        - bank_account: Channel's bank account (Tier 2 in 3-tier priority)
	        - tax_account: Channel's tax account (or None to use store default)
	        - tax_accounting_class: Accounting class for tax lines
	        - shipping_account: Channel's shipping account (or None to use store default)
	        - shipping_accounting_class: Accounting class for shipping lines
	        - channel_name: The matched channel name (for logging)
	    All values are None if no channel mapping found.
	"""
	source_name = shopify_order.get("source_name", "").lower().strip()
	
	result = {
		"cost_center": None,
		"bank_account": None,
		"tax_account": None,
		"tax_accounting_class": None,
		"shipping_account": None,
		"shipping_accounting_class": None,
		"channel_name": None,
	}
	
	if not source_name or not hasattr(setting, "sales_channel_mapping"):
		return result
	
	# Look up in sales channel mapping table
	for mapping in setting.sales_channel_mapping:
		mapped_name = mapping.sales_channel_name.lower().strip()
		if mapped_name == source_name:
			result["cost_center"] = mapping.cost_center
			result["bank_account"] = getattr(mapping, "cash_bank_account", None)
			result["tax_account"] = getattr(mapping, "tax_account", None)
			result["tax_accounting_class"] = getattr(mapping, "tax_accounting_class", None)
			result["shipping_account"] = getattr(mapping, "shipping_account", None)
			result["shipping_accounting_class"] = getattr(mapping, "shipping_accounting_class", None)
			result["channel_name"] = mapping.sales_channel_name
			break
	
	return result


def _get_channel_financials(shopify_order: dict, setting) -> tuple[str | None, str | None]:
	"""Get cost center and bank account based on order's sales channel.
	
	DEPRECATED: Use _get_channel_settings() instead for full channel configuration.
	This function is kept for backward compatibility.
	
	Args:
	    shopify_order: Shopify order data dict
	    setting: Shopify Store or Setting doc
	
	Returns:
	    tuple: (cost_center, cash_bank_account) or (None, None) if using defaults
	"""
	channel = _get_channel_settings(shopify_order, setting)
	return channel["cost_center"], channel["bank_account"]


def _get_channel_cost_center(shopify_order: dict, setting) -> str | None:
	"""Get cost center based on order's sales channel (source_name).
	
	This is for P&L attribution - which marketing channel generated this sale.
	
	Args:
	    shopify_order: Shopify order data dict
	    setting: Shopify Store or Setting doc
	
	Returns:
	    Cost center name or None to use default
	"""
	channel = _get_channel_settings(shopify_order, setting)
	return channel["cost_center"]


def _get_channel_bank_account_legacy(shopify_order: dict, setting) -> str | None:
	"""Legacy: Get bank account from sales channel mapping.
	
	DEPRECATED: Use _get_payment_gateway_bank_account() instead.
	This is kept for backward compatibility with existing Sales Channel Mapping entries.
	"""
	channel = _get_channel_settings(shopify_order, setting)
	return channel["bank_account"]


def _get_payment_gateway_bank_account(shopify_order: dict, setting) -> str | None:
	"""Get bank account based on payment gateway used for the order.
	
	This is for treasury/cash flow - which bank account will receive the funds.
	
	Shopify provides payment gateway info in several fields:
	- payment_gateway_names: Array of gateway names used
	- gateway: Primary gateway (may be empty)
	- transactions: Array with gateway info per transaction
	
	Args:
	    shopify_order: Shopify order data dict
	    setting: Shopify Store or Setting doc
	
	Returns:
	    Bank account name or None to use default
	"""
	if not hasattr(setting, "payment_gateway_mapping") or not setting.payment_gateway_mapping:
		return None
	
	# Try to get gateway from various Shopify fields
	gateway = None
	
	# Method 1: payment_gateway_names array (most reliable)
	gateway_names = shopify_order.get("payment_gateway_names", [])
	if gateway_names and len(gateway_names) > 0:
		gateway = gateway_names[0].lower().strip()
	
	# Method 2: gateway field
	if not gateway:
		gateway = (shopify_order.get("gateway") or "").lower().strip()
	
	# Method 3: Check transactions array
	if not gateway:
		transactions = shopify_order.get("transactions", [])
		for txn in transactions:
			if txn.get("gateway"):
				gateway = txn.get("gateway").lower().strip()
				break
	
	if not gateway:
		return None
	
	# Look up in payment gateway mapping table
	for mapping in setting.payment_gateway_mapping:
		mapped_gateway = mapping.gateway_name.lower().strip()
		if mapped_gateway == gateway:
			return mapping.bank_account
	
	# No mapping found
	return None


def _sync_order_tags(document, shopify_tags: str) -> None:
	"""Parse Shopify tags and add them to Sales Order or Sales Invoice using ERPNext native tagging.
	
	Args:
	    document: ERPNext Sales Order or Sales Invoice document
	    shopify_tags: Comma-separated string of tags from Shopify (e.g., "wholesale, priority")
	"""
	if not shopify_tags or not isinstance(shopify_tags, str):
		return
	
	# Parse comma-separated tags and clean them
	tags = [tag.strip() for tag in shopify_tags.split(",") if tag.strip()]
	
	# Add each tag using ERPNext's native tagging system
	from frappe.desk.doctype.tag.tag import add_tag
	for tag in tags:
		try:
			add_tag(tag, document.doctype, document.name)
		except Exception as e:
			# Don't fail the order sync if tagging fails
			frappe.log_error(
				message=f"Failed to add tag '{tag}' to {document.doctype} {document.name}: {str(e)}",
				title="Shopify Tag Sync Error"
			)


def _fetch_old_orders(from_time, to_time):
	"""Fetch all shopify orders in specified range and return an iterator on fetched orders."""

	from_time = get_datetime(from_time).astimezone().isoformat()
	to_time = get_datetime(to_time).astimezone().isoformat()
	orders_iterator = PaginatedIterator(
		Order.find(created_at_min=from_time, created_at_max=to_time, limit=250)
	)

	for orders in orders_iterator:
		for order in orders:
			# Using generator instead of fetching all at once is better for
			# avoiding rate limits and reducing resource usage.
			yield order.to_dict()


@frappe.whitelist()
def resync_invoice_items(invoice_name):
	"""Re-sync items from Shopify for a hollow Sales Invoice.
	
	This is used when an invoice was created but items couldn't be mapped
	because they didn't exist in ERPNext at the time. After adding the items,
	this function can be called to re-fetch and add them to the invoice.
	
	Args:
		invoice_name: Sales Invoice name
		
	Returns:
		dict: Result with status and message
	"""
	try:
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
		
		# Verify this is a Shopify invoice
		if not invoice.get(ORDER_ID_FIELD):
			return {"status": "error", "message": "This is not a Shopify invoice"}
		
		# Verify invoice is still in draft
		if invoice.docstatus != 0:
			return {"status": "error", "message": "Invoice is not in draft status. Cannot modify."}
		
		# Get store settings
		store_name = invoice.get(STORE_LINK_FIELD)
		if not store_name:
			return {"status": "error", "message": "No Shopify Store linked to this invoice"}
		
		setting = frappe.get_doc(STORE_DOCTYPE, store_name)
		
		# Fetch order from Shopify using decorated function
		order_id = invoice.get(ORDER_ID_FIELD)
		
		@temp_shopify_session
		def fetch_shopify_order(order_id, store_name=None):
			"""Fetch order from Shopify API."""
			order = Order.find(order_id)
			if order:
				return order.to_dict()
			return None
		
		try:
			shopify_order = fetch_shopify_order(order_id, store_name=store_name)
			if not shopify_order:
				return {"status": "error", "message": f"Order {order_id} not found in Shopify"}
		except Exception as e:
			return {"status": "error", "message": f"Failed to fetch order from Shopify: {str(e)}"}
		
		# Get items from Shopify order
		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
			store_name=store_name,
			shopify_order=shopify_order,
		)
		
		if not items:
			return {
				"status": "warning", 
				"message": "Still unable to map items. Please ensure all SKUs exist in ERPNext."
			}
		
		# Get channel settings for cost center
		channel_settings = _get_channel_settings(shopify_order, setting)
		cost_center = channel_settings.get("cost_center") if channel_settings else None
		
		# Clear existing items (if any) and add new ones
		invoice.items = []
		for item in items:
			invoice.append("items", item)
		
		# Apply cost center to items
		if cost_center:
			for item in invoice.items:
				item.cost_center = cost_center
		
		# Re-calculate taxes if needed
		taxes = get_order_taxes(
			shopify_order, 
			setting, 
			items, 
			store_name=store_name,
			channel_settings=channel_settings
		)
		
		taxes = update_taxes_with_shipping_lines(
			taxes,
			shopify_order.get("shipping_lines"),
			setting,
			items,
			taxes_inclusive=shopify_order.get("taxes_included"),
			store_name=store_name,
			channel_settings=channel_settings
		)
		
		# Update taxes
		invoice.taxes = []
		for tax in taxes:
			invoice.append("taxes", tax)
		
		# Apply cost center to taxes
		if cost_center:
			for tax in invoice.taxes:
				tax.cost_center = cost_center
		
		# Save the invoice
		invoice.flags.ignore_validate = True
		invoice.flags.ignore_mandatory = True
		invoice.save(ignore_permissions=True)
		
		# Add comment
		invoice.add_comment(
			comment_type="Info",
			text=f"Items re-synced from Shopify. {len(items)} item(s) added."
		)
		
		frappe.db.commit()
		
		return {
			"status": "success", 
			"message": f"Successfully synced {len(items)} item(s) from Shopify",
			"items_count": len(items)
		}
		
	except Exception as e:
		frappe.log_error(
			message=f"Failed to resync items for {invoice_name}: {str(e)}\n{frappe.get_traceback()}",
			title="Resync Invoice Items Error"
		)
		return {"status": "error", "message": str(e)}


def fix_hollow_invoices(store_name=None):
	"""Find and fix Sales Invoices with no items (hollow invoices).
	
	This is called by the scheduled job to automatically retry item mapping
	for invoices that were created when items didn't exist in ERPNext.
	
	Args:
		store_name: Optional - limit to specific store
	"""
	filters = {
		"docstatus": 0,  # Draft only
		ORDER_ID_FIELD: ["is", "set"],  # Has Shopify order ID
	}
	
	if store_name:
		filters[STORE_LINK_FIELD] = store_name
	
	# Find invoices with no items
	hollow_invoices = frappe.db.sql("""
		SELECT si.name, si.shopify_order_id, si.shopify_store
		FROM `tabSales Invoice` si
		LEFT JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
		WHERE si.docstatus = 0
		AND si.shopify_order_id IS NOT NULL
		AND si.shopify_order_id != ''
		{store_filter}
		GROUP BY si.name
		HAVING COUNT(sii.name) = 0
	""".format(
		store_filter=f"AND si.shopify_store = '{store_name}'" if store_name else ""
	), as_dict=True)
	
	if not hollow_invoices:
		frappe.log_error(
			message="No hollow invoices found",
			title="Hollow Invoice Fix - No Action"
		)
		return
	
	frappe.log_error(
		message=f"Found {len(hollow_invoices)} hollow invoices to process",
		title="Hollow Invoice Fix - Starting"
	)
	
	fixed = 0
	still_hollow = 0
	errors = 0
	
	for inv in hollow_invoices:
		try:
			result = resync_invoice_items(inv.name)
			
			if result.get("status") == "success":
				fixed += 1
				frappe.log_error(
					message=f"Fixed {inv.name}: {result.get('message')}",
					title="Hollow Invoice Fixed"
				)
			elif result.get("status") == "warning":
				still_hollow += 1
				# Don't log every time - items still don't exist
			else:
				errors += 1
				frappe.log_error(
					message=f"Error fixing {inv.name}: {result.get('message')}",
					title="Hollow Invoice Fix Error"
				)
		except Exception as e:
			errors += 1
			frappe.log_error(
				message=f"Exception fixing {inv.name}: {str(e)}",
				title="Hollow Invoice Fix Exception"
			)
	
	frappe.log_error(
		message=f"Hollow invoice fix complete. Fixed: {fixed}, Still hollow: {still_hollow}, Errors: {errors}",
		title="Hollow Invoice Fix - Complete"
	)
