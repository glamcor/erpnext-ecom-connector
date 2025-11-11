// Copyright (c) 2025, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on('Shopify Store', {
	refresh: function(frm) {
		// Add custom buttons
		if (!frm.is_new()) {
			frm.add_custom_button(__('Sync Old Orders'), function() {
				frappe.call({
					method: 'ecommerce_integrations_multistore.shopify.order.sync_old_orders_for_store',
					args: {
						store_name: frm.doc.name
					},
					callback: function(r) {
						if (!r.exc) {
							frappe.msgprint(__('Order sync has been queued'));
						}
					}
				});
			}, __('Actions'));

			frm.add_custom_button(__('Update Inventory'), function() {
				frappe.call({
					method: 'ecommerce_integrations_multistore.shopify.inventory.update_inventory_for_store',
					args: {
						store_name: frm.doc.name
					},
					callback: function(r) {
						if (!r.exc) {
							frappe.msgprint(__('Inventory update has been queued'));
						}
					}
				});
			}, __('Actions'));
		}
		
		// Populate naming series dropdowns
		frm.set_query('sales_order_series', function() {
			return {
				filters: {
					'document_type': 'Sales Order'
				}
			};
		});
		
		frm.set_query('delivery_note_series', function() {
			return {
				filters: {
					'document_type': 'Delivery Note'
				}
			};
		});
		
		frm.set_query('sales_invoice_series', function() {
			return {
				filters: {
					'document_type': 'Sales Invoice'
				}
			};
		});
		
		// Dynamically populate the series dropdowns
		if (frm.doc.sales_order_series === undefined || frm.doc.sales_order_series === null) {
			frappe.call({
				method: 'ecommerce_integrations_multistore.utils.naming_series.get_series',
				args: {
					doctype: 'Sales Order'
				},
				callback: function(r) {
					if (r.message && r.message.length > 0) {
						frm.set_df_property('sales_order_series', 'options', r.message.join('\n'));
					}
				}
			});
		}
		
		if (frm.doc.delivery_note_series === undefined || frm.doc.delivery_note_series === null) {
			frappe.call({
				method: 'ecommerce_integrations_multistore.utils.naming_series.get_series',
				args: {
					doctype: 'Delivery Note'
				},
				callback: function(r) {
					if (r.message && r.message.length > 0) {
						frm.set_df_property('delivery_note_series', 'options', r.message.join('\n'));
					}
				}
			});
		}
		
		if (frm.doc.sales_invoice_series === undefined || frm.doc.sales_invoice_series === null) {
			frappe.call({
				method: 'ecommerce_integrations_multistore.utils.naming_series.get_series',
				args: {
					doctype: 'Sales Invoice'
				},
				callback: function(r) {
					if (r.message && r.message.length > 0) {
						frm.set_df_property('sales_invoice_series', 'options', r.message.join('\n'));
					}
				}
			});
		}
	},
	
	onload: function(frm) {
		frappe.call({
			method: "ecommerce_integrations_multistore.shopify.doctype.shopify_store.shopify_store.get_series",
			callback: function(r) {
				if (!r.message) return;
				
				// Set options for naming series fields
				if (r.message.sales_order_series) {
					frm.set_df_property('sales_order_series', 'options', r.message.sales_order_series.join('\n'));
				}
				if (r.message.delivery_note_series) {
					frm.set_df_property('delivery_note_series', 'options', r.message.delivery_note_series.join('\n'));
				}
				if (r.message.sales_invoice_series) {
					frm.set_df_property('sales_invoice_series', 'options', r.message.sales_invoice_series.join('\n'));
				}
			}
		});
	}
});