"""Monitoring utilities for Shopify integration."""

import frappe
from frappe import _
from frappe.utils import now_datetime, add_to_date, get_datetime_str


@frappe.whitelist()
def get_integration_health():
    """Get health status of Shopify integration.
    
    Returns dict with:
    - worker_status: Status of background workers
    - queued_jobs: Number of queued jobs
    - recent_errors: Recent error count
    - last_successful_sync: Timestamp of last successful order
    """
    # Check queued jobs in last 24 hours
    since_24h = add_to_date(now_datetime(), hours=-24)
    
    queued_count = frappe.db.count(
        "Ecommerce Integration Log",
        filters={
            "status": "Queued",
            "method": ["like", "%shopify%"],
            "creation": [">", since_24h]
        }
    )
    
    # Check recent errors
    error_count = frappe.db.count(
        "Ecommerce Integration Log",
        filters={
            "status": "Error",
            "method": ["like", "%shopify%"],
            "creation": [">", since_24h]
        }
    )
    
    # Get last successful sync
    last_success = frappe.db.get_value(
        "Ecommerce Integration Log",
        filters={
            "status": "Success",
            "method": ["like", "%shopify.order%"]
        },
        fieldname="creation",
        order_by="creation desc"
    )
    
    # Check worker status
    from frappe.utils.background_jobs import get_jobs
    
    worker_status = "Unknown"
    try:
        # If we can get job counts, workers are likely running
        short_queue = len(get_jobs(queue="short", key="queued"))
        long_queue = len(get_jobs(queue="long", key="queued"))
        
        if queued_count > 10 and not last_success:
            worker_status = "Not Processing"
        elif short_queue > 100 or long_queue > 100:
            worker_status = "Overloaded"
        else:
            worker_status = "Active"
    except:
        worker_status = "Error"
    
    return {
        "worker_status": worker_status,
        "queued_jobs": queued_count,
        "recent_errors": error_count,
        "last_successful_sync": get_datetime_str(last_success) if last_success else None,
        "health_status": "Critical" if worker_status != "Active" or queued_count > 50 else "Good"
    }


@frappe.whitelist()
def process_single_queued_job(log_name):
    """Manually process a single queued job.
    
    Args:
        log_name: Name of the Ecommerce Integration Log
        
    Returns:
        dict with processing result
    """
    frappe.only_for("System Manager")
    
    try:
        import json
        
        log = frappe.get_doc("Ecommerce Integration Log", log_name)
        
        if log.status != "Queued":
            return {"success": False, "message": f"Job is not queued (status: {log.status})"}
        
        # Parse request data
        request_data = json.loads(log.request_data) if log.request_data else {}
        
        # Import and call the method
        method_path = log.method
        module_path, method_name = method_path.rsplit(".", 1)
        
        import importlib
        module = importlib.import_module(module_path)
        method = getattr(module, method_name)
        
        # Set the request_id flag
        frappe.flags.request_id = log.name
        
        # Call the method
        result = method(
            payload=request_data,
            request_id=log.name,
            store_name=log.shopify_store
        )
        
        # Check status
        log.reload()
        
        return {
            "success": True,
            "message": f"Processed successfully. New status: {log.status}",
            "new_status": log.status
        }
        
    except Exception as e:
        frappe.log_error(title="Manual Job Processing Error")
        return {
            "success": False,
            "message": str(e)
        }
