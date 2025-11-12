#!/usr/bin/env python
"""Fix queued Shopify orders by reprocessing them."""

import frappe
import json
from frappe.utils import now_datetime, add_to_date

def fix_queued_orders(hours=24, limit=50):
    """Reprocess queued Shopify orders.
    
    Args:
        hours: Look back this many hours for queued orders
        limit: Process this many orders
    """
    frappe.init(site="glamcor-private.v.frappe.cloud")  # Update with your site
    frappe.connect()
    frappe.set_user("Administrator")
    
    try:
        # Get queued orders
        since = add_to_date(now_datetime(), hours=-hours)
        
        queued_logs = frappe.get_all(
            "Ecommerce Integration Log",
            filters={
                "status": "Queued",
                "method": ["like", "%shopify%"],
                "creation": [">", since]
            },
            fields=["name", "method", "request_data", "shopify_store", "creation"],
            order_by="creation asc",
            limit=limit
        )
        
        print(f"Found {len(queued_logs)} queued Shopify jobs from the last {hours} hours")
        
        processed = 0
        errors = 0
        
        for log in queued_logs:
            try:
                print(f"\nProcessing {log.name} - {log.method}")
                
                # Get the full log document
                log_doc = frappe.get_doc("Ecommerce Integration Log", log.name)
                
                # Parse the request data
                request_data = json.loads(log_doc.request_data) if log_doc.request_data else {}
                
                # Import and call the method
                method_path = log_doc.method
                module_path, method_name = method_path.rsplit(".", 1)
                
                import importlib
                module = importlib.import_module(module_path)
                method = getattr(module, method_name)
                
                # Set the request_id flag so the log gets updated
                frappe.flags.request_id = log.name
                
                # Call the method
                result = method(
                    payload=request_data,
                    request_id=log.name,
                    store_name=log_doc.shopify_store
                )
                
                # Check the status after processing
                log_doc.reload()
                print(f"  Status after processing: {log_doc.status}")
                
                if log_doc.status != "Queued":
                    processed += 1
                
            except Exception as e:
                errors += 1
                print(f"  ERROR: {str(e)}")
                
                # Update the log with error
                frappe.db.set_value(
                    "Ecommerce Integration Log",
                    log.name,
                    {
                        "status": "Error",
                        "message": str(e),
                        "traceback": frappe.get_traceback()
                    }
                )
                frappe.db.commit()
        
        print(f"\n=== Summary ===")
        print(f"Total processed: {processed}")
        print(f"Errors: {errors}")
        print(f"Still queued: {len(queued_logs) - processed - errors}")
        
    except Exception as e:
        print(f"Fatal error: {str(e)}")
        import traceback
        traceback.print_exc()
    
    finally:
        frappe.destroy()

if __name__ == "__main__":
    fix_queued_orders()
