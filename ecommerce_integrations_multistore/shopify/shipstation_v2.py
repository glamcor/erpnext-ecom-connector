"""ShipStation V2 API Integration for Shopify Orders."""

import frappe
import requests
from frappe.utils import cint, flt, nowdate, getdate

# ShipStation V2 API uses API Key authentication
SHIPSTATION_BASE_URL = "https://api.shipstation.com"

# Common country name to ISO code mappings
COUNTRY_CODE_MAP = {
    "United States": "US",
    "United States of America": "US",
    "USA": "US",
    "Canada": "CA",
    "United Kingdom": "GB",
    "UK": "GB",
    "Australia": "AU",
    "Germany": "DE",
    "France": "FR",
    "Spain": "ES",
    "Italy": "IT",
    "Mexico": "MX",
    "Japan": "JP",
    "China": "CN",
    "India": "IN",
    "Brazil": "BR"
}

def get_country_code(country_name):
    """Convert country name to 2-letter ISO code."""
    if not country_name:
        return "US"
    
    # Check if already a 2-letter code
    if len(country_name) == 2:
        return country_name.upper()
    
    # Try to map common country names
    return COUNTRY_CODE_MAP.get(country_name, "US")

def is_us_domestic_order(delivery_note):
    """Check if the order is a US domestic shipment.
    
    Args:
        delivery_note: ERPNext Delivery Note document
    
    Returns:
        bool: True if US domestic order, False otherwise
    """
    # Check shipping address
    if delivery_note.shipping_address_name:
        shipping_address = frappe.get_doc("Address", delivery_note.shipping_address_name)
        country = shipping_address.country
        
        # Check if it's a US address
        if country and country.upper() in ["US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"]:
            return True
        else:
            frappe.log_error(
                message=f"Non-US shipping address for {delivery_note.name}: {country}",
                title="International Order Detected"
            )
            return False
    
    # No shipping address, assume not US domestic
    return False

def send_delivery_note_to_shipstation_v2(delivery_note, api_key):
    """Send Delivery Note to ShipStation using V2 API.
    
    Args:
        delivery_note: ERPNext Delivery Note document
        api_key: ShipStation V2 Production API Key (Bearer token)
    
    Returns:
        dict: Response from ShipStation API
    """
    # ShipStation V2 API uses API Key authentication
    headers = {
        "x-api-key": api_key,  # V2 uses x-api-key header
        "Content-Type": "application/json"
    }
    
    # Get customer and shipping details
    customer = frappe.get_doc("Customer", delivery_note.customer)
    
    # Build ShipStation shipment payload for V2 API
    shipment_data = {
        "shipment_id": None,  # Let ShipStation generate this
        "carrier_id": None,  # Will be set by user in ShipStation
        "service_code": None,  # Will be set by user in ShipStation
        "external_shipment_id": delivery_note.name,  # Our reference
        "ship_date": getdate(nowdate()).isoformat(),
        "ship_to": {
            "name": customer.customer_name,
            "phone": customer.mobile_no or "",
            "email": customer.email_id or "",
            "company_name": customer.customer_name,
            "address_line1": "",
            "address_line2": "",
            "city_locality": "",
            "state_province": "",
            "postal_code": "",
            "country_code": "US",  # Default to US
            "address_residential_indicator": "unknown"
        },
        "ship_from": {
            "name": frappe.defaults.get_global_default("company") or "Your Company",
            "phone": "",
            "company_name": frappe.defaults.get_global_default("company") or "Your Company",
            "address_line1": "",
            "city_locality": "",
            "state_province": "",
            "postal_code": "",
            "country_code": "US"
        },
        "packages": [{
            "weight": {
                "value": 1.0,  # Default weight
                "unit": "pound"
            }
        }],
        "items": []
    }
    
    # Add shipping address if available
    if delivery_note.shipping_address_name:
        shipping_address = frappe.get_doc("Address", delivery_note.shipping_address_name)
        shipment_data["ship_to"].update({
            "address_line1": shipping_address.address_line1 or "",
            "address_line2": shipping_address.address_line2 or "",
            "city_locality": shipping_address.city or "",
            "state_province": shipping_address.state or "",
            "postal_code": shipping_address.pincode or "",
            "country_code": get_country_code(shipping_address.country) if shipping_address.country else "US"
        })
    
    # Add billing address if available
    if delivery_note.customer_address:
        billing_address = frappe.get_doc("Address", delivery_note.customer_address)
        shipment_data["ship_from"].update({
            "address_line1": billing_address.address_line1 or "",
            "city_locality": billing_address.city or "",
            "state_province": billing_address.state or "",
            "postal_code": billing_address.pincode or ""
        })
    
    # Add line items
    total_weight = 0
    for item in delivery_note.items:
        shipment_data["items"].append({
            "name": item.item_name,
            "sku": item.item_code,
            "quantity": cint(item.qty),
            "unit_price": {
                "currency": "USD",
                "amount": flt(item.rate)
            }
        })
        # Calculate total weight (assuming 1 lb per item if not specified)
        item_weight = flt(item.weight_per_unit) if hasattr(item, 'weight_per_unit') else 1.0
        total_weight += item_weight * cint(item.qty)
    
    # Update package weight with actual total
    if total_weight > 0:
        shipment_data["packages"][0]["weight"]["value"] = total_weight
    
    # Add internal notes
    if hasattr(delivery_note, 'shopify_order_id'):
        shipment_data["internal_notes"] = f"Shopify Order ID: {delivery_note.shopify_order_id}"
    
    try:
        # Create shipment in ShipStation V2
        response = requests.post(
            f"{SHIPSTATION_BASE_URL}/v2/shipments",
            json=shipment_data,
            headers=headers,
            timeout=30
        )
        
        response.raise_for_status()
        
        result = response.json()
        
        # Log success
        frappe.log_error(
            message=f"Successfully sent {delivery_note.name} to ShipStation. Shipment ID: {result.get('shipment_id')}",
            title="ShipStation V2 Success"
        )
        
        # Add comment to delivery note
        delivery_note.add_comment(
            comment_type="Info",
            text=f"Sent to ShipStation V2. Shipment ID: {result.get('shipment_id')}"
        )
        
        return {
            "success": True,
            "shipment_id": result.get("shipment_id"),
            "external_shipment_id": result.get("external_shipment_id")
        }
        
    except requests.exceptions.HTTPError as e:
        error_detail = f"HTTP {e.response.status_code}: {e.response.text if e.response else 'No response body'}"
        frappe.log_error(
            message=f"Failed to send {delivery_note.name} to ShipStation: {error_detail}\nURL: {e.response.url}\nRequest: {shipment_data}",
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
    
    # Check if this is a US domestic order
    if not is_us_domestic_order(delivery_note):
        frappe.log_error(
            message=f"Skipping ShipStation for non-US order {delivery_note.name}",
            title="ShipStation - International Order"
        )
        return {"success": False, "error": "ShipStation only handles US domestic orders"}
    
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
