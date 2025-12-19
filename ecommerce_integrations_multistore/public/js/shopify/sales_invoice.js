// Sales Invoice form customizations for Shopify integration

frappe.ui.form.on('Sales Invoice', {
	refresh(frm) {
		// Only show for Shopify invoices in draft status
		if (frm.doc.shopify_order_id && frm.doc.docstatus === 0) {
			// Check if invoice has no items (hollow invoice)
			const has_items = frm.doc.items && frm.doc.items.length > 0 && 
				frm.doc.items.some(item => item.item_code);
			
			if (!has_items) {
				// Show warning banner
				frm.dashboard.add_comment(
					__('This invoice has no items. Items may not have existed in ERPNext when the order was synced.'),
					'yellow',
					true
				);
			}
			
			// Add Re-sync Items button
			frm.add_custom_button(__('Re-sync Items from Shopify'), function() {
				frappe.confirm(
					__('This will fetch the latest order data from Shopify and attempt to map items. Continue?'),
					function() {
						frappe.call({
							method: 'ecommerce_integrations_multistore.shopify.order.resync_invoice_items',
							args: {
								invoice_name: frm.doc.name
							},
							freeze: true,
							freeze_message: __('Fetching items from Shopify...'),
							callback: function(r) {
								if (r.message) {
									if (r.message.status === 'success') {
										frappe.show_alert({
											message: r.message.message,
											indicator: 'green'
										});
										frm.reload_doc();
									} else if (r.message.status === 'warning') {
										frappe.msgprint({
											title: __('Items Still Missing'),
											message: r.message.message,
											indicator: 'orange'
										});
									} else {
										frappe.msgprint({
											title: __('Error'),
											message: r.message.message,
											indicator: 'red'
										});
									}
								}
							}
						});
					}
				);
			}, __('Shopify'));
		}
	}
});

