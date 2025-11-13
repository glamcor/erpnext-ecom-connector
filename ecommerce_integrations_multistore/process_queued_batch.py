"""Process a batch of queued orders"""
import frappe
import json

@frappe.whitelist()
def process_queued_batch(limit=10):
    """Process a batch of queued orders"""
    frappe.set_user('Administrator')
    
    # Get queued orders
    queued_orders = frappe.db.sql("""
        SELECT name, request_data
        FROM `tabEcommerce Integration Log`
        WHERE status = 'Queued'
        AND method LIKE '%shopify%'
        ORDER BY modified ASC
        LIMIT %s
    """, limit, as_dict=1)
    
    print(f"\nProcessing {len(queued_orders)} queued orders...")
    
    results = {
        'processed': 0,
        'success': 0,
        'incomplete': 0,
        'error': 0,
        'errors': []
    }
    
    for log in queued_orders:
        try:
            # Parse order data
            if isinstance(log.request_data, str):
                order = json.loads(log.request_data)
            else:
                order = log.request_data
            
            # Import and run sync function
            from ecommerce_integrations_multistore.shopify.order import sync_sales_order
            
            # Set request ID for log updates
            frappe.flags.request_id = log.name
            
            # Process order
            sync_sales_order(order)
            
            # Check result
            updated_log = frappe.get_doc("Ecommerce Integration Log", log.name)
            results['processed'] += 1
            
            if updated_log.status == "Success":
                results['success'] += 1
                print(f"✓ {log.name}: Success - Order {order.get('name')}")
            elif updated_log.status == "Incomplete Order":
                results['incomplete'] += 1
                print(f"⏸ {log.name}: Incomplete - {order.get('name')}")
            else:
                results['error'] += 1
                print(f"✗ {log.name}: {updated_log.status} - {updated_log.message[:50]}...")
                
            # Commit after each successful order
            frappe.db.commit()
            
        except Exception as e:
            results['error'] += 1
            error_msg = str(e)[:200]
            results['errors'].append(f"{log.name}: {error_msg}")
            print(f"✗ {log.name}: ERROR - {error_msg}")
            frappe.db.rollback()
    
    # Print summary
    print("\n" + "="*60)
    print("PROCESSING SUMMARY")
    print("="*60)
    print(f"Processed: {results['processed']}")
    print(f"Success: {results['success']}")
    print(f"Incomplete: {results['incomplete']}")
    print(f"Error: {results['error']}")
    
    if results['errors']:
        print("\nDetailed Errors:")
        for err in results['errors'][:10]:
            print(f"  {err}")
    
    return results
