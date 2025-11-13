# Copyright (c) 2025, Frappe Technologies and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

class TestPowerSupplyMapping(FrappeTestCase):
    def test_get_power_supply_for_country(self):
        from .power_supply_mapping import PowerSupplyMapping
        
        # Create test mappings
        mappings = [
            {"country_code": "US", "country_name": "United States", "power_supply_type": "US"},
            {"country_code": "CA", "country_name": "Canada", "power_supply_type": "US"},
            {"country_code": "CN", "country_name": "China", "power_supply_type": "US"},
            {"country_code": "GB", "country_name": "United Kingdom", "power_supply_type": "UK"},
            {"country_code": "PK", "country_name": "Pakistan", "power_supply_type": "UK"},
            {"country_code": "DE", "country_name": "Germany", "power_supply_type": "EU"},
            {"country_code": "FR", "country_name": "France", "power_supply_type": "EU"},
            {"country_code": "AU", "country_name": "Australia", "power_supply_type": "AU"},
            {"country_code": "NZ", "country_name": "New Zealand", "power_supply_type": "AU"},
        ]
        
        for mapping in mappings:
            if not frappe.db.exists("Power Supply Mapping", mapping["country_code"]):
                doc = frappe.get_doc({
                    "doctype": "Power Supply Mapping",
                    **mapping
                })
                doc.insert()
        
        # Test mappings
        self.assertEqual(PowerSupplyMapping.get_power_supply_for_country("US"), "US")
        self.assertEqual(PowerSupplyMapping.get_power_supply_for_country("GB"), "UK")
        self.assertEqual(PowerSupplyMapping.get_power_supply_for_country("DE"), "EU")
        self.assertEqual(PowerSupplyMapping.get_power_supply_for_country("AU"), "AU")
        self.assertEqual(PowerSupplyMapping.get_power_supply_for_country("PK"), "UK")
        
        # Test unknown country
        self.assertIsNone(PowerSupplyMapping.get_power_supply_for_country("XX"))
        
        # Test case insensitive
        self.assertEqual(PowerSupplyMapping.get_power_supply_for_country("us"), "US")
