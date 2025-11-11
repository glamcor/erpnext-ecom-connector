# Copyright (c) 2025, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class PowerSupplyMapping(Document):
	def validate(self):
		self.validate_items()
		self.check_duplicate_mapping()
	
	def validate_items(self):
		"""Ensure bundle item and power supply item are different."""
		if self.bundle_item == self.power_supply_item:
			frappe.throw("Bundle Item and Power Supply Item cannot be the same")
	
	def check_duplicate_mapping(self):
		"""Check for duplicate active mappings."""
		if self.is_active:
			existing = frappe.db.exists(
				"Power Supply Mapping",
				{
					"bundle_item": self.bundle_item,
					"country": self.country,
					"is_active": 1,
					"name": ["!=", self.name]
				}
			)
			if existing:
				frappe.throw(
					f"An active mapping already exists for {self.bundle_item} in {self.country}"
				)


@frappe.whitelist()
def get_power_supply_for_item(bundle_item, country):
	"""Get the power supply item for a bundle item and country.
	
	Args:
	    bundle_item: Item code of the bundle
	    country: Country name
	
	Returns:
	    Power supply item code or None
	"""
	mapping = frappe.db.get_value(
		"Power Supply Mapping",
		{
			"bundle_item": bundle_item,
			"country": country,
			"is_active": 1
		},
		"power_supply_item"
	)
	return mapping
