"""ShipStation API Integration for Shopify Orders using API Key authentication."""

import frappe
import requests
import base64
from frappe.utils import cint, flt, nowdate

# ShipStation uses Basic Auth with API Key as username and API Secret as password
# For V2/newer auth, just the API Key is used as Bearer token
SHIPSTATION_BASE_URL = "https://ssapi.shipstation.com"

def send_delivery_note_to_shipstation_v2(delivery_note, api_key):
    """Send Delivery Note to ShipStation using V2 API.
    
    Args:
        delivery_note: ERPNext Delivery Note document
        api_key: ShipStation V2 Production API Key (Bearer token)
    
    Returns:
        dict: Response from ShipStation API
    """
    # ShipStation API uses Basic Auth with API Key:API Secret
    # Since we only have API Key, we'll use it as Bearer token or Basic auth
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # Get customer and shipping details
    customer = frappe.get_doc("Customer", delivery_note.customer)
    
    # Build ShipStation order payload
    order_data = {
        "orderNumber": delivery_note.name,
        "orderDate": delivery_note.posting_date.isoformat(),
        "orderStatus": "awaiting_shipment",
        "customerUsername": customer.name,
        "customerEmail": customer.email_id or "",
        "billTo": {
            "name": customer.customer_name,
            "company": customer.customer_name,
            "street1": "",
            "city": "",
            "state": "",
            "postalCode": "",
            "country": "",
            "phone": customer.mobile_no or ""
        },
        "shipTo": {
            "name": delivery_note.shipping_address_name or customer.customer_name,
            "company": "",
            "street1": "",
            "city": "",
            "state": "",
            "postalCode": "",
            "country": "",
            "phone": customer.mobile_no or ""
        },
        "items": []
    }
    
    # Add shipping address if available
    if delivery_note.shipping_address_name:
        shipping_address = frappe.get_doc("Address", delivery_note.shipping_address_name)
        order_data["shipTo"].update({
            "street1": shipping_address.address_line1 or "",
            "street2": shipping_address.address_line2 or "",
            "city": shipping_address.city or "",
            "state": shipping_address.state or "",
            "postalCode": shipping_address.pincode or "",
            "country": shipping_address.country or ""
        })
    
    # Add line items
    for item in delivery_note.items:
        order_data["items"].append({
            "lineItemKey": item.name,
            "sku": item.item_code,
            "name": item.item_name,
            "quantity": cint(item.qty),
            "unitPrice": flt(item.rate),
            "weight": {
                "value": flt(item.weight_per_unit) if hasattr(item, 'weight_per_unit') else 0,
                "units": "pounds"
            }
        })
    
    # Add order notes if available
    if hasattr(delivery_note, 'shopify_order_id'):
        order_data["internalNotes"] = f"Shopify Order ID: {delivery_note.shopify_order_id}"
    
    try:
        # Create order in ShipStation
        # Using the standard orders endpoint
        response = requests.post(
            f"{SHIPSTATION_BASE_URL}/orders",
            json=order_data,
            headers=headers,
            timeout=30
        )
        
        response.raise_for_status()
        
        result = response.json()
        
        # Log success
        frappe.log_error(
            message=f"Successfully sent {delivery_note.name} to ShipStation. Order ID: {result.get('orderId')}",
            title="ShipStation V2 Success"
        )
        
        # Add comment to delivery note
        delivery_note.add_comment(
            comment_type="Info",
            text=f"Sent to ShipStation V2. Order ID: {result.get('orderId')}"
        )
        
        return {
            "success": True,
            "order_id": result.get("orderId"),
            "order_key": result.get("orderKey")
        }
        
    except requests.exceptions.HTTPError as e:
        error_detail = f"HTTP {e.response.status_code}: {e.response.text if e.response else 'No response body'}"
        frappe.log_error(
            message=f"Failed to send {delivery_note.name} to ShipStation: {error_detail}\nURL: {e.response.url}\nRequest: {order_data}",
            title="ShipStation API Error"
        )
        return {
            "success": False,
            "error": error_detail
        }
    except requests.exceptions.RequestException as e:
        frappe.log_error(
            message=f"Failed to send {delivery_note.name} to ShipStation: {str(e)}",
            title="ShipStation Connection Error"
        )
        return {
            "success": False,
            "error": str(e)
        }


def update_shipstation_integration_for_v2(delivery_note, setting):
    """Updated function to use ShipStation V2 API.
    
    This replaces the old send_to_shipstation function for V2 compatibility.
    """
    # Check if ShipStation is enabled
    if not (hasattr(setting, 'shipstation_enabled') and setting.shipstation_enabled):
        frappe.log_error(
            message=f"ShipStation integration is disabled for store {setting.name}",
            title="ShipStation Integration Disabled"
        )
        return {"success": False, "error": "ShipStation integration disabled"}
    
    # Check if ShipStation V2 API key is configured
    if hasattr(setting, 'shipstation_api_key') and setting.shipstation_api_key:
        result = send_delivery_note_to_shipstation_v2(
            delivery_note, 
            setting.shipstation_api_key
        )
        return result
    else:
        frappe.log_error(
            message=f"ShipStation API Key not configured for store {setting.name}",
            title="ShipStation Configuration Missing"
        )
        return {"success": False, "error": "API Key not configured"}
