"""Analyze queued orders to find patterns"""
import frappe
import json
from collections import defaultdict

@frappe.whitelist()
def analyze_queued_orders():
    """Analyze patterns in queued orders"""
    frappe.set_user('Administrator')
    
    print("Analyzing queued orders...")
    
    # Get all queued orders
    queued_orders = frappe.db.sql("""
        SELECT name, request_data, modified
        FROM `tabEcommerce Integration Log`
        WHERE status = 'Queued'
        AND method LIKE '%shopify%'
        ORDER BY modified DESC
    """, as_dict=1)
    
    print(f"\nTotal queued orders: {len(queued_orders)}")
    
    # Analyze patterns
    patterns = defaultdict(int)
    email_domains = defaultdict(int)
    stores = defaultdict(int)
    sample_orders = []
    
    for log in queued_orders[:100]:  # Analyze first 100
        try:
            if isinstance(log.request_data, str):
                order = json.loads(log.request_data)
            else:
                order = log.request_data
                
            # Check patterns
            customer = order.get('customer', {})
            email = customer.get('email', '')
            
            # Email patterns
            if '@' in email:
                domain = email.split('@')[1]
                email_domains[domain] += 1
            
            # Store patterns
            source_name = order.get('source_name', 'Unknown')
            stores[source_name] += 1
            
            # Check for common issues
            if not customer:
                patterns['no_customer'] += 1
            elif not order.get('shipping_address'):
                patterns['no_shipping_address'] += 1
            elif '@tiktokw.us' in email:
                patterns['tiktok_order'] += 1
            elif not order.get('line_items'):
                patterns['no_line_items'] += 1
            else:
                patterns['other'] += 1
                
            # Collect sample
            if len(sample_orders) < 5:
                sample_orders.append({
                    'log_id': log.name,
                    'order_name': order.get('name'),
                    'email': email,
                    'source': source_name,
                    'has_shipping': bool(order.get('shipping_address')),
                    'line_items': len(order.get('line_items', []))
                })
                
        except Exception as e:
            patterns['parse_error'] += 1
            print(f"Error parsing {log.name}: {e}")
    
    # Print analysis
    print("\n" + "="*60)
    print("QUEUED ORDERS ANALYSIS")
    print("="*60)
    
    print("\nPatterns found:")
    for pattern, count in sorted(patterns.items(), key=lambda x: x[1], reverse=True):
        print(f"  {pattern}: {count}")
    
    print("\nEmail domains:")
    for domain, count in sorted(email_domains.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {domain}: {count}")
    
    print("\nOrder sources:")
    for source, count in sorted(stores.items(), key=lambda x: x[1], reverse=True):
        print(f"  {source}: {count}")
    
    print("\nSample orders:")
    for sample in sample_orders:
        print(f"\n  Log: {sample['log_id']}")
        print(f"  Order: {sample['order_name']}")
        print(f"  Email: {sample['email']}")
        print(f"  Source: {sample['source']}")
        print(f"  Has shipping: {sample['has_shipping']}")
        print(f"  Line items: {sample['line_items']}")
    
    return {
        "total": len(queued_orders),
        "patterns": dict(patterns),
        "email_domains": dict(list(email_domains.items())[:10]),
        "sources": dict(stores),
        "samples": sample_orders
    }
