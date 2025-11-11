frappe.listview_settings['Sales Invoice'] = frappe.listview_settings['Sales Invoice'] || {};

// Extend existing settings
const existing_onload = frappe.listview_settings['Sales Invoice'].onload;

frappe.listview_settings['Sales Invoice'].onload = function(listview) {
	// Call existing onload if it exists
	if (existing_onload) {
		existing_onload(listview);
	}
	
	// Add bulk submit button for Shopify invoices
	if (frappe.user.has_role(['System Manager', 'Sales Manager'])) {
		listview.page.add_action_item(__('Submit Shopify Invoices'), function() {
			const selected = listview.get_checked_items();
			
			if (selected.length === 0) {
				frappe.msgprint(__('Please select invoices to submit'));
				return;
			}
			
			// Filter for draft Shopify invoices only
			const names = selected
				.filter(item => item.docstatus === 0)
				.map(item => item.name);
			
			if (names.length === 0) {
				frappe.msgprint(__('No draft invoices selected'));
				return;
			}
			
			frappe.confirm(
				__('Submit {0} draft invoice(s)?', [names.length]),
				() => {
					frappe.call({
						method: 'ecommerce_integrations_multistore.shopify.bulk_operations.bulk_submit_invoices',
						args: {
							names: names
						},
						callback: (r) => {
							if (r.message) {
								frappe.show_alert({
									message: r.message.message,
									indicator: r.message.errors.length > 0 ? 'orange' : 'green'
								});
								
								if (r.message.errors.length > 0) {
									// Show errors
									let error_msg = '<h5>Errors:</h5><ul>';
									r.message.errors.forEach(err => {
										error_msg += `<li>${err.invoice}: ${err.error}</li>`;
									});
									error_msg += '</ul>';
									frappe.msgprint(error_msg);
								}
								
								listview.refresh();
							}
						}
					});
				}
			);
		});
	}
};
