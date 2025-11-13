# Copyright (c) 2025, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

class ProductPowerSupplyConfig(Document):
    def validate(self):
        # Validate that all power supply items exist
        for supply_type in ['us_power_supply', 'uk_power_supply', 'eu_power_supply', 'au_power_supply']:
            supply_item = self.get(supply_type)
            if supply_item and not frappe.db.exists("Item", supply_item):
                frappe.throw(f"{supply_type.replace('_', ' ').title()}: Item {supply_item} does not exist")
    
    @staticmethod
    def get_power_supply_item(product_code, power_supply_type):
        """Get the power supply item code for a product and supply type.
        
        Args:
            product_code: The product item code
            power_supply_type: The power supply type (US, UK, EU, AU)
            
        Returns:
            Power supply item code or None if not configured
        """
        config = frappe.db.get_value(
            "Product Power Supply Config",
            {"product": product_code},
            ["us_power_supply", "uk_power_supply", "eu_power_supply", "au_power_supply"],
            as_dict=True
        )
        
        if not config:
            return None
            
        # Map power supply type to field name
        field_map = {
            "US": "us_power_supply",
            "UK": "uk_power_supply", 
            "EU": "eu_power_supply",
            "AU": "au_power_supply"
        }
        
        field_name = field_map.get(power_supply_type.upper())
        if field_name:
            return config.get(field_name)
            
        return None
    
    @staticmethod
    def product_needs_power_supply(product_code):
        """Check if a product needs a power supply.
        
        Args:
            product_code: The product item code
            
        Returns:
            True if product has power supply configuration, False otherwise
        """
        return frappe.db.exists("Product Power Supply Config", {"product": product_code})
