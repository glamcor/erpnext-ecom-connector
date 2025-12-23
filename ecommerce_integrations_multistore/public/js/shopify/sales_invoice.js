// Sales Invoice form customizations for Shopify integration

frappe.ui.form.on('Sales Invoice', {
	refresh(frm) {
		// Only show for Shopify invoices
		if (!frm.doc.shopify_order_id) return;
		
		// DRAFT STATUS - Re-sync items button
		if (frm.doc.docstatus === 0) {
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
		
		// SUBMITTED STATUS - Repair buttons for missing DN/Payment
		if (frm.doc.docstatus === 1) {
			const is_paid = (frm.doc.shopify_order_status || '').toLowerCase() === 'paid';
			const has_outstanding = frm.doc.outstanding_amount > 0;
			
			// Check if DN exists for this invoice
			frappe.call({
				method: 'frappe.client.get_count',
				args: {
					doctype: 'Delivery Note',
					filters: {
						shopify_order_id: frm.doc.shopify_order_id,
						docstatus: ['!=', 2]
					}
				},
				async: false,
				callback: function(r) {
					const has_dn = r.message > 0;
					
					// Show "Create Delivery Note" button if missing
					if (!has_dn) {
						frm.add_custom_button(__('Create Delivery Note'), function() {
							frappe.confirm(
								__('Create Delivery Note and send to ShipStation?'),
								function() {
									frappe.call({
										method: 'ecommerce_integrations_multistore.shopify.invoice.create_missing_delivery_note',
										args: {
											invoice_name: frm.doc.name
										},
										freeze: true,
										freeze_message: __('Creating Delivery Note...'),
										callback: function(r) {
											if (r.message && r.message.status === 'success') {
												frappe.show_alert({
													message: r.message.message,
													indicator: 'green'
												});
												frm.reload_doc();
											} else {
												frappe.msgprint({
													title: __('Error'),
													message: r.message ? r.message.message : 'Unknown error',
													indicator: 'red'
												});
											}
										}
									});
								}
							);
						}, __('Shopify'));
					}
				}
			});
			
			// Show "Create Payment Entry" button if paid in Shopify but outstanding in ERPNext
			if (is_paid && has_outstanding) {
				frm.add_custom_button(__('Create Payment Entry'), function() {
					frappe.confirm(
						__('Create Payment Entry for this paid Shopify order?'),
						function() {
							frappe.call({
								method: 'ecommerce_integrations_multistore.shopify.invoice.create_missing_payment_entry',
								args: {
									invoice_name: frm.doc.name
								},
								freeze: true,
								freeze_message: __('Creating Payment Entry...'),
								callback: function(r) {
									if (r.message && r.message.status === 'success') {
										frappe.show_alert({
											message: r.message.message,
											indicator: 'green'
										});
										frm.reload_doc();
									} else {
										frappe.msgprint({
											title: __('Error'),
											message: r.message ? r.message.message : 'Unknown error',
											indicator: 'red'
										});
									}
								}
							});
						}
					);
				}, __('Shopify'));
			}
		}
	}
});

