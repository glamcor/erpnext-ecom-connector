#!/usr/bin/env python
"""Test processing a single Shopify order to debug queued status issue."""

import frappe
import json

def test_process_order(log_name):
    """Manually process a single order to see what happens."""
    
    frappe.init(site="glamcor-private.v.frappe.cloud")  # Update with your site
    frappe.connect()
    frappe.set_user("Administrator")
    
    print(f"Testing order processing for log: {log_name}")
    
    try:
        # Get the log
        log = frappe.get_doc("Ecommerce Integration Log", log_name)
        print(f"Initial status: {log.status}")
        print(f"Method: {log.method}")
        
        # Parse request data
        order_data = json.loads(log.request_data) if log.request_data else {}
        print(f"Order: {order_data.get('name', 'Unknown')}")
        print(f"Customer email: {order_data.get('customer', {}).get('email', 'No email')}")
        
        # Import the sync function
        from ecommerce_integrations_multistore.shopify.order import sync_sales_order
        
        # IMPORTANT: Set the request_id flag
        frappe.flags.request_id = log.name
        print(f"Set frappe.flags.request_id = {log.name}")
        
        # Call the function
        print("\nCalling sync_sales_order...")
        result = sync_sales_order(
            payload=order_data,
            request_id=log.name,
            store_name=log.shopify_store
        )
        
        print(f"Function returned: {result}")
        
        # Check if log was updated
        log.reload()
        print(f"\nFinal status: {log.status}")
        print(f"Message: {log.message}")
        
        # Check if invoice was created
        order_id = order_data.get("id")
        if order_id:
            invoice = frappe.db.get_value(
                "Sales Invoice",
                {"shopify_order_id": str(order_id)},
                "name"
            )
            print(f"Sales Invoice created: {invoice}")
        
    except Exception as e:
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # Check log status after error
        try:
            log.reload()
            print(f"\nLog status after error: {log.status}")
        except:
            pass
    
    finally:
        frappe.destroy()

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python test_single_order.py LOG_NAME")
        sys.exit(1)
    
    test_process_order(sys.argv[1])
