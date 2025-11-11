import frappe
from frappe import _
from frappe.utils import cint


@frappe.whitelist()
def bulk_submit_invoices(names):
	"""Bulk submit draft Sales Invoices from Shopify.
	
	Args:
	    names: List of Sales Invoice names to submit
	
	Returns:
	    dict with success count and errors
	"""
	if isinstance(names, str):
		names = frappe.parse_json(names)
	
	success_count = 0
	errors = []
	
	for name in names:
		try:
			# Get the invoice
			invoice = frappe.get_doc("Sales Invoice", name)
			
			# Check if it's a Shopify invoice
			if not invoice.get("shopify_order_id"):
				errors.append({
					"invoice": name,
					"error": "Not a Shopify invoice"
				})
				continue
			
			# Check if already submitted
			if invoice.docstatus == 1:
				errors.append({
					"invoice": name,
					"error": "Already submitted"
				})
				continue
			
			# Check if cancelled
			if invoice.docstatus == 2:
				errors.append({
					"invoice": name,
					"error": "Invoice is cancelled"
				})
				continue
			
			# Submit the invoice
			invoice.submit()
			success_count += 1
			
		except Exception as e:
			errors.append({
				"invoice": name,
				"error": str(e)
			})
	
	# Create summary message
	message = f"Successfully submitted {success_count} invoice(s)."
	if errors:
		message += f" {len(errors)} invoice(s) had errors."
	
	return {
		"success_count": success_count,
		"errors": errors,
		"message": message
	}


@frappe.whitelist()
def get_shopify_order_summary(store_name=None):
	"""Get summary of Shopify orders by status.
	
	Args:
	    store_name: Optional store filter
	
	Returns:
	    dict with order counts by status
	"""
	filters = {}
	if store_name:
		filters["shopify_store"] = store_name
	
	# Get incomplete orders count
	incomplete_orders = frappe.db.count(
		"Ecommerce Integration Log",
		filters={
			"status": "Incomplete Order",
			"method": ["like", "%sync_sales_order%"],
			**filters
		}
	)
	
	# Get draft invoices count
	draft_invoices = frappe.db.sql("""
		SELECT COUNT(*) as count
		FROM `tabSales Invoice`
		WHERE docstatus = 0
		AND shopify_order_id IS NOT NULL
		AND shopify_order_id != ''
		{store_filter}
	""".format(
		store_filter=f"AND shopify_store = %(store_name)s" if store_name else ""
	), {"store_name": store_name}, as_dict=True)[0]["count"]
	
	# Get submitted invoices count (today)
	submitted_today = frappe.db.sql("""
		SELECT COUNT(*) as count
		FROM `tabSales Invoice`
		WHERE docstatus = 1
		AND shopify_order_id IS NOT NULL
		AND shopify_order_id != ''
		AND DATE(posting_date) = CURDATE()
		{store_filter}
	""".format(
		store_filter=f"AND shopify_store = %(store_name)s" if store_name else ""
	), {"store_name": store_name}, as_dict=True)[0]["count"]
	
	# Get pending delivery notes
	pending_delivery = frappe.db.sql("""
		SELECT COUNT(*) as count
		FROM `tabSales Invoice` si
		WHERE si.docstatus = 1
		AND si.shopify_order_id IS NOT NULL
		AND si.shopify_order_id != ''
		AND NOT EXISTS (
			SELECT 1 FROM `tabDelivery Note` dn
			WHERE dn.shopify_order_id = si.shopify_order_id
			AND dn.docstatus = 1
		)
		{store_filter}
	""".format(
		store_filter=f"AND si.shopify_store = %(store_name)s" if store_name else ""
	), {"store_name": store_name}, as_dict=True)[0]["count"]
	
	return {
		"incomplete_orders": incomplete_orders,
		"draft_invoices": draft_invoices,
		"submitted_today": submitted_today,
		"pending_delivery": pending_delivery
	}


@frappe.whitelist()
def check_incomplete_orders_for_updates(store_name=None):
	"""Check incomplete orders for updates via Shopify API.
	
	Args:
	    store_name: Optional store filter
	
	Returns:
	    dict with check results
	"""
	filters = {
		"status": "Incomplete Order",
		"method": ["like", "%sync_sales_order%"]
	}
	if store_name:
		filters["shopify_store"] = store_name
	
	# Get incomplete order logs
	incomplete_logs = frappe.get_all(
		"Ecommerce Integration Log",
		filters=filters,
		fields=["name", "request_data", "shopify_store"],
		limit=50  # Process in batches
	)
	
	checked_count = 0
	now_complete = []
	
	for log in incomplete_logs:
		try:
			# Parse the order data
			order_data = frappe.parse_json(log.request_data)
			order_id = order_data.get("id")
			
			if not order_id:
				continue
			
			# Trigger order update check
			from ecommerce_integrations_multistore.shopify.order import handle_order_update
			
			# This will check if the order is now complete
			handle_order_update(order_data, store_name=log.shopify_store)
			checked_count += 1
			
		except Exception as e:
			frappe.log_error(
				message=f"Error checking order {log.name}: {str(e)}",
				title="Incomplete Order Check Error"
			)
	
	return {
		"checked_count": checked_count,
		"message": f"Checked {checked_count} incomplete orders for updates"
	}
