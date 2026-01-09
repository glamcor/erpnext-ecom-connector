// Copyright (c) 2025, Frappe and contributors
// For license information, please see license.txt

frappe.listview_settings['Delivery Note'] = frappe.listview_settings['Delivery Note'] || {};

// Extend existing settings
let original_onload = frappe.listview_settings['Delivery Note'].onload;

frappe.listview_settings['Delivery Note'].onload = function(listview) {
	// Call original onload if exists
	if (original_onload) {
		original_onload(listview);
	}
	
	// Add bulk action for ShipStation sync
	listview.page.add_action_item(__('Sync Tracking from ShipStation'), function() {
		let selected = listview.get_checked_items();
		
		if (selected.length === 0) {
			frappe.msgprint(__('Please select at least one Delivery Note'));
			return;
		}
		
		let dn_names = selected.map(d => d.name);
		
		frappe.call({
			method: 'ecommerce_integrations_multistore.shopify.shipstation_webhook.bulk_sync_shipstation_tracking',
			args: {
				delivery_notes: JSON.stringify(dn_names)
			},
			freeze: true,
			freeze_message: __('Syncing tracking for {0} Delivery Notes...', [dn_names.length]),
			callback: function(r) {
				if (r.message) {
					frappe.show_alert({
						message: __('Synced: {0}, Failed: {1}', [r.message.synced, r.message.failed]),
						indicator: r.message.failed > 0 ? 'orange' : 'green'
					}, 5);
					listview.refresh();
				}
			}
		});
	});
};

