# Copyright (c) 2025, Frappe and contributors
# For license information, please see LICENSE

"""
Cleanup script to find Delivery Notes that were incorrectly sent to ShipStation
when they already had tracking info (already shipped).

Run this from bench console:
    bench --site [sitename] console
    from ecommerce_integrations_multistore.shopify.cleanup_duplicate_shipments import find_duplicate_shipments
    find_duplicate_shipments()
"""

import frappe
from frappe.utils import now_datetime, add_to_date


@frappe.whitelist()
def find_duplicate_shipments(hours_back=6, store_name=None):
    """
    Find Delivery Notes that were sent to ShipStation but already had tracking.
    
    These are duplicates that need to be cancelled in ShipStation.
    
    Args:
        hours_back: How many hours back to look (default 6)
        store_name: Optional - filter by store
    
    Returns:
        dict: Summary and list of duplicate DNs
    """
    cutoff_time = add_to_date(now_datetime(), hours=-hours_back)
    
    # Find DNs created recently that have BOTH:
    # 1. A ShipStation shipment ID (sent to ShipStation)
    # 2. Tracking info (already shipped)
    
    filters = {
        "creation": [">=", cutoff_time],
        "docstatus": ["!=", 2],  # Not cancelled
    }
    
    if store_name:
        filters["shopify_store"] = store_name
    
    # Get all recent DNs with ShipStation and tracking info
    dns = frappe.get_all(
        "Delivery Note",
        filters=filters,
        fields=[
            "name",
            "shopify_order_number",
            "shopify_order_id", 
            "creation",
            "custom_shipstation_shipment_id",
            "shipstation_shipment_id",
            "custom_shipstation_tracking_number",
            "shopify_store",
            "docstatus"
        ],
        order_by="creation desc"
    )
    
    duplicates = []
    
    for dn in dns:
        # Check if it has a ShipStation shipment ID
        shipstation_id = dn.get("custom_shipstation_shipment_id") or dn.get("shipstation_shipment_id")
        tracking = dn.get("custom_shipstation_tracking_number")
        
        if shipstation_id and tracking:
            # This DN was sent to ShipStation AND has tracking = likely duplicate
            duplicates.append({
                "delivery_note": dn.name,
                "shopify_order": dn.shopify_order_number,
                "shopify_order_id": dn.shopify_order_id,
                "shipstation_id": shipstation_id,
                "tracking_number": tracking,
                "store": dn.shopify_store,
                "created": str(dn.creation),
                "status": "Submitted" if dn.docstatus == 1 else "Draft"
            })
    
    result = {
        "total_dns_checked": len(dns),
        "duplicates_found": len(duplicates),
        "hours_back": hours_back,
        "cutoff_time": str(cutoff_time),
        "duplicates": duplicates
    }
    
    # Print summary
    print(f"\n{'='*60}")
    print(f"DUPLICATE SHIPMENT FINDER")
    print(f"{'='*60}")
    print(f"Checked DNs created since: {cutoff_time}")
    print(f"Total DNs checked: {len(dns)}")
    print(f"Duplicates found: {len(duplicates)}")
    print(f"{'='*60}\n")
    
    if duplicates:
        print("DUPLICATES TO CANCEL IN SHIPSTATION:")
        print("-" * 60)
        for d in duplicates:
            print(f"  DN: {d['delivery_note']}")
            print(f"    Shopify Order: {d['shopify_order']}")
            print(f"    ShipStation ID: {d['shipstation_id']}")
            print(f"    Tracking: {d['tracking_number']}")
            print(f"    Created: {d['created']}")
            print()
        
        # Also print just the ShipStation IDs for easy copying
        print("\nSHIPSTATION ORDER IDs TO CANCEL:")
        print("-" * 60)
        for d in duplicates:
            print(f"  {d['shipstation_id']}")
    else:
        print("No duplicates found!")
    
    return result


@frappe.whitelist()
def find_all_dns_with_shipstation_and_tracking(days_back=7):
    """
    Find ALL Delivery Notes that have both ShipStation ID and tracking.
    
    This is a broader search to catch any that might have been missed.
    """
    cutoff_time = add_to_date(now_datetime(), days=-days_back)
    
    # SQL query to find DNs with both shipstation ID and tracking
    dns = frappe.db.sql("""
        SELECT 
            name,
            shopify_order_number,
            shopify_order_id,
            creation,
            COALESCE(custom_shipstation_shipment_id, shipstation_shipment_id) as shipstation_id,
            custom_shipstation_tracking_number as tracking,
            shopify_store,
            docstatus
        FROM `tabDelivery Note`
        WHERE creation >= %s
        AND docstatus != 2
        AND (custom_shipstation_shipment_id IS NOT NULL OR shipstation_shipment_id IS NOT NULL)
        AND custom_shipstation_tracking_number IS NOT NULL
        AND custom_shipstation_tracking_number != ''
        ORDER BY creation DESC
    """, (cutoff_time,), as_dict=True)
    
    print(f"\n{'='*60}")
    print(f"ALL DNs WITH SHIPSTATION ID AND TRACKING (last {days_back} days)")
    print(f"{'='*60}")
    print(f"Found: {len(dns)}")
    print(f"{'='*60}\n")
    
    for dn in dns:
        print(f"  DN: {dn['name']}")
        print(f"    Order: {dn['shopify_order_number']}")
        print(f"    ShipStation: {dn['shipstation_id']}")
        print(f"    Tracking: {dn['tracking']}")
        print()
    
    return dns


if __name__ == "__main__":
    # For testing
    find_duplicate_shipments(hours_back=6)

