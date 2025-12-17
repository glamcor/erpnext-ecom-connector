import json

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from frappe.utils import cint, cstr, getdate, nowdate

from ecommerce_integrations_multistore.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
	STORE_DOCTYPE,
	STORE_LINK_FIELD,
)
from ecommerce_integrations_multistore.shopify.utils import create_shopify_log


def prepare_sales_invoice(payload, request_id=None, store_name=None, retry_count=0):
	"""Update payment status on existing Sales Invoice.
	
	Since we now create Sales Invoice on orders/create webhook,
	this webhook only updates the payment status.
	
	Args:
	    payload: Shopify order data
	    request_id: Integration log ID
	    store_name: Shopify Store name (multi-store support)
	    retry_count: Number of retries for concurrent modification handling
	"""
	import time
	
	MAX_RETRIES = 5
	order = payload
	new_status = order.get("financial_status")

	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	try:
		# Look for existing Sales Invoice - include current status for idempotency check
		sales_invoice = frappe.db.get_value(
			"Sales Invoice", 
			{ORDER_ID_FIELD: cstr(order["id"])}, 
			["name", "docstatus", ORDER_STATUS_FIELD],
			as_dict=True
		)
		
		if sales_invoice:
			current_status = sales_invoice.get(ORDER_STATUS_FIELD)
			
			# FIX 2: Idempotency check - skip if already at target status
			if current_status == new_status:
				create_shopify_log(
					status="Success", 
					message=f"Payment status already '{new_status}', skipping update",
					store_name=store_name
				)
				return
			
			# Update payment status
			si = frappe.get_doc("Sales Invoice", sales_invoice.name)
			
			# FIX 1: Actually update the shopify_order_status field
			si.db_set(ORDER_STATUS_FIELD, new_status)
			
			# Also update order number if needed
			if ORDER_NUMBER_FIELD in [f.fieldname for f in si.meta.fields]:
				si.db_set(ORDER_NUMBER_FIELD, order.get("name"))
			
			# Add comment about payment received
			si.add_comment(
				comment_type="Info",
				text=f"Payment received via Shopify. Financial status: {new_status}"
			)
			
			create_shopify_log(
				status="Success", 
				message=f"Payment status updated: {current_status} → {new_status}",
				store_name=store_name
			)
		else:
			create_shopify_log(
				status="Invalid",
				message="Sales Invoice not found for updating payment status.",
				store_name=store_name
			)
	
	# FIX 3: Better retry logic for concurrent modifications
	except frappe.TimestampMismatchError:
		if retry_count < MAX_RETRIES:
			frappe.log_error(
				message=f"orders/paid: Invoice modified by another process, retrying... (attempt {retry_count + 1}/{MAX_RETRIES})",
				title="Concurrent Modification - Retrying"
			)
			# Exponential backoff: 0.5s, 1s, 1.5s, 2s, 2.5s
			time.sleep(0.5 * (retry_count + 1))
			return prepare_sales_invoice(payload, request_id, store_name, retry_count + 1)
		else:
			frappe.log_error(
				message=f"orders/paid: Failed to update invoice after {MAX_RETRIES} retries due to concurrent modifications",
				title="Concurrent Modification - Failed"
			)
			create_shopify_log(
				status="Error", 
				exception=f"TimestampMismatchError after {MAX_RETRIES} retries",
				rollback=True, 
				store_name=store_name
			)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True, store_name=store_name)


def auto_create_delivery_note(doc, method=None):
	"""Automatically create Delivery Note when Sales Invoice is submitted.
	
	This is called via hooks when a Sales Invoice is submitted.
	Only creates DN for invoices that originated from Shopify.
	
	IMPORTANT: ShipStation API calls and Payment Entry creation are deferred
	to run AFTER the database transaction commits to prevent timeout issues.
	
	Args:
	    doc: Sales Invoice document
	    method: Hook method name (not used)
	"""
	# Debug log - FIRST THING
	frappe.log_error(
		message=f"auto_create_delivery_note START - invoice {doc.name}",
		title="DN Hook Entry Point"
	)
	
	# Wrap EVERYTHING in try-catch to find the issue
	try:
		# Check if this is a Shopify invoice
		if not doc.get(ORDER_ID_FIELD):
			return
		
		# Check if delivery note already exists for this store
		order_id = doc.get(ORDER_ID_FIELD)
		dn_filters = {ORDER_ID_FIELD: order_id}
		store_name = doc.get(STORE_LINK_FIELD)
		
		if store_name:
			dn_filters[STORE_LINK_FIELD] = store_name
		
		existing_dn = frappe.db.get_value("Delivery Note", dn_filters, "name")
		
		if existing_dn:
			frappe.log_error(
				message=f"Delivery Note {existing_dn} already exists for invoice {doc.name}",
				title="Shopify Hook - DN Exists"
			)
			return
		
		# Import here to avoid circular dependency
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_delivery_note
		
		# Get store settings
		store_name = doc.get(STORE_LINK_FIELD)
		if store_name:
			setting = frappe.get_doc(STORE_DOCTYPE, store_name)
		else:
			# Try to find store from Shopify order ID
			store_name = frappe.db.get_value(
				"Ecommerce Integration Log",
				{
					"request_data": ["like", f'%"id": {doc.get(ORDER_ID_FIELD)}%'],
					"method": ["like", "%sync_sales_order%"],
					"status": "Success"
				},
				"shopify_store"
			)
			if store_name:
				setting = frappe.get_doc(STORE_DOCTYPE, store_name)
			else:
				# Can't determine store, skip
				return
		
		# Check if auto-create is enabled
		if not cint(setting.sync_delivery_note):
			return
		
		# Create delivery note (this is fast and should be in the transaction)
		dn = make_delivery_note(doc.name)
		
		# Copy Shopify fields
		dn.set(ORDER_ID_FIELD, doc.get(ORDER_ID_FIELD))
		dn.set(ORDER_NUMBER_FIELD, doc.get(ORDER_NUMBER_FIELD))
		if store_name:
			dn.set(STORE_LINK_FIELD, store_name)
		
		dn.naming_series = setting.delivery_note_series or "DN-Shopify-"
		dn.flags.ignore_mandatory = True
		
		# Save and submit
		dn.save(ignore_permissions=True)
		dn.submit()
		
		# Add comment to invoice
		doc.add_comment(
			comment_type="Info",
			text=f"Delivery Note {dn.name} created automatically"
		)
		
		frappe.log_error(
			message=f"Auto-created Delivery Note {dn.name} for Sales Invoice {doc.name}",
			title="Shopify Auto Delivery Note"
		)
		
		# CRITICAL FIX: Defer ShipStation and Payment Entry to AFTER transaction commits
		# This prevents timeout issues because:
		# 1. ShipStation API calls can take 5-30 seconds
		# 2. Payment Entry creation involves multiple DB operations
		# 3. All of this was previously inside the Sales Invoice submit transaction
		# 4. If total time exceeded DB lock timeout (~50s), everything rolled back
		#    EXCEPT ShipStation (external API call can't be rolled back)
		#
		# By using frappe.db.after_commit(), these operations run AFTER the transaction
		# commits successfully, so:
		# - Invoice submission completes quickly
		# - Delivery Note is created and committed
		# - ShipStation and Payment Entry run outside the transaction
		# - If they fail, the invoice/DN are still saved
		
		# Store references for the after_commit callback
		dn_name = dn.name
		invoice_name = doc.name
		setting_name = setting.name
		
		def after_commit_tasks():
			"""Run ShipStation and Payment Entry after transaction commits."""
			try:
				# Re-fetch documents (we're in a new transaction now)
				delivery_note = frappe.get_doc("Delivery Note", dn_name)
				invoice = frappe.get_doc("Sales Invoice", invoice_name)
				store_setting = frappe.get_doc(STORE_DOCTYPE, setting_name)
				
				frappe.log_error(
					message=f"After-commit tasks starting for invoice {invoice_name}",
					title="After Commit - Start"
				)
				
				# Send to ShipStation if configured
				from ecommerce_integrations_multistore.shopify.fulfillment import send_to_shipstation
				send_to_shipstation(delivery_note, store_setting)
				
				# Create payment entry if order is paid
				create_payment_entry_for_invoice(invoice, store_setting)
				
				frappe.log_error(
					message=f"After-commit tasks completed for invoice {invoice_name}",
					title="After Commit - Complete"
				)
				
			except Exception as e:
				# Log error but don't fail - the invoice is already committed
				frappe.log_error(
					message=f"After-commit tasks failed for invoice {invoice_name}: {str(e)}\n{frappe.get_traceback()}",
					title="After Commit - Error"
				)
		
		# Register the callback to run after transaction commits
		frappe.db.after_commit.add(after_commit_tasks)
		
		frappe.log_error(
			message=f"Registered after-commit tasks for invoice {doc.name}",
			title="After Commit - Registered"
		)
		
	except Exception as e:
		# Log error but don't fail the invoice submission
		frappe.log_error(
			message=f"CRITICAL ERROR in auto_create_delivery_note for {doc.name}: {str(e)}\n{frappe.get_traceback()}",
			title="DN Hook Critical Error"
		)


def create_payment_entry_for_invoice(invoice, setting):
	"""Create Payment Entry for paid Shopify orders.
	
	Args:
	    invoice: Sales Invoice document
	    setting: Shopify Store settings
	"""
	frappe.log_error(
		message=f"create_payment_entry_for_invoice called for invoice {invoice.name}",
		title="Payment Entry Creation Start"
	)
	
	try:
		# Get the Shopify order data
		order_id = invoice.get(ORDER_ID_FIELD)
		if not order_id:
			frappe.log_error(
				message=f"No ORDER_ID_FIELD on invoice {invoice.name}",
				title="Payment Entry Skipped - No Order ID"
			)
			return
		
		# Import ORDER_STATUS_FIELD constant
		from ecommerce_integrations_multistore.shopify.constants import ORDER_STATUS_FIELD
		
		# First check the invoice's shopify_order_status field
		financial_status = invoice.get(ORDER_STATUS_FIELD)
		
		# Always try to get the full Shopify order data from Integration Log
		# This is needed for payment gateway mapping (payment_gateway_names field)
		# Search across all possible webhook methods that could have logged this order
		order_data = None
		
		# Method 1: orders/create webhook -> sync_sales_order
		order_data = frappe.db.get_value(
			"Ecommerce Integration Log",
			{
				"request_data": ["like", f'%"id": {order_id}%'],
				"method": ["like", "%sync_sales_order%"],
				"status": "Success"
			},
			"request_data"
		)
		
		# Method 2: orders/paid webhook -> prepare_sales_invoice
		if not order_data:
			order_data = frappe.db.get_value(
				"Ecommerce Integration Log",
				{
					"request_data": ["like", f'%"id": {order_id}%'],
					"method": ["like", "%prepare_sales_invoice%"],
					"status": "Success"
				},
				"request_data"
			)
		
		# Method 3: orders/updated webhook -> handle_order_update (get most recent)
		if not order_data:
			update_logs = frappe.get_all(
				"Ecommerce Integration Log",
				filters={
					"request_data": ["like", f'%"id": {order_id}%'],
					"method": ["like", "%handle_order_update%"],
					"status": "Success"
				},
				fields=["request_data"],
				order_by="modified desc",
				limit=1
			)
			if update_logs:
				order_data = update_logs[0].request_data
		
		shopify_order = None
		if order_data:
			try:
				shopify_order = json.loads(order_data)
				# Update financial_status from the full order data if available
				financial_status = shopify_order.get("financial_status") or financial_status
			except:
				pass
		
		# If we couldn't get the full order data, create a minimal dict
		# Note: This will fall back to store default bank account since we won't have payment_gateway_names
		if not shopify_order:
			if financial_status != "paid":
				frappe.log_error(
					message=f"Invoice {invoice.name} has financial status '{financial_status}' and no order data found - not creating payment entry",
					title="Payment Entry Skipped - Not Paid"
				)
				return
			
			shopify_order = {
				"id": order_id,
				"name": invoice.get(ORDER_NUMBER_FIELD),
				"financial_status": financial_status,
				"created_at": invoice.posting_date,
				# Don't hardcode gateway - let it fall through to store default
			}
			frappe.log_error(
				message=f"Could not find full Shopify order data for invoice {invoice.name}. Payment gateway mapping will not be available.",
				title="Payment Entry Warning - No Order Data"
			)
		
		# Check if order is paid
		if financial_status != "paid":
			frappe.log_error(
				message=f"Invoice {invoice.name} has financial_status '{financial_status}' (not 'paid') - skipping payment entry",
				title="Payment Entry Skipped - Not Paid Status"
			)
			return
		
		# Check if payment entry already exists
		existing_pe = frappe.db.exists(
			"Payment Entry",
			{
				"reference_name": invoice.name,
				"reference_doctype": "Sales Invoice",
				"docstatus": ["!=", 2]
			}
		)
		
		if existing_pe:
			frappe.log_error(
				message=f"Payment Entry already exists for invoice {invoice.name}: {existing_pe}",
				title="Payment Entry Skipped - Already Exists"
			)
			return
		
		frappe.log_error(
			message=f"Proceeding to create Payment Entry for invoice {invoice.name}. Financial status: {financial_status}",
			title="Payment Entry - Proceeding"
		)
		
		# Get cost center from sales channel (for P&L attribution)
		from ecommerce_integrations_multistore.shopify.order import (
			_get_channel_cost_center,
			_get_channel_bank_account_legacy,
			_get_payment_gateway_bank_account,
		)
		cost_center = _get_channel_cost_center(shopify_order, setting)
		
		# Determine gateway and source for logging
		gateway_names = shopify_order.get("payment_gateway_names", [])
		gateway = gateway_names[0] if gateway_names else shopify_order.get("gateway", "")
		source_name = shopify_order.get("source_name", "")
		
		# 3-tier priority for bank account selection:
		# 1. Payment Gateway Mapping (most specific - e.g., afterpay, klarna)
		# 2. Sales Channel Mapping (channel-specific - e.g., tiktok)
		# 3. Store Default (fallback)
		
		cash_bank_account = None
		account_source = None
		
		# Tier 1: Payment Gateway Mapping
		cash_bank_account = _get_payment_gateway_bank_account(shopify_order, setting)
		if cash_bank_account:
			account_source = f"Payment Gateway '{gateway}'"
		
		# Tier 2: Sales Channel Mapping
		if not cash_bank_account:
			cash_bank_account = _get_channel_bank_account_legacy(shopify_order, setting)
			if cash_bank_account:
				account_source = f"Sales Channel '{source_name}'"
		
		# Tier 3: Store Default
		if not cash_bank_account:
			cash_bank_account = setting.cash_bank_account
			if cash_bank_account:
				account_source = "Store Default"
		
		if not cash_bank_account:
			frappe.log_error(
				message=(
					f"No cash/bank account configured for payment entry creation.\n"
					f"Gateway: {gateway}, Source: {source_name}\n"
					f"Checked: Payment Gateway Mapping, Sales Channel Mapping, Store Default"
				),
				title="Payment Entry Configuration Missing"
			)
			return
		
		# Skip payment entry for $0 invoices (free products, 100% discount, etc.)
		if invoice.grand_total == 0:
			frappe.log_error(
				message=f"Invoice {invoice.name} has $0 grand total. Skipping payment entry creation.",
				title="Payment Entry Skipped - Zero Amount"
			)
			return
		
		# Create payment entry
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
		
		pe = get_payment_entry("Sales Invoice", invoice.name, bank_account=cash_bank_account)
		pe.reference_no = shopify_order.get("name")  # Shopify order number
		pe.reference_date = getdate(shopify_order.get("created_at"))
		
		# Set cost center if available
		if cost_center:
			pe.cost_center = cost_center
		
		# Add payment gateway info to remarks - include account source for audit trail
		gateway_display = gateway.replace("_", " ").title() if gateway else "Unknown"
		pe.remarks = f"Payment via {gateway_display} - Shopify Order {shopify_order.get('name')} - Account from {account_source}"
		
		pe.save(ignore_permissions=True)
		pe.submit()
		
		# Add comment to invoice with account source
		invoice.add_comment(
			comment_type="Info",
			text=f"Payment Entry {pe.name} created automatically (Bank: {cash_bank_account} via {account_source})"
		)
		
		frappe.log_error(
			message=(
				f"Auto-created Payment Entry {pe.name} for Sales Invoice {invoice.name}\n"
				f"Bank Account: {cash_bank_account} (from {account_source})\n"
				f"Gateway: {gateway}, Source: {source_name}"
			),
			title="Shopify Auto Payment Entry"
		)
		
	except Exception as e:
		# Log error but don't fail the invoice submission
		frappe.log_error(
			message=f"Failed to auto-create payment entry for invoice {invoice.name}: {str(e)}",
			title="Auto Payment Entry Error"
		)


def create_sales_invoice(shopify_order, setting, so, store_name=None):
	"""Create Sales Invoice from Shopify order.
	
	Args:
	    shopify_order: Shopify order data
	    setting: Store or Setting doc
	    so: Sales Order doc
	    store_name: Shopify Store name for multi-store support
	"""
	# Check if invoice exists for this store
	invoice_filters = {ORDER_ID_FIELD: shopify_order.get("id")}
	if store_name:
		invoice_filters[STORE_LINK_FIELD] = store_name
		
	if (
		not frappe.db.get_value("Sales Invoice", invoice_filters, "name")
		and so.docstatus == 1
		and not so.per_billed
		and cint(setting.sync_sales_invoice)
	):
		posting_date = getdate(shopify_order.get("created_at")) or nowdate()

		sales_invoice = make_sales_invoice(so.name, ignore_permissions=True)
		sales_invoice.set(ORDER_ID_FIELD, str(shopify_order.get("id")))
		sales_invoice.set(ORDER_NUMBER_FIELD, shopify_order.get("name"))
		
		# Set store reference for multi-store
		if store_name:
			sales_invoice.set(STORE_LINK_FIELD, store_name)
		
		sales_invoice.set_posting_time = 1
		sales_invoice.posting_date = posting_date
		sales_invoice.due_date = posting_date
		sales_invoice.naming_series = setting.sales_invoice_series or "SI-Shopify-"
		sales_invoice.flags.ignore_mandatory = True
		set_cost_center(sales_invoice.items, setting.cost_center)
		sales_invoice.insert(ignore_mandatory=True)
		sales_invoice.submit()
		if sales_invoice.grand_total > 0:
			make_payament_entry_against_sales_invoice(sales_invoice, setting, posting_date, shopify_order)

		if shopify_order.get("note"):
			sales_invoice.add_comment(text=f"Order Note: {shopify_order.get('note')}")


def set_cost_center(items, cost_center):
	for item in items:
		item.cost_center = cost_center


def make_payament_entry_against_sales_invoice(doc, setting, posting_date=None, shopify_order=None):
	"""Create payment entry for a sales invoice using 3-tier bank account priority.
	
	Args:
	    doc: Sales Invoice document
	    setting: Shopify Store or Setting doc
	    posting_date: Optional posting date
	    shopify_order: Optional Shopify order data for gateway/channel lookup
	"""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
	from ecommerce_integrations_multistore.shopify.order import (
		_get_channel_cost_center,
		_get_channel_bank_account_legacy,
		_get_payment_gateway_bank_account,
	)
	
	# 3-tier priority for bank account selection (same as create_payment_entry_for_invoice)
	cash_bank_account = None
	account_source = None
	
	if shopify_order:
		gateway_names = shopify_order.get("payment_gateway_names", [])
		gateway = gateway_names[0] if gateway_names else shopify_order.get("gateway", "")
		source_name = shopify_order.get("source_name", "")
		
		# Tier 1: Payment Gateway Mapping
		cash_bank_account = _get_payment_gateway_bank_account(shopify_order, setting)
		if cash_bank_account:
			account_source = f"Payment Gateway '{gateway}'"
		
		# Tier 2: Sales Channel Mapping
		if not cash_bank_account:
			cash_bank_account = _get_channel_bank_account_legacy(shopify_order, setting)
			if cash_bank_account:
				account_source = f"Sales Channel '{source_name}'"
	
	# Tier 3: Store Default
	if not cash_bank_account:
		cash_bank_account = setting.cash_bank_account
		account_source = "Store Default"
	
	if not cash_bank_account:
		frappe.log_error(
			message=f"No cash/bank account configured for payment entry creation for invoice {doc.name}",
			title="Payment Entry Configuration Missing"
		)
		return

	payment_entry = get_payment_entry(doc.doctype, doc.name, bank_account=cash_bank_account)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = doc.name
	payment_entry.posting_date = posting_date or nowdate()
	payment_entry.reference_date = posting_date or nowdate()
	
	# Add remarks showing which tier was used
	payment_entry.remarks = f"Auto-created from Shopify - Account from {account_source}"
	
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	
	frappe.log_error(
		message=f"Payment Entry {payment_entry.name} created for {doc.name} using {account_source}",
		title="Shopify Payment Entry Created"
	)
