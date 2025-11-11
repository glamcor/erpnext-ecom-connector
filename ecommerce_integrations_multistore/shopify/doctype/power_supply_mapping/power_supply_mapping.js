// Copyright (c) 2025, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on('Power Supply Mapping', {
	refresh: function(frm) {
		// Add custom buttons if needed
	},
	
	bundle_item: function(frm) {
		// Validate that bundle item is not the same as power supply item
		if (frm.doc.bundle_item && frm.doc.bundle_item === frm.doc.power_supply_item) {
			frm.set_value('power_supply_item', '');
			frappe.msgprint(__('Power Supply Item cannot be the same as Bundle Item'));
		}
	},
	
	power_supply_item: function(frm) {
		// Validate that power supply item is not the same as bundle item
		if (frm.doc.power_supply_item && frm.doc.power_supply_item === frm.doc.bundle_item) {
			frm.set_value('power_supply_item', '');
			frappe.msgprint(__('Power Supply Item cannot be the same as Bundle Item'));
		}
	}
});
