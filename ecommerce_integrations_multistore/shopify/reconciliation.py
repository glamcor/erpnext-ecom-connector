# Copyright (c) 2025, Frappe and contributors
# For license information, please see LICENSE

"""
Shopify Order Reconciliation

This module provides tools to find and fix discrepancies between Shopify orders
and ERPNext documents (Sales Invoices, Payment Entries, Delivery Notes).

Usage:
    1. Run reconcile_shopify_orders() to get a report of discrepancies
    2. Run fix_missing_payments(), fix_missing_delivery_notes(), etc. to fix issues
"""

import json
import frappe
from frappe import _
from frappe.utils import cint, cstr, getdate, nowdate, add_days, get_datetime
from shopify.resources import Order

from shopify.collection import PaginatedIterator

from ecommerce_integrations_multistore.shopify.connection import temp_shopify_session
from ecommerce_integrations_multistore.shopify.constants import (
    ORDER_ID_FIELD,
    ORDER_NUMBER_FIELD,
    STORE_DOCTYPE,
)


@frappe.whitelist()
def reconcile_shopify_orders(
    store_name,
    date_from=None,
    date_to=None,
    order_from=None,
    order_to=None,
    check_invoices=True,
    check_payments=True,
    check_delivery_notes=True,
    check_tracking=True
):
    """
    Compare Shopify orders against ERPNext to find discrepancies.
    
    Args:
        store_name: Shopify Store name (required)
        date_from: Start date for order range (YYYY-MM-DD)
        date_to: End date for order range (YYYY-MM-DD)
        order_from: Starting order number (e.g., "RLR150000")
        order_to: Ending order number (e.g., "RLR151000")
        check_invoices: Check for missing Sales Invoices
        check_payments: Check for missing Payment Entries
        check_delivery_notes: Check for missing Delivery Notes
        check_tracking: Check for missing tracking info
    
    Returns:
        dict: Summary and details of discrepancies found
    """
    # Convert string booleans from JS
    check_invoices = cint(check_invoices)
    check_payments = cint(check_payments)
    check_delivery_notes = cint(check_delivery_notes)
    check_tracking = cint(check_tracking)
    
    if not store_name:
        frappe.throw(_("Store name is required"))
    
    store = frappe.get_doc(STORE_DOCTYPE, store_name)
    if not store.is_enabled():
        frappe.throw(_("Store {0} is not enabled").format(store_name))
    
    # Default date range: last 30 days
    if not date_from:
        date_from = add_days(nowdate(), -30)
    if not date_to:
        date_to = nowdate()
    
    # Fetch orders from Shopify
    shopify_orders = _fetch_shopify_orders_for_reconciliation(
        store_name, date_from, date_to, order_from, order_to
    )
    
    # Initialize results
    results = {
        "store": store_name,
        "date_from": str(date_from),
        "date_to": str(date_to),
        "order_from": order_from,
        "order_to": order_to,
        "total_shopify_orders": 0,
        "missing_invoices": [],
        "missing_payments": [],
        "missing_delivery_notes": [],
        "missing_tracking": [],
        "summary": {}
    }
    
    for order in shopify_orders:
        results["total_shopify_orders"] += 1
        order_id = cstr(order.get("id"))
        order_number = order.get("name", "")
        order_date = order.get("created_at", "")[:10] if order.get("created_at") else ""
        financial_status = order.get("financial_status", "")
        fulfillment_status = order.get("fulfillment_status", "")
        
        # Filter by order number if specified
        if order_from or order_to:
            # Extract numeric part from order number (e.g., "RLR150000" -> 150000)
            order_num = _extract_order_number(order_number)
            from_num = _extract_order_number(order_from) if order_from else 0
            to_num = _extract_order_number(order_to) if order_to else float('inf')
            
            if order_num < from_num or order_num > to_num:
                results["total_shopify_orders"] -= 1  # Don't count filtered orders
                continue
        
        order_info = {
            "order_id": order_id,
            "order_number": order_number,
            "order_date": order_date,
            "financial_status": financial_status,
            "fulfillment_status": fulfillment_status,
            "total": order.get("total_price", "0"),
            "customer": _get_customer_name(order),
        }
        
        # Check for Sales Invoice
        invoice = frappe.db.get_value(
            "Sales Invoice",
            {ORDER_ID_FIELD: order_id},
            ["name", "docstatus", "outstanding_amount"],
            as_dict=True
        )
        
        if check_invoices and not invoice:
            # Only flag if order is paid (we expect an invoice for paid orders)
            if financial_status in ("paid", "partially_paid"):
                results["missing_invoices"].append(order_info)
        
        if invoice:
            # Check for Payment Entry
            if check_payments and financial_status == "paid":
                # Check if invoice has outstanding amount (not fully paid)
                if invoice.docstatus == 1 and float(invoice.outstanding_amount or 0) > 0:
                    order_info["invoice"] = invoice.name
                    order_info["outstanding"] = invoice.outstanding_amount
                    results["missing_payments"].append(order_info)
            
            # Check for Delivery Note
            if check_delivery_notes and fulfillment_status in ("fulfilled", "partial"):
                dn = frappe.db.get_value(
                    "Delivery Note",
                    {ORDER_ID_FIELD: order_id, "docstatus": ["!=", 2]},
                    "name"
                )
                if not dn:
                    order_info["invoice"] = invoice.name
                    order_info["fulfillments"] = _get_fulfillment_info(order)
                    results["missing_delivery_notes"].append(order_info)
            
            # Check for tracking info
            if check_tracking and fulfillment_status in ("fulfilled", "partial"):
                dn = frappe.db.get_value(
                    "Delivery Note",
                    {ORDER_ID_FIELD: order_id, "docstatus": ["!=", 2]},
                    ["name", "custom_shipstation_tracking_number"],
                    as_dict=True
                )
                if dn and not dn.custom_shipstation_tracking_number:
                    # Get tracking from Shopify
                    shopify_tracking = _get_shopify_tracking(order)
                    if shopify_tracking:
                        order_info["delivery_note"] = dn.name
                        order_info["shopify_tracking"] = shopify_tracking
                        results["missing_tracking"].append(order_info)
    
    # Build summary
    results["summary"] = {
        "total_orders_checked": results["total_shopify_orders"],
        "missing_invoices_count": len(results["missing_invoices"]),
        "missing_payments_count": len(results["missing_payments"]),
        "missing_delivery_notes_count": len(results["missing_delivery_notes"]),
        "missing_tracking_count": len(results["missing_tracking"]),
    }
    
    return results


@frappe.whitelist()
def fix_missing_invoices(store_name, order_ids=None):
    """
    Create Sales Invoices for orders that are missing them.
    
    Args:
        store_name: Shopify Store name
        order_ids: JSON list of Shopify order IDs to process (or None for all from last reconciliation)
    
    Returns:
        dict: Results of the fix operation
    """
    from ecommerce_integrations_multistore.shopify.order import sync_sales_order
    
    if isinstance(order_ids, str):
        order_ids = json.loads(order_ids)
    
    if not order_ids:
        frappe.throw(_("No order IDs provided"))
    
    results = {
        "success": 0,
        "failed": 0,
        "details": []
    }
    
    for order_id in order_ids:
        try:
            # Fetch order from Shopify
            shopify_order = _fetch_single_order(store_name, order_id)
            if not shopify_order:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "status": "failed",
                    "reason": "Order not found in Shopify"
                })
                continue
            
            # Create invoice using existing sync function
            # bypass_cutoff=True because reconciliation is intentionally fixing old orders
            sync_sales_order(shopify_order, store_name=store_name, bypass_cutoff=True)
            
            # Verify invoice was created
            invoice = frappe.db.get_value(
                "Sales Invoice",
                {ORDER_ID_FIELD: cstr(order_id)},
                "name"
            )
            
            if invoice:
                results["success"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "order_number": shopify_order.get("name"),
                    "status": "success",
                    "invoice": invoice
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "order_number": shopify_order.get("name"),
                    "status": "failed",
                    "reason": "Invoice not created (check Error Log for details)"
                })
            
            frappe.db.commit()
            
        except Exception as e:
            results["failed"] += 1
            results["details"].append({
                "order_id": order_id,
                "status": "failed",
                "reason": str(e)[:200]
            })
            frappe.log_error(
                message=f"Failed to create invoice for order {order_id}: {str(e)}",
                title="Reconciliation - Fix Invoice Failed"
            )
    
    return results


@frappe.whitelist()
def fix_missing_payments(store_name, order_ids=None):
    """
    Create Payment Entries for invoices that are missing them.
    
    Args:
        store_name: Shopify Store name
        order_ids: JSON list of Shopify order IDs to process
    
    Returns:
        dict: Results of the fix operation
    """
    from ecommerce_integrations_multistore.shopify.invoice import create_payment_entry_for_invoice
    
    if isinstance(order_ids, str):
        order_ids = json.loads(order_ids)
    
    if not order_ids:
        frappe.throw(_("No order IDs provided"))
    
    store = frappe.get_doc(STORE_DOCTYPE, store_name)
    
    results = {
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "details": []
    }
    
    for order_id in order_ids:
        try:
            # Get the invoice
            invoice = frappe.db.get_value(
                "Sales Invoice",
                {ORDER_ID_FIELD: cstr(order_id)},
                ["name", "docstatus", "outstanding_amount", "grand_total", ORDER_NUMBER_FIELD],
                as_dict=True
            )
            
            if not invoice:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "status": "failed",
                    "reason": "Invoice not found"
                })
                continue
            
            if invoice.docstatus != 1:
                results["skipped"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "invoice": invoice.name,
                    "status": "skipped",
                    "reason": "Invoice not submitted"
                })
                continue
            
            if float(invoice.outstanding_amount or 0) <= 0:
                results["skipped"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "invoice": invoice.name,
                    "status": "skipped",
                    "reason": "Invoice already paid"
                })
                continue
            
            # Create payment entry using existing function
            # The function reads shopify order data from the invoice's custom fields
            invoice_doc = frappe.get_doc("Sales Invoice", invoice.name)
            
            # Check existing PE before calling
            existing_pe = frappe.db.sql("""
                SELECT parent FROM `tabPayment Entry Reference`
                WHERE reference_doctype = 'Sales Invoice'
                AND reference_name = %s
                AND docstatus != 2
                LIMIT 1
            """, (invoice.name,), as_dict=True)
            
            if existing_pe:
                results["skipped"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "invoice": invoice.name,
                    "status": "skipped",
                    "reason": f"Payment Entry {existing_pe[0].parent} already exists"
                })
                continue
            
            create_payment_entry_for_invoice(invoice_doc, store)
            
            # Verify PE was created
            new_pe = frappe.db.sql("""
                SELECT parent FROM `tabPayment Entry Reference`
                WHERE reference_doctype = 'Sales Invoice'
                AND reference_name = %s
                AND docstatus != 2
                LIMIT 1
            """, (invoice.name,), as_dict=True)
            
            if new_pe:
                results["success"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "invoice": invoice.name,
                    "status": "success",
                    "payment_entry": new_pe[0].parent
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "invoice": invoice.name,
                    "status": "failed",
                    "reason": "Payment entry not created (check error log)"
                })
            
            frappe.db.commit()
            
        except Exception as e:
            results["failed"] += 1
            results["details"].append({
                "order_id": order_id,
                "status": "failed",
                "reason": str(e)[:200]
            })
            frappe.log_error(
                message=f"Failed to create payment for order {order_id}: {str(e)}",
                title="Reconciliation - Fix Payment Failed"
            )
    
    return results


@frappe.whitelist()
def fix_missing_delivery_notes(store_name, order_ids=None):
    """
    Create Delivery Notes for fulfilled orders that are missing them.
    
    Args:
        store_name: Shopify Store name
        order_ids: JSON list of Shopify order IDs to process
    
    Returns:
        dict: Results of the fix operation
    """
    from ecommerce_integrations_multistore.shopify.fulfillment import create_delivery_note
    
    if isinstance(order_ids, str):
        order_ids = json.loads(order_ids)
    
    if not order_ids:
        frappe.throw(_("No order IDs provided"))
    
    store = frappe.get_doc(STORE_DOCTYPE, store_name)
    
    results = {
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "details": []
    }
    
    for order_id in order_ids:
        try:
            # Get the invoice
            invoice = frappe.db.get_value(
                "Sales Invoice",
                {ORDER_ID_FIELD: cstr(order_id)},
                ["name", "docstatus"],
                as_dict=True
            )
            
            if not invoice:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "status": "failed",
                    "reason": "Invoice not found"
                })
                continue
            
            if invoice.docstatus != 1:
                results["skipped"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "invoice": invoice.name,
                    "status": "skipped",
                    "reason": "Invoice not submitted"
                })
                continue
            
            # Check if DN already exists
            existing_dn = frappe.db.get_value(
                "Delivery Note",
                {ORDER_ID_FIELD: cstr(order_id), "docstatus": ["!=", 2]},
                "name"
            )
            
            if existing_dn:
                results["skipped"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "invoice": invoice.name,
                    "status": "skipped",
                    "reason": f"Delivery Note {existing_dn} already exists"
                })
                continue
            
            # Fetch order from Shopify
            shopify_order = _fetch_single_order(store_name, order_id)
            if not shopify_order:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "status": "failed",
                    "reason": "Order not found in Shopify"
                })
                continue
            
            # Check if order is fulfilled
            if not shopify_order.get("fulfillments"):
                results["skipped"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "invoice": invoice.name,
                    "status": "skipped",
                    "reason": "Order has no fulfillments in Shopify"
                })
                continue
            
            # Create delivery note
            invoice_doc = frappe.get_doc("Sales Invoice", invoice.name)
            create_delivery_note(shopify_order, store, invoice_doc, store_name=store_name)
            
            # Verify DN was created
            dn = frappe.db.get_value(
                "Delivery Note",
                {ORDER_ID_FIELD: cstr(order_id), "docstatus": ["!=", 2]},
                "name"
            )
            
            if dn:
                results["success"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "order_number": shopify_order.get("name"),
                    "invoice": invoice.name,
                    "status": "success",
                    "delivery_note": dn
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "order_number": shopify_order.get("name"),
                    "status": "failed",
                    "reason": "Delivery Note not created"
                })
            
            frappe.db.commit()
            
        except Exception as e:
            results["failed"] += 1
            results["details"].append({
                "order_id": order_id,
                "status": "failed",
                "reason": str(e)[:200]
            })
            frappe.log_error(
                message=f"Failed to create delivery note for order {order_id}: {str(e)}",
                title="Reconciliation - Fix DN Failed"
            )
    
    return results


@frappe.whitelist()
def fix_missing_tracking(store_name, order_ids=None):
    """
    Update Delivery Notes with tracking info from Shopify.
    
    Args:
        store_name: Shopify Store name
        order_ids: JSON list of Shopify order IDs to process
    
    Returns:
        dict: Results of the fix operation
    """
    if isinstance(order_ids, str):
        order_ids = json.loads(order_ids)
    
    if not order_ids:
        frappe.throw(_("No order IDs provided"))
    
    results = {
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "details": []
    }
    
    for order_id in order_ids:
        try:
            # Get the delivery note
            dn = frappe.db.get_value(
                "Delivery Note",
                {ORDER_ID_FIELD: cstr(order_id), "docstatus": ["!=", 2]},
                ["name", "custom_shipstation_tracking_number"],
                as_dict=True
            )
            
            if not dn:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "status": "failed",
                    "reason": "Delivery Note not found"
                })
                continue
            
            if dn.custom_shipstation_tracking_number:
                results["skipped"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "delivery_note": dn.name,
                    "status": "skipped",
                    "reason": "Already has tracking"
                })
                continue
            
            # Fetch order from Shopify
            shopify_order = _fetch_single_order(store_name, order_id)
            if not shopify_order:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "status": "failed",
                    "reason": "Order not found in Shopify"
                })
                continue
            
            # Get tracking from fulfillments
            tracking_info = _get_shopify_tracking(shopify_order)
            
            if not tracking_info:
                results["skipped"] += 1
                results["details"].append({
                    "order_id": order_id,
                    "delivery_note": dn.name,
                    "status": "skipped",
                    "reason": "No tracking in Shopify"
                })
                continue
            
            # Update delivery note with tracking
            frappe.db.set_value(
                "Delivery Note",
                dn.name,
                {
                    "custom_shipstation_tracking_number": tracking_info.get("tracking_number"),
                    "custom_shipstation_carrier": tracking_info.get("carrier")
                }
            )
            
            results["success"] += 1
            results["details"].append({
                "order_id": order_id,
                "order_number": shopify_order.get("name"),
                "delivery_note": dn.name,
                "status": "success",
                "tracking_number": tracking_info.get("tracking_number"),
                "carrier": tracking_info.get("carrier")
            })
            
            frappe.db.commit()
            
        except Exception as e:
            results["failed"] += 1
            results["details"].append({
                "order_id": order_id,
                "status": "failed",
                "reason": str(e)[:200]
            })
            frappe.log_error(
                message=f"Failed to update tracking for order {order_id}: {str(e)}",
                title="Reconciliation - Fix Tracking Failed"
            )
    
    return results


# Helper functions

def _fetch_shopify_orders_for_reconciliation(store_name, date_from, date_to, order_from=None, order_to=None):
    """Fetch orders from Shopify for reconciliation.
    
    If specific order numbers are provided, fetches by order name (much faster).
    Otherwise fetches by date range.
    """
    
    @temp_shopify_session
    def fetch_orders_by_date(store_name=None):
        from_time = get_datetime(date_from).replace(hour=0, minute=0, second=0).astimezone().isoformat()
        to_time = get_datetime(date_to).replace(hour=23, minute=59, second=59).astimezone().isoformat()
        
        # Fetch orders with pagination
        orders_iterator = PaginatedIterator(
            Order.find(
                created_at_min=from_time,
                created_at_max=to_time,
                status="any",  # Include all statuses
                limit=250
            )
        )
        
        orders = []
        for order_batch in orders_iterator:
            for order in order_batch:
                orders.append(order.to_dict())
        
        return orders
    
    @temp_shopify_session
    def fetch_orders_by_name(order_name, store_name=None):
        """Fetch orders by name/number - Shopify supports this directly."""
        try:
            # Shopify API allows searching by name
            orders = Order.find(name=order_name, status="any")
            return [o.to_dict() for o in orders] if orders else []
        except Exception as e:
            frappe.log_error(
                message=f"Failed to fetch order by name {order_name}: {str(e)}",
                title="Reconciliation - Fetch by Name Failed"
            )
            return []
    
    @temp_shopify_session
    def fetch_orders_by_name_range(order_from, order_to, store_name=None):
        """Fetch a range of orders by iterating through order numbers."""
        from_num = _extract_order_number(order_from)
        to_num = _extract_order_number(order_to)
        
        # Extract prefix (e.g., "RLR" from "RLR155184")
        import re
        prefix_match = re.match(r'^([A-Za-z]+)', order_from or order_to or "")
        prefix = prefix_match.group(1) if prefix_match else ""
        
        orders = []
        # Limit to 100 orders max to prevent timeout
        max_orders = min(to_num - from_num + 1, 100)
        
        for i in range(max_orders):
            order_name = f"{prefix}{from_num + i}"
            try:
                found_orders = Order.find(name=order_name, status="any")
                if found_orders:
                    for o in found_orders:
                        orders.append(o.to_dict())
            except Exception:
                pass  # Order might not exist, continue
        
        return orders
    
    # If single order specified (from == to), fetch directly by name
    if order_from and order_to and order_from == order_to:
        return fetch_orders_by_name(order_from, store_name=store_name)
    
    # If order range specified, fetch by iterating (faster than date range for small ranges)
    if order_from and order_to:
        from_num = _extract_order_number(order_from)
        to_num = _extract_order_number(order_to)
        if to_num - from_num <= 100:  # Only use this method for ranges <= 100 orders
            return fetch_orders_by_name_range(order_from, order_to, store_name=store_name)
    
    # Otherwise fetch by date range
    return fetch_orders_by_date(store_name=store_name)


def _fetch_single_order(store_name, order_id):
    """Fetch a single order from Shopify."""
    
    @temp_shopify_session
    def fetch_order(order_id, store_name=None):
        try:
            order = Order.find(order_id)
            if order:
                return order.to_dict()
        except Exception as e:
            frappe.log_error(
                message=f"Failed to fetch order {order_id}: {str(e)}",
                title="Reconciliation - Fetch Order Failed"
            )
        return None
    
    return fetch_order(order_id, store_name=store_name)


def _extract_order_number(order_name):
    """Extract numeric part from order name (e.g., 'RLR150000' -> 150000)."""
    if not order_name:
        return 0
    # Remove all non-numeric characters
    import re
    numbers = re.sub(r'[^0-9]', '', str(order_name))
    return int(numbers) if numbers else 0


def _get_customer_name(order):
    """Get customer name from Shopify order."""
    customer = order.get("customer", {})
    if customer:
        first = customer.get("first_name", "")
        last = customer.get("last_name", "")
        return f"{first} {last}".strip() or customer.get("email", "Unknown")
    
    shipping = order.get("shipping_address", {})
    if shipping:
        return shipping.get("name", "Unknown")
    
    return "Unknown"


def _get_fulfillment_info(order):
    """Get fulfillment info from Shopify order."""
    fulfillments = order.get("fulfillments", [])
    info = []
    for f in fulfillments:
        info.append({
            "id": f.get("id"),
            "status": f.get("status"),
            "tracking_number": f.get("tracking_number"),
            "tracking_company": f.get("tracking_company"),
            "created_at": f.get("created_at", "")[:10] if f.get("created_at") else ""
        })
    return info


def _get_shopify_tracking(order):
    """Get tracking info from Shopify fulfillments."""
    fulfillments = order.get("fulfillments", [])
    for f in fulfillments:
        tracking = f.get("tracking_number")
        if tracking:
            return {
                "tracking_number": tracking,
                "carrier": f.get("tracking_company", "")
            }
    return None

