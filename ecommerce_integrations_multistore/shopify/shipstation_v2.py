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

# US state name to 2-letter code mappings
US_STATE_CODES = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
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

def get_state_code(state_name):
    """Convert US state name to 2-letter code."""
    if not state_name:
        return ""
    
    # Check if already a 2-letter code
    if len(state_name) == 2:
        return state_name.upper()
    
    # Try to map state name
    return US_STATE_CODES.get(state_name, state_name)

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
        api_key: ShipStation V2 Production API Key
    
    Returns:
        dict: Response from ShipStation API
    """
    # Clean the API key - remove any whitespace that might have been added
    api_key = api_key.strip() if api_key else ""
    
    # ShipStation V2 API uses API-Key header authentication
    headers = {
        "API-Key": api_key,  # V2 uses API-Key header (not Authorization)
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # Debug: Log API key details and verify header format
    # TEMPORARY: Log full API key for debugging (REMOVE AFTER FIXING)
    expected_key = "wZ9hqcBMZAgdbSj2VaqTnTzk2BIidQfSc6JxWZoJD5I"
    key_matches = (api_key == expected_key) if api_key else False
    
    # Show actual key for debugging
    frappe.log_error(
        message=f"FULL API KEY (TEMPORARY DEBUG):\n'{api_key}'\n\nExpected:\n'{expected_key}'\n\nLength: {len(api_key) if api_key else 0}\nKey matches: {key_matches}\nHeader Keys: {list(headers.keys())}\nAPI-Key header exists: {'API-Key' in headers}",
        title="ShipStation V2 Debug - Auth"
    )
    
    # Get customer and shipping details
    customer = frappe.get_doc("Customer", delivery_note.customer)
    
    # Build ShipStation shipment for V2 API (following known-good schema)
    # carrier_id and service_code will be determined by ShipStation automation rules
    shipment = {
        "carrier_id": "se-1553310",  # UPS carrier ID - ShipStation will use automation rules
        "service_code": "ups_ground_saver",  # Default service - ShipStation will optimize
        "external_shipment_id": delivery_note.name,  # Our reference
        "create_sales_order": True,  # Create Order in ShipStation UI (not just Shipment API object)
        "ship_to": {
            "name": customer.customer_name,
            "phone": customer.mobile_no or "(000) 000-0000",  # Default if no phone
            "email": customer.email_id or "",
            "company_name": customer.customer_name,
            "address_line1": "",
            "address_line2": "",
            "city_locality": "",
            "state_province": "",
            "postal_code": "",
            "country_code": "US",
            "address_residential_indicator": "unknown"
        },
        "ship_from": {
            "name": "GLAMCOR GLOBAL LLC",
            "phone": "(212) 555-1212",  # GLAMCOR default phone
            "email": "",
            "company_name": "GLAMCOR GLOBAL LLC",
            "address_line1": "227 Route 33 E",
            "address_line2": "Bldg 2, Unit 7",
            "city_locality": "Manalapan",
            "state_province": "NJ",
            "postal_code": "07726",
            "country_code": "US",
            "address_residential_indicator": "no"
        },
        "items": []
    }
    
    # Add shipping address if available
    if delivery_note.shipping_address_name:
        shipping_address = frappe.get_doc("Address", delivery_note.shipping_address_name)
        
        # Get phone from address or customer
        ship_to_phone = shipping_address.phone or customer.mobile_no or customer.phone or "(000) 000-0000"
        
        shipment["ship_to"].update({
            "phone": ship_to_phone,
            "address_line1": shipping_address.address_line1 or "",
            "address_line2": shipping_address.address_line2 or "",
            "city_locality": shipping_address.city or "",
            "state_province": get_state_code(shipping_address.state),  # Convert to 2-letter code
            "postal_code": shipping_address.pincode or "",
            "country_code": get_country_code(shipping_address.country) if shipping_address.country else "US"
        })
    
    # Add line items (unit_price as plain number, not object)
    for item in delivery_note.items:
        shipment["items"].append({
            "name": item.item_name,
            "sku": item.item_code,
            "quantity": cint(item.qty),
            "unit_price": flt(item.rate)  # Plain number, not object
        })
    
    # Wrap shipment in shipments array as required by V2 API
    payload = {
        "shipments": [shipment]
    }
    
    try:
        # Create shipment in ShipStation V2
        url = f"{SHIPSTATION_BASE_URL}/v2/shipments"
        
        # Debug: Log the request with actual header keys (masking values)
        masked_headers = {k: '***' if k.lower() in ['api-key', 'authorization'] else v for k, v in headers.items()}
        frappe.log_error(
            message=f"Sending to URL: {url}\nActual Headers (masked): {masked_headers}\nHeader count: {len(headers)}",
            title="ShipStation V2 Debug - Request"
        )
        
        # Send the request with payload (shipments array)
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=30,
            auth=None  # Explicitly disable any default auth
        )
        
        # Debug: Log response details
        frappe.log_error(
            message=f"Response Status: {response.status_code}, Headers: {dict(response.headers)}",
            title="ShipStation V2 Debug - Response"
        )
        
        # If 401, try to get the error message before raising
        if response.status_code == 401:
            try:
                error_text = response.text
                frappe.log_error(
                    message=f"401 Response Body: {error_text}",
                    title="ShipStation V2 Debug - 401 Error"
                )
            except:
                pass
        
        response.raise_for_status()
        
        data = response.json()
        
        # Debug: Log full response to see structure
        frappe.log_error(
            message=f"ShipStation Response:\n{frappe.as_json(data, indent=2)}",
            title="ShipStation V2 Full Response"
        )
        
        # V2 API returns: {"shipments": [{...}]}
        # Extract shipment_id from first shipment in array
        shipment_id = None
        external_id = None
        shipment_info = {}
        
        if "shipments" in data and data["shipments"]:
            shipment_info = data["shipments"][0]
            shipment_id = shipment_info.get("shipment_id")
            external_id = shipment_info.get("external_shipment_id")
        
        # Persist shipment_id on Delivery Note
        if shipment_id:
            delivery_note.db_set("shipstation_shipment_id", shipment_id, update_modified=False)
            frappe.log_error(
                message=f"Successfully sent {delivery_note.name} to ShipStation.\nShipment ID: {shipment_id}\nExternal ID: {external_id}",
                title="ShipStation V2 Success"
            )
            # Add comment to delivery note
            delivery_note.add_comment(
                comment_type="Info",
                text=f"Sent to ShipStation V2. Shipment ID: {shipment_id}"
            )
        else:
            frappe.log_error(
                title="ShipStation V2 - Missing shipment_id",
                message=frappe.as_json(data, indent=2)
            )
        
        return {
            "success": True,
            "shipment_id": shipment_id,
            "external_shipment_id": external_id,
            "full_response": shipment_info
        }
        
    except requests.exceptions.HTTPError as e:
        # Try to get the actual error message from the response
        error_body = "No response body"
        status_code = "Unknown"
        url = "Unknown"
        
        if hasattr(e, 'response') and e.response is not None:
            status_code = e.response.status_code
            url = e.response.url
            try:
                error_body = e.response.text
                # If it's JSON, try to parse it for better error message
                if e.response.headers.get('Content-Type', '').startswith('application/json'):
                    try:
                        error_json = e.response.json()
                        error_body = f"JSON: {error_json}"
                    except:
                        pass
            except Exception as decode_error:
                error_body = f"Could not decode response: {str(decode_error)}"
        
        error_detail = f"HTTP {status_code}: {error_body}"
        frappe.log_error(
            message=f"Failed to send {delivery_note.name} to ShipStation: {error_detail}\nURL: {url}\nRequest: {payload}",
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
    # Must use get_password() for Password fields, not direct access
    api_key = setting.get_password("shipstation_api_key")
    
    if api_key:
        result = send_delivery_note_to_shipstation_v2(
            delivery_note, 
            api_key
        )
        return result
    else:
        frappe.log_error(
            message=f"ShipStation API Key not configured for store {setting.name}",
            title="ShipStation Configuration Missing"
        )
        return {"success": False, "error": "API Key not configured"}