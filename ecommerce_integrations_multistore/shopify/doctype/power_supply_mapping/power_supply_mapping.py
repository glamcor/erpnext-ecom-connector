# Copyright (c) 2025, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

class PowerSupplyMapping(Document):
    def validate(self):
        # Ensure power supply type is uppercase
        if self.power_supply_type:
            self.power_supply_type = self.power_supply_type.upper()
    
    @staticmethod
    def get_power_supply_for_country(country_code):
        """Get the power supply type for a given country code.
        
        Args:
            country_code: Two-letter country code (e.g., 'US', 'GB', 'DE')
            
        Returns:
            Power supply type (e.g., 'US', 'UK', 'EU') or None if not found
        """
        if not country_code:
            return None
            
        # Check for exact match first
        mapping = frappe.db.get_value(
            "Power Supply Mapping",
            {"country_code": country_code.upper()},
            "power_supply_type"
        )
        
        if mapping:
            return mapping
            
        # If no mapping found, return None (could default to EU or US based on business rules)
        return None