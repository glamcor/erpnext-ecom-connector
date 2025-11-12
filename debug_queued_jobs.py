#!/usr/bin/env python
"""Debug script to check why Shopify jobs are stuck in Queued status."""

import frappe

def check_queued_jobs():
    """Check status of queued Shopify integration jobs."""
    
    # Connect to site
    frappe.init(site="glamcor-private.v.frappe.cloud")  # Update with your site name
    frappe.connect()
    
    print("=== Checking Ecommerce Integration Logs ===")
    
    # Check recent logs
    logs = frappe.get_all(
        "Ecommerce Integration Log",
        filters={
            "status": "Queued",
            "creation": (">", frappe.utils.add_days(frappe.utils.now(), -1))
        },
        fields=["name", "method", "status", "creation", "modified", "shopify_store"],
        order_by="creation desc",
        limit=20
    )
    
    print(f"\nFound {len(logs)} queued jobs from the last 24 hours:")
    for log in logs:
        print(f"- {log.name}: {log.method} ({log.shopify_store}) - Created: {log.creation}")
    
    # Check if there are any successful recent jobs
    success_logs = frappe.get_all(
        "Ecommerce Integration Log",
        filters={
            "status": "Success",
            "creation": (">", frappe.utils.add_days(frappe.utils.now(), -1))
        },
        limit=5
    )
    
    print(f"\nSuccessful jobs in last 24 hours: {len(success_logs)}")
    
    # Check background jobs
    print("\n=== Checking Background Jobs ===")
    
    # Check RQ jobs
    from frappe.utils.background_jobs import get_jobs
    
    queues = ["default", "short", "long"]
    for queue in queues:
        jobs = get_jobs(queue=queue, key="queued")
        print(f"\n{queue.upper()} queue - Queued jobs: {len(jobs)}")
        
        # Show first few jobs
        for job in jobs[:3]:
            print(f"  - Job ID: {job.id}")
            print(f"    Function: {job.func_name}")
            print(f"    Created: {job.created_at}")
    
    # Check failed jobs
    print("\n=== Checking Failed Jobs ===")
    for queue in queues:
        failed = get_jobs(queue=queue, key="failed")
        if failed:
            print(f"\n{queue.upper()} queue - Failed jobs: {len(failed)}")
            for job in failed[:2]:
                print(f"  - Job ID: {job.id}")
                print(f"    Function: {job.func_name}")
                print(f"    Error: {job.exc_info}")
    
    # Check if workers are running
    print("\n=== Worker Status ===")
    try:
        from frappe.utils.scheduler import is_scheduler_inactive
        if is_scheduler_inactive():
            print("WARNING: Scheduler is INACTIVE!")
        else:
            print("Scheduler is active")
    except:
        print("Could not check scheduler status")
    
    frappe.destroy()

if __name__ == "__main__":
    check_queued_jobs()
