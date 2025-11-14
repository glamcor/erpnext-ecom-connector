import json

import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from frappe.utils import cint, cstr, getdate, nowdate

from ecommerce_integrations_multistore.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
	STORE_DOCTYPE,
	STORE_LINK_FIELD,
)
from ecommerce_integrations_multistore.shopify.utils import create_shopify_log


def prepare_sales_invoice(payload, request_id=None, store_name=None):
	"""Update payment status on existing Sales Invoice.
	
	Since we now create Sales Invoice on orders/create webhook,
	this webhook only updates the payment status.
	
	Args:
	    payload: Shopify order data
	    request_id: Integration log ID
	    store_name: Shopify Store name (multi-store support)
	"""
	order = payload

	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	try:
		# Look for existing Sales Invoice
		sales_invoice = frappe.db.get_value(
			"Sales Invoice", 
			{ORDER_ID_FIELD: cstr(order["id"])}, 
			["name", "docstatus"],
			as_dict=True
		)
		
		if sales_invoice:
			# Update payment status
			si = frappe.get_doc("Sales Invoice", sales_invoice.name)
			
			# Update financial status
			if ORDER_NUMBER_FIELD in si.meta.fields:
				si.db_set(ORDER_NUMBER_FIELD, order.get("name"))
			
			# Add comment about payment received
			si.add_comment(
				comment_type="Info",
				text=f"Payment received via Shopify. Financial status: {order.get('financial_status')}"
			)
			
			create_shopify_log(status="Success", message="Payment status updated", store_name=store_name)
		else:
			create_shopify_log(
				status="Invalid",
				message="Sales Invoice not found for updating payment status.",
				store_name=store_name
			)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True, store_name=store_name)


def auto_create_delivery_note(doc, method=None):
	"""Automatically create Delivery Note when Sales Invoice is submitted.
	
	This is called via hooks when a Sales Invoice is submitted.
	Only creates DN for invoices that originated from Shopify.
	
	Args:
	    doc: Sales Invoice document
	    method: Hook method name (not used)
	"""
	# Debug log - FIRST THING
	frappe.log_error(
		message=f"auto_create_delivery_note START - invoice {doc.name}",
		title="DN Hook Entry Point"
	)
	
	# Debug log
	frappe.log_error(
		message=f"auto_create_delivery_note called for invoice {doc.name}, ORDER_ID_FIELD: {doc.get(ORDER_ID_FIELD)}",
		title="Shopify Hook Debug"
	)
	
	# Check if this is a Shopify invoice
	if not doc.get(ORDER_ID_FIELD):
		frappe.log_error(
			message=f"Invoice {doc.name} is not a Shopify invoice - no {ORDER_ID_FIELD} field",
			title="Shopify Hook Skipped"
		)
		return
	
	# Wrap entire function in try-catch to catch any error
	try:
		frappe.log_error(
			message=f"Step 1: Starting delivery note check",
			title="DN Creation Debug 1"
		)
		
		# Check if delivery note already exists for this store
		order_id = doc.get(ORDER_ID_FIELD)
		frappe.log_error(
			message=f"Step 2: Order ID = {order_id}",
			title="DN Creation Debug 2"
		)
		
		dn_filters = {ORDER_ID_FIELD: order_id}
		store_name = doc.get(STORE_LINK_FIELD)
		frappe.log_error(
			message=f"Step 3: Store name = {store_name}",
			title="DN Creation Debug 3"
		)
		
		if store_name:
			dn_filters[STORE_LINK_FIELD] = store_name
			
		frappe.log_error(
			message=f"Step 4: DN filters = {dn_filters}",
			title="DN Creation Debug 4"
		)
		
		existing_dn = frappe.db.get_value(
			"Delivery Note",
			dn_filters,
			"name"
		)
		
		frappe.log_error(
			message=f"Step 5: Existing DN = {existing_dn}",
			title="DN Creation Debug 5"
		)
		
		if existing_dn:
			# Delivery Note already exists
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
		
		# Create delivery note
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
		
		# Send to ShipStation if configured
		from ecommerce_integrations_multistore.shopify.fulfillment import send_to_shipstation
		send_to_shipstation(dn, setting)
		
		# Create payment entry if order is paid
		create_payment_entry_for_invoice(doc, setting)
		
	except Exception as e:
		# Log error but don't fail the invoice submission
		frappe.log_error(
			message=f"Failed to auto-create delivery note for invoice {doc.name}: {str(e)}\n{frappe.get_traceback()}",
			title="Auto Delivery Note Error - Full Trace"
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
		
		# If not paid according to the invoice field, check integration logs as fallback
		if financial_status != "paid":
			# Get the original order data from the integration log
			order_data = frappe.db.get_value(
				"Ecommerce Integration Log",
				{
					"request_data": ["like", f'%"id": {order_id}%'],
					"method": ["like", "%sync_sales_order%"],
					"status": "Success"
				},
				"request_data"
			)
			
			if not order_data:
				# Try to find from order update logs as well
				order_data = frappe.db.get_value(
					"Ecommerce Integration Log",
					{
						"request_data": ["like", f'%"id": {order_id}%'],
						"method": ["like", "%handle_order_update%"],
						"status": "Success"
					},
					"request_data",
					order="modified desc"
				)
			
			if not order_data:
				frappe.log_error(
					message=f"Invoice {invoice.name} has financial status '{financial_status}' - not creating payment entry",
					title="Payment Entry Skipped - Not Paid"
				)
				return
			
			try:
				shopify_order = json.loads(order_data)
				financial_status = shopify_order.get("financial_status")
			except:
				return
		else:
			# Create a minimal shopify_order dict for downstream use
			shopify_order = {
				"id": order_id,
				"name": invoice.get(ORDER_NUMBER_FIELD),
				"financial_status": financial_status,
				"created_at": invoice.posting_date,
				"gateway": "shopify"  # Default gateway
			}
		
		# Check if order is paid
		if financial_status != "paid":
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
			return
		
		# Get payment account from sales channel mapping
		from ecommerce_integrations_multistore.shopify.order import _get_channel_financials
		cost_center, cash_bank_account = _get_channel_financials(shopify_order, setting)
		
		# If no channel-specific account, use company default
		if not cash_bank_account:
			cash_bank_account = setting.cash_bank_account
		
		if not cash_bank_account:
			frappe.log_error(
				message=f"No cash/bank account configured for payment entry creation",
				title="Payment Entry Configuration Missing"
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
		
		# Add payment gateway info if available
		gateway = shopify_order.get("gateway", "").replace("_", " ").title()
		if gateway:
			pe.remarks = f"Payment via {gateway} - Shopify Order {shopify_order.get('name')}"
		
		pe.save(ignore_permissions=True)
		pe.submit()
		
		# Add comment to invoice
		invoice.add_comment(
			comment_type="Info",
			text=f"Payment Entry {pe.name} created automatically"
		)
		
		frappe.log_error(
			message=f"Auto-created Payment Entry {pe.name} for Sales Invoice {invoice.name}",
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
			make_payament_entry_against_sales_invoice(sales_invoice, setting, posting_date)

		if shopify_order.get("note"):
			sales_invoice.add_comment(text=f"Order Note: {shopify_order.get('note')}")


def set_cost_center(items, cost_center):
	for item in items:
		item.cost_center = cost_center


def make_payament_entry_against_sales_invoice(doc, setting, posting_date=None):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment_entry = get_payment_entry(doc.doctype, doc.name, bank_account=setting.cash_bank_account)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = doc.name
	payment_entry.posting_date = posting_date or nowdate()
	payment_entry.reference_date = posting_date or nowdate()
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
