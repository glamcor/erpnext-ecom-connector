#!/usr/bin/env python
"""Manually process a queued Shopify job to see any errors."""

import frappe
import json

def process_queued_job(log_name=None):
    """Manually process a queued integration log."""
    
    # Connect to site
    frappe.init(site="glamcor-private.v.frappe.cloud")  # Update with your site name
    frappe.connect()
    frappe.set_user("Administrator")
    
    try:
        if not log_name:
            # Get the most recent queued job
            log_name = frappe.get_value(
                "Ecommerce Integration Log",
                {"status": "Queued"},
                "name",
                order_by="creation desc"
            )
        
        if not log_name:
            print("No queued jobs found")
            return
        
        print(f"Processing job: {log_name}")
        
        # Get the log
        log = frappe.get_doc("Ecommerce Integration Log", log_name)
        print(f"Method: {log.method}")
        print(f"Store: {log.shopify_store}")
        
        # Get the method to call
        method_path = log.method
        module_path, method_name = method_path.rsplit(".", 1)
        
        # Import the module and get the method
        import importlib
        module = importlib.import_module(module_path)
        method = getattr(module, method_name)
        
        # Parse request data
        payload = json.loads(log.request_data) if log.request_data else {}
        
        print(f"\nCalling {method_name}...")
        
        # Call the method
        result = method(
            payload=payload,
            request_id=log.name,
            store_name=log.shopify_store
        )
        
        print("Method executed successfully!")
        
        # Check if log status was updated
        log.reload()
        print(f"Log status after execution: {log.status}")
        
    except Exception as e:
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
    
    finally:
        frappe.destroy()

if __name__ == "__main__":
    import sys
    log_name = sys.argv[1] if len(sys.argv) > 1 else None
    process_queued_job(log_name)
