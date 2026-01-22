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
	
	// Add bulk action for Resend to ShipStation
	listview.page.add_action_item(__('Resend to ShipStation'), function() {
		let selected = listview.get_checked_items();
		
		if (selected.length === 0) {
			frappe.msgprint(__('Please select at least one Delivery Note'));
			return;
		}
		
		// Filter to only submitted DNs without ShipStation ID
		let eligible = selected.filter(d => d.docstatus === 1);
		
		if (eligible.length === 0) {
			frappe.msgprint(__('No submitted Delivery Notes selected. Only submitted DNs can be sent to ShipStation.'));
			return;
		}
		
		let dn_names = eligible.map(d => d.name);
		
		frappe.confirm(
			__('This will send {0} Delivery Note(s) to ShipStation. DNs that already have a ShipStation ID will be skipped. Continue?', [dn_names.length]),
			function() {
				frappe.call({
					method: 'ecommerce_integrations_multistore.shopify.shipstation_v2.bulk_resend_to_shipstation',
					args: {
						delivery_notes: JSON.stringify(dn_names)
					},
					freeze: true,
					freeze_message: __('Sending {0} Delivery Notes to ShipStation...', [dn_names.length]),
					callback: function(r) {
						if (r.message) {
							let msg = __('Sent: {0}, Skipped: {1}, Failed: {2}', [
								r.message.sent || 0,
								r.message.skipped || 0,
								r.message.failed || 0
							]);
							
							frappe.show_alert({
								message: msg,
								indicator: r.message.failed > 0 ? 'orange' : 'green'
							}, 5);
							
							if (r.message.errors && r.message.errors.length > 0) {
								frappe.msgprint({
									title: __('Some DNs Failed'),
									indicator: 'orange',
									message: r.message.errors.join('<br>')
								});
							}
							
							listview.refresh();
						}
					}
				});
			}
		);
	});
};

