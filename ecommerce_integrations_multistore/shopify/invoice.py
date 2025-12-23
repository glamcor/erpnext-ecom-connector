import json

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from frappe.utils import cint, cstr, getdate, nowdate, get_datetime

from ecommerce_integrations_multistore.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	PAYMENT_CAPTURE_DATE_FIELD,
	PAYMENT_GATEWAY_FIELD,
	SETTING_DOCTYPE,
	SOURCE_NAME_FIELD,
	STORE_DOCTYPE,
	STORE_LINK_FIELD,
)
from ecommerce_integrations_multistore.shopify.utils import create_shopify_log


def get_payment_capture_date(shopify_order):
	"""Extract the payment capture date from Shopify order transactions.
	
	Shopify orders have a transactions array with entries like:
	- kind: "authorization" (when card is authorized)
	- kind: "capture" (when payment is actually captured)
	- kind: "sale" (for immediate capture like PayPal)
	
	For accurate accounting, we want the date when money was actually captured.
	
	Args:
		shopify_order: Dict with Shopify order data including transactions
		
	Returns:
		date: The capture date, or order created_at date as fallback
	"""
	transactions = shopify_order.get("transactions", [])
	
	# Look for capture or sale transaction (actual payment)
	for txn in transactions:
		kind = (txn.get("kind") or "").lower()
		status = (txn.get("status") or "").lower()
		
		# Only consider successful captures/sales
		if kind in ("capture", "sale") and status == "success":
			txn_date = txn.get("created_at")
			if txn_date:
				dt = get_datetime(txn_date)
				if dt:
					# Strip timezone for ERPNext compatibility
					return dt.replace(tzinfo=None).date()
	
	# Fallback to order created_at date
	created_at = shopify_order.get("created_at")
	if created_at:
		dt = get_datetime(created_at)
		if dt:
			return dt.replace(tzinfo=None).date()
	
	# Final fallback to today
	return nowdate()


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
	from frappe.utils import get_datetime
	
	MAX_RETRIES = 5
	order = payload
	new_status = order.get("financial_status")

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
					message=f"Payment update for order {order.get('name')} ignored - order created at {order_created_at} is before cutoff date {cutoff_date}",
					store_name=store_name
				)
				return

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
		
		# CRITICAL: Use background job for ShipStation and Payment Entry
		# 
		# Why background job (frappe.enqueue) instead of after_commit:
		# - after_commit still runs in the same HTTP request, causing timeouts
		# - Background jobs run in a separate worker process
		# - HTTP response returns immediately to the user
		# - ShipStation API (5-30s) doesn't block the UI
		#
		# The job runs after the current transaction commits because we're
		# enqueueing it here (before commit), and the worker will fetch
		# fresh data from the database.
		
		frappe.enqueue(
			"ecommerce_integrations_multistore.shopify.invoice.process_post_submit_tasks",
			queue="default",
			timeout=300,  # 5 minutes max for external API calls
			invoice_name=doc.name,
			dn_name=dn.name,
			setting_name=setting.name,
		)
		
		frappe.log_error(
			message=f"Enqueued background job for invoice {doc.name}, DN {dn.name}",
			title="Post-Submit Job Enqueued"
		)
		
	except Exception as e:
		# Log error but don't fail the invoice submission
		frappe.log_error(
			message=f"CRITICAL ERROR in auto_create_delivery_note for {doc.name}: {str(e)}\n{frappe.get_traceback()}",
			title="DN Hook Critical Error"
		)


def process_post_submit_tasks(invoice_name, dn_name, setting_name):
	"""Background job to process ShipStation and Payment Entry after invoice submit.
	
	This runs in a separate worker process, so it doesn't block the HTTP request.
	The user sees immediate response when submitting the invoice.
	
	Args:
		invoice_name: Sales Invoice name
		dn_name: Delivery Note name
		setting_name: Shopify Store name
	"""
	frappe.log_error(
		message=f"Background job starting for invoice {invoice_name}, DN {dn_name}",
		title="Post-Submit Job - Start"
	)
	
	try:
		# Fetch documents fresh from database
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
		delivery_note = frappe.get_doc("Delivery Note", dn_name)
		setting = frappe.get_doc(STORE_DOCTYPE, setting_name)
		
		# Send to ShipStation if configured
		if cint(setting.sync_delivery_note):
			from ecommerce_integrations_multistore.shopify.fulfillment import send_to_shipstation
			send_to_shipstation(delivery_note, setting)
		
		# Create payment entry if order is paid
		create_payment_entry_for_invoice(invoice, setting)
		
		# Commit the payment entry
		frappe.db.commit()
		
		frappe.log_error(
			message=f"Background job completed for invoice {invoice_name}",
			title="Post-Submit Job - Complete"
		)
		
	except Exception as e:
		frappe.log_error(
			message=f"Background job failed for invoice {invoice_name}: {str(e)}\n{frappe.get_traceback()}",
			title="Post-Submit Job - Error"
		)
		# Don't re-raise - the invoice/DN are already committed


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
		
		# Debug: Log the raw financial status from invoice
		frappe.log_error(
			message=f"Invoice {invoice.name}: ORDER_STATUS_FIELD raw value = '{financial_status}' (type: {type(financial_status).__name__})",
			title="Payment Entry Debug - Status Check"
		)
		
		# Get payment gateway and source from invoice fields (stored at creation time)
		# This avoids slow Integration Log lookups
		payment_gateway = invoice.get(PAYMENT_GATEWAY_FIELD) or ""
		source_name = invoice.get(SOURCE_NAME_FIELD) or ""
		
		frappe.log_error(
			message=f"Invoice {invoice.name}: payment_gateway='{payment_gateway}', source_name='{source_name}'",
			title="Payment Entry Debug - Gateway Info"
		)
		
		# Build shopify_order dict with gateway info for bank account mapping
		shopify_order = {
			"id": order_id,
			"name": invoice.get(ORDER_NUMBER_FIELD),
			"financial_status": financial_status,
			"created_at": invoice.posting_date,
			"payment_gateway_names": [payment_gateway] if payment_gateway else [],
			"gateway": payment_gateway,
			"source_name": source_name,
		}
		
		# Check if order is paid (case-insensitive comparison)
		# Shopify uses lowercase "paid" but ERPNext UI might display differently
		financial_status_lower = (financial_status or "").lower().strip()
		
		# Log the final status being used (should match invoice field, not log data)
		log_status = shopify_order.get("financial_status") if shopify_order else None
		frappe.log_error(
			message=f"Invoice {invoice.name}: using invoice status='{financial_status}' (normalized='{financial_status_lower}'), log had='{log_status}'",
			title="Payment Entry Debug - Final Status"
		)
		
		if financial_status_lower != "paid":
			frappe.log_error(
				message=f"Invoice {invoice.name} has financial_status '{financial_status}' (normalized: '{financial_status_lower}', not 'paid') - skipping payment entry",
				title="Payment Entry Skipped - Not Paid Status"
			)
			return
		
		# Check if payment entry already exists
		# Note: Payment Entry uses a child table "Payment Entry Reference" for references
		# This query checks for any PE that references this invoice
		existing_pe = frappe.db.sql("""
			SELECT parent FROM `tabPayment Entry Reference`
			WHERE reference_doctype = 'Sales Invoice'
			AND reference_name = %s
			AND docstatus != 2
			LIMIT 1
		""", (invoice.name,), as_dict=True)
		
		existing_pe = existing_pe[0].parent if existing_pe else None
		
		frappe.log_error(
			message=f"Invoice {invoice.name}: existing_pe check result = '{existing_pe}'",
			title="Payment Entry Debug - Exists Check"
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
		
		# Skip if invoice has no outstanding amount (already paid via other means)
		# Reload invoice to get fresh outstanding_amount
		invoice.reload()
		
		frappe.log_error(
			message=f"Invoice {invoice.name}: outstanding_amount={invoice.outstanding_amount}, grand_total={invoice.grand_total}, status={invoice.status}",
			title="Payment Entry Debug - Outstanding Check"
		)
		
		if invoice.outstanding_amount <= 0:
			frappe.log_error(
				message=f"Invoice {invoice.name} has no outstanding amount ({invoice.outstanding_amount}). May have been paid via journal entry or credit note.",
				title="Payment Entry Skipped - No Outstanding"
			)
			return
		
		# Create payment entry
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
		
		try:
			pe = get_payment_entry("Sales Invoice", invoice.name, bank_account=cash_bank_account)
		except Exception as pe_error:
			error_msg = str(pe_error)
			if "already been fully paid" in error_msg or "outstanding amount" in error_msg.lower():
				frappe.log_error(
					message=f"Invoice {invoice.name}: get_payment_entry failed with '{error_msg}'. Outstanding was {invoice.outstanding_amount}. This may be a timing issue.",
					title="Payment Entry Skipped - ERPNext Says Paid"
				)
				return
			raise  # Re-raise other errors
		pe.reference_no = shopify_order.get("name")  # Shopify order number
		
		# Use payment capture date for accurate accounting
		# This is the date when money was actually captured, not when order was placed
		# For Friday order / Saturday capture / Monday processing scenario:
		# - Invoice posting_date = Friday (when order was placed)
		# - Payment posting_date = Saturday (when payment was captured)
		capture_date = invoice.get(PAYMENT_CAPTURE_DATE_FIELD)
		if capture_date:
			pe.posting_date = capture_date
			pe.reference_date = capture_date
		else:
			# Fallback to invoice posting date if no capture date stored
			pe.posting_date = invoice.posting_date
			pe.reference_date = invoice.posting_date
		
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
	
	# Use payment capture date for accurate accounting if available
	# Priority: capture date from invoice > passed posting_date > today
	capture_date = doc.get(PAYMENT_CAPTURE_DATE_FIELD)
	effective_posting_date = capture_date or posting_date or nowdate()
	payment_entry.posting_date = effective_posting_date
	payment_entry.reference_date = effective_posting_date
	
	# Add remarks showing which tier was used
	payment_entry.remarks = f"Auto-created from Shopify - Account from {account_source}"
	
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	
	frappe.log_error(
		message=f"Payment Entry {payment_entry.name} created for {doc.name} using {account_source}",
		title="Shopify Payment Entry Created"
	)


@frappe.whitelist()
def check_invoice_status(invoice_name):
	"""Check if a submitted Shopify invoice is missing DN or Payment.
	
	Args:
		invoice_name: Sales Invoice name
		
	Returns:
		dict: Status flags for missing_dn and missing_payment
	"""
	try:
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
		
		# Not a Shopify invoice
		if not invoice.get(ORDER_ID_FIELD):
			return {"missing_dn": False, "missing_payment": False}
		
		# Not submitted
		if invoice.docstatus != 1:
			return {"missing_dn": False, "missing_payment": False}
		
		order_id = invoice.get(ORDER_ID_FIELD)
		
		# Check for missing DN
		dn_exists = frappe.db.exists(
			"Delivery Note",
			{ORDER_ID_FIELD: order_id, "docstatus": ["!=", 2]}
		)
		missing_dn = not dn_exists
		
		# Check for missing Payment
		missing_payment = False
		financial_status = (invoice.get(ORDER_STATUS_FIELD) or "").lower().strip()
		
		if financial_status == "paid" and invoice.outstanding_amount > 0:
			# Check if payment entry exists
			pe_exists = frappe.db.sql("""
				SELECT 1 FROM `tabPayment Entry Reference`
				WHERE reference_doctype = 'Sales Invoice'
				AND reference_name = %s
				AND docstatus != 2
				LIMIT 1
			""", (invoice.name,))
			
			missing_payment = not pe_exists
		
		return {
			"missing_dn": missing_dn,
			"missing_payment": missing_payment,
			"outstanding_amount": invoice.outstanding_amount,
			"financial_status": financial_status
		}
		
	except Exception as e:
		frappe.log_error(
			message=f"Error checking invoice status for {invoice_name}: {str(e)}",
			title="Check Invoice Status Error"
		)
		return {"missing_dn": False, "missing_payment": False, "error": str(e)}


@frappe.whitelist()
def create_missing_delivery_note(invoice_name):
	"""Create missing Delivery Note for a submitted Shopify invoice.
	
	This is used to repair invoices that were submitted but the DN creation failed.
	
	Args:
		invoice_name: Sales Invoice name
		
	Returns:
		dict: Result with status and message
	"""
	try:
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
		
		# Verify this is a submitted Shopify invoice
		if not invoice.get(ORDER_ID_FIELD):
			return {"status": "error", "message": "This is not a Shopify invoice"}
		
		if invoice.docstatus != 1:
			return {"status": "error", "message": "Invoice must be submitted"}
		
		# Check if DN already exists
		existing_dn = frappe.db.get_value(
			"Delivery Note",
			{ORDER_ID_FIELD: invoice.get(ORDER_ID_FIELD), "docstatus": ["!=", 2]},
			"name"
		)
		
		if existing_dn:
			return {"status": "error", "message": f"Delivery Note {existing_dn} already exists"}
		
		# Get store settings
		store_name = invoice.get(STORE_LINK_FIELD)
		if not store_name:
			return {"status": "error", "message": "No Shopify Store linked to this invoice"}
		
		setting = frappe.get_doc(STORE_DOCTYPE, store_name)
		
		# Create delivery note
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_delivery_note
		
		dn = make_delivery_note(invoice.name)
		
		# Copy Shopify fields
		dn.set(ORDER_ID_FIELD, invoice.get(ORDER_ID_FIELD))
		dn.set(ORDER_NUMBER_FIELD, invoice.get(ORDER_NUMBER_FIELD))
		dn.set(STORE_LINK_FIELD, store_name)
		
		dn.naming_series = setting.delivery_note_series or "DN-Shopify-"
		dn.flags.ignore_mandatory = True
		
		# Save and submit
		dn.save(ignore_permissions=True)
		dn.submit()
		
		# Add comment to invoice
		invoice.add_comment(
			comment_type="Info",
			text=f"Delivery Note {dn.name} created manually (repair)"
		)
		
		# Send to ShipStation if enabled
		if cint(setting.enable_shipstation):
			try:
				from ecommerce_integrations_multistore.shopify.shipstation import send_to_shipstation
				send_to_shipstation(dn, setting)
				frappe.db.commit()
				return {
					"status": "success",
					"message": f"Delivery Note {dn.name} created and sent to ShipStation",
					"delivery_note": dn.name
				}
			except Exception as e:
				frappe.log_error(
					message=f"ShipStation send failed for {dn.name}: {str(e)}",
					title="ShipStation Error"
				)
				frappe.db.commit()
				return {
					"status": "success",
					"message": f"Delivery Note {dn.name} created (ShipStation send failed: {str(e)})",
					"delivery_note": dn.name
				}
		
		frappe.db.commit()
		return {
			"status": "success",
			"message": f"Delivery Note {dn.name} created",
			"delivery_note": dn.name
		}
		
	except Exception as e:
		frappe.log_error(
			message=f"Failed to create DN for {invoice_name}: {str(e)}\n{frappe.get_traceback()}",
			title="Create Missing DN Error"
		)
		return {"status": "error", "message": str(e)}


@frappe.whitelist()
def create_missing_payment_entry(invoice_name):
	"""Create missing Payment Entry for a submitted Shopify invoice.
	
	This is used to repair invoices that were submitted but the Payment creation failed.
	
	Args:
		invoice_name: Sales Invoice name
		
	Returns:
		dict: Result with status and message
	"""
	try:
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
		
		# Verify this is a submitted Shopify invoice
		if not invoice.get(ORDER_ID_FIELD):
			return {"status": "error", "message": "This is not a Shopify invoice"}
		
		if invoice.docstatus != 1:
			return {"status": "error", "message": "Invoice must be submitted"}
		
		# Check if payment already exists
		existing_pe = frappe.db.sql("""
			SELECT parent FROM `tabPayment Entry Reference`
			WHERE reference_doctype = 'Sales Invoice'
			AND reference_name = %s
			AND docstatus != 2
			LIMIT 1
		""", (invoice.name,), as_dict=True)
		
		if existing_pe:
			return {"status": "error", "message": f"Payment Entry {existing_pe[0].parent} already exists"}
		
		# Check if order is paid
		financial_status = (invoice.get(ORDER_STATUS_FIELD) or "").lower().strip()
		if financial_status != "paid":
			return {"status": "error", "message": f"Order status is '{financial_status}', not 'paid'"}
		
		# Check outstanding amount
		if invoice.outstanding_amount <= 0:
			return {"status": "error", "message": "Invoice has no outstanding amount"}
		
		# Get store settings
		store_name = invoice.get(STORE_LINK_FIELD)
		if not store_name:
			return {"status": "error", "message": "No Shopify Store linked to this invoice"}
		
		setting = frappe.get_doc(STORE_DOCTYPE, store_name)
		
		# Call the existing payment creation function
		create_payment_entry_for_invoice(invoice, setting)
		
		frappe.db.commit()
		
		# Check if payment was created
		new_pe = frappe.db.sql("""
			SELECT parent FROM `tabPayment Entry Reference`
			WHERE reference_doctype = 'Sales Invoice'
			AND reference_name = %s
			AND docstatus != 2
			LIMIT 1
		""", (invoice.name,), as_dict=True)
		
		if new_pe:
			return {
				"status": "success",
				"message": f"Payment Entry {new_pe[0].parent} created",
				"payment_entry": new_pe[0].parent
			}
		else:
			return {"status": "error", "message": "Payment Entry creation failed - check Error Log"}
		
	except Exception as e:
		frappe.log_error(
			message=f"Failed to create Payment for {invoice_name}: {str(e)}\n{frappe.get_traceback()}",
			title="Create Missing Payment Error"
		)
		return {"status": "error", "message": str(e)}


def repair_submitted_invoices(store_name=None, dry_run=True):
	"""Find and repair submitted Shopify invoices missing DN or Payment.
	
	This can be run from bench console to fix invoices in bulk:
	
		from ecommerce_integrations_multistore.shopify.invoice import repair_submitted_invoices
		repair_submitted_invoices(dry_run=False)
	
	Args:
		store_name: Optional - limit to specific store
		dry_run: If True, only report what would be done without making changes
		
	Returns:
		dict: Summary of repairs made
	"""
	filters = {
		"docstatus": 1,  # Submitted only
		ORDER_ID_FIELD: ["is", "set"],
	}
	
	if store_name:
		filters[STORE_LINK_FIELD] = store_name
	
	# Find submitted Shopify invoices
	invoices = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=["name", ORDER_ID_FIELD, ORDER_STATUS_FIELD, STORE_LINK_FIELD, "outstanding_amount"]
	)
	
	print(f"Found {len(invoices)} submitted Shopify invoices")
	
	missing_dn = []
	missing_payment = []
	
	for inv in invoices:
		# Check for missing DN
		dn_exists = frappe.db.exists(
			"Delivery Note",
			{ORDER_ID_FIELD: inv.get(ORDER_ID_FIELD), "docstatus": ["!=", 2]}
		)
		
		if not dn_exists:
			missing_dn.append(inv.name)
		
		# Check for missing Payment (only if paid and has outstanding)
		financial_status = (inv.get(ORDER_STATUS_FIELD) or "").lower().strip()
		if financial_status == "paid" and inv.outstanding_amount > 0:
			pe_exists = frappe.db.sql("""
				SELECT 1 FROM `tabPayment Entry Reference`
				WHERE reference_doctype = 'Sales Invoice'
				AND reference_name = %s
				AND docstatus != 2
				LIMIT 1
			""", (inv.name,))
			
			if not pe_exists:
				missing_payment.append(inv.name)
	
	print(f"\nMissing Delivery Notes: {len(missing_dn)}")
	print(f"Missing Payment Entries: {len(missing_payment)}")
	
	if dry_run:
		print("\n[DRY RUN] No changes made. Run with dry_run=False to repair.")
		if missing_dn:
			print(f"\nInvoices needing DN: {missing_dn[:10]}{'...' if len(missing_dn) > 10 else ''}")
		if missing_payment:
			print(f"\nInvoices needing Payment: {missing_payment[:10]}{'...' if len(missing_payment) > 10 else ''}")
		return {
			"missing_dn": missing_dn,
			"missing_payment": missing_payment,
			"dry_run": True
		}
	
	# Actually repair
	dn_created = 0
	dn_errors = []
	pe_created = 0
	pe_errors = []
	
	print("\nCreating missing Delivery Notes...")
	for inv_name in missing_dn:
		try:
			result = create_missing_delivery_note(inv_name)
			if result.get("status") == "success":
				dn_created += 1
				print(f"  ✓ {inv_name}: {result.get('delivery_note')}")
			else:
				dn_errors.append((inv_name, result.get("message")))
				print(f"  ✗ {inv_name}: {result.get('message')}")
		except Exception as e:
			dn_errors.append((inv_name, str(e)))
			print(f"  ✗ {inv_name}: {str(e)}")
	
	print("\nCreating missing Payment Entries...")
	for inv_name in missing_payment:
		try:
			result = create_missing_payment_entry(inv_name)
			if result.get("status") == "success":
				pe_created += 1
				print(f"  ✓ {inv_name}: {result.get('payment_entry')}")
			else:
				pe_errors.append((inv_name, result.get("message")))
				print(f"  ✗ {inv_name}: {result.get('message')}")
		except Exception as e:
			pe_errors.append((inv_name, str(e)))
			print(f"  ✗ {inv_name}: {str(e)}")
	
	print(f"\n=== REPAIR SUMMARY ===")
	print(f"Delivery Notes created: {dn_created}/{len(missing_dn)}")
	print(f"Payment Entries created: {pe_created}/{len(missing_payment)}")
	
	if dn_errors:
		print(f"\nDN Errors ({len(dn_errors)}):")
		for inv, err in dn_errors[:5]:
			print(f"  {inv}: {err}")
	
	if pe_errors:
		print(f"\nPayment Errors ({len(pe_errors)}):")
		for inv, err in pe_errors[:5]:
			print(f"  {inv}: {err}")
	
	return {
		"dn_created": dn_created,
		"dn_errors": dn_errors,
		"pe_created": pe_created,
		"pe_errors": pe_errors
	}
