// Copyright (c) 2025, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on('Delivery Note', {
	refresh: function(frm) {
		// Get ShipStation fields (may have custom_ prefix or not depending on how they were created)
		let shipstation_id = frm.doc.custom_shipstation_shipment_id || frm.doc.shipstation_shipment_id;
		let tracking_number = frm.doc.custom_shipstation_tracking_number || frm.doc.shipstation_tracking_number;
		let carrier = frm.doc.custom_shipstation_carrier || frm.doc.shipstation_carrier;
		
		// Show buttons for submitted Delivery Notes that are from Shopify OR have ShipStation ID
		let is_shopify_dn = frm.doc.shopify_order_id || frm.doc.custom_shopify_order_id;
		
		if (frm.doc.docstatus === 1 && (shipstation_id || is_shopify_dn)) {
			// If DN has no ShipStation ID, show "Resend to ShipStation" button
			if (!shipstation_id && is_shopify_dn && !tracking_number) {
				frm.add_custom_button(__('Resend to ShipStation'), function() {
					frappe.confirm(
						__('This will send the Delivery Note to ShipStation. Continue?'),
						function() {
							frappe.call({
								method: 'ecommerce_integrations_multistore.shopify.shipstation_v2.resend_to_shipstation',
								args: {
									delivery_note_name: frm.doc.name
								},
								freeze: true,
								freeze_message: __('Sending to ShipStation...'),
								callback: function(r) {
									if (r.message) {
										if (r.message.success) {
											frappe.show_alert({
												message: __('Sent to ShipStation. Shipment ID: {0}', [r.message.shipment_id || 'N/A']),
												indicator: 'green'
											}, 5);
											frm.reload_doc();
										} else {
											frappe.msgprint({
												title: __('Failed to Send'),
												indicator: 'red',
												message: r.message.error || 'Unknown error'
											});
										}
									}
								}
							});
						}
					);
				}, __('ShipStation'));
			}
			
			// Check if tracking is missing but has ShipStation ID
			if (!tracking_number && shipstation_id) {
				frm.add_custom_button(__('Sync Tracking from ShipStation'), function() {
					frappe.call({
						method: 'ecommerce_integrations_multistore.shopify.shipstation_webhook.manually_sync_shipstation_tracking',
						args: {
							delivery_note_name: frm.doc.name
						},
						freeze: true,
						freeze_message: __('Fetching tracking info from ShipStation...'),
						callback: function(r) {
							if (r.message) {
								if (r.message.success) {
									frappe.show_alert({
										message: __('Tracking synced: {0}', [r.message.tracking_number || 'N/A']),
										indicator: 'green'
									}, 3);
									frm.reload_doc();
								} else {
									frappe.msgprint({
										title: __('Sync Failed'),
										indicator: 'red',
										message: r.message.message
									});
								}
							}
						}
					});
				}, __('ShipStation'));
			}
			
			// Always show option to manually enter tracking
			frm.add_custom_button(__('Enter Tracking Manually'), function() {
				frappe.prompt([
					{
						fieldname: 'tracking_number',
						label: __('Tracking Number'),
						fieldtype: 'Data',
						reqd: 1,
						default: tracking_number || ''
					},
					{
						fieldname: 'carrier',
						label: __('Carrier'),
						fieldtype: 'Select',
						options: '\nUPS\nUSPS\nFedEx\nDHL\nDHL Express\nOther',
						default: carrier || ''
					}
				], function(values) {
					frappe.call({
						method: 'ecommerce_integrations_multistore.shopify.shipstation_webhook.manually_sync_shipstation_tracking',
						args: {
							delivery_note_name: frm.doc.name,
							tracking_number: values.tracking_number,
							carrier: values.carrier
						},
						freeze: true,
						freeze_message: __('Updating tracking info...'),
						callback: function(r) {
							if (r.message) {
								if (r.message.success) {
									frappe.msgprint({
										title: __('Tracking Updated'),
										indicator: 'green',
										message: r.message.message
									});
									frm.reload_doc();
								} else {
									frappe.msgprint({
										title: __('Update Failed'),
										indicator: 'red',
										message: r.message.message
									});
								}
							}
						}
					});
				}, __('Enter Tracking Information'), __('Update'));
			}, __('ShipStation'));
		}
		
		// Show tracking info if available
		let display_tracking = frm.doc.custom_shipstation_tracking_number || frm.doc.shipstation_tracking_number;
		if (display_tracking) {
			frm.dashboard.add_indicator(
				__('Tracking: {0}', [display_tracking]),
				'green'
			);
		}
	}
});

