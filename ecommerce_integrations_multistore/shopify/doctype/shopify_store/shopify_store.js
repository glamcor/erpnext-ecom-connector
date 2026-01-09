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

			frm.add_custom_button(__('Retry Failed Syncs'), function() {
				frappe.prompt([
					{
						fieldname: 'days_back',
						label: __('Days to look back'),
						fieldtype: 'Int',
						default: 7,
						reqd: 1
					},
					{
						fieldname: 'max_retries',
						label: __('Max orders to retry'),
						fieldtype: 'Int',
						default: 50,
						reqd: 1
					}
				], function(values) {
					frappe.call({
						method: 'ecommerce_integrations_multistore.shopify.order.retry_failed_order_syncs',
						args: {
							days_back: values.days_back,
							store_name: frm.doc.name,
							max_retries: values.max_retries
						},
						callback: function(r) {
							if (!r.exc && r.message) {
								let msg = r.message.message;
								if (r.message.succeeded > 0) {
									frappe.msgprint({
										title: __('Retry Complete'),
										indicator: 'green',
										message: msg
									});
								} else if (r.message.retried === 0) {
									frappe.msgprint({
										title: __('No Failed Syncs'),
										indicator: 'blue',
										message: msg
									});
								} else {
									frappe.msgprint({
										title: __('Retry Complete'),
										indicator: 'orange',
										message: msg
									});
								}
							}
						}
					});
				}, __('Retry Failed Order Syncs'), __('Retry'));
			}, __('Actions'));

			// Reconcile Orders button
			frm.add_custom_button(__('Reconcile Orders'), function() {
				frappe.prompt([
					{
						fieldname: 'date_from',
						label: __('Date From'),
						fieldtype: 'Date',
						default: frappe.datetime.add_days(frappe.datetime.nowdate(), -30),
						reqd: 1
					},
					{
						fieldname: 'date_to',
						label: __('Date To'),
						fieldtype: 'Date',
						default: frappe.datetime.nowdate(),
						reqd: 1
					},
					{
						fieldtype: 'Column Break'
					},
					{
						fieldname: 'order_from',
						label: __('Order Number From (optional)'),
						fieldtype: 'Data',
						description: __('e.g., RLR150000')
					},
					{
						fieldname: 'order_to',
						label: __('Order Number To (optional)'),
						fieldtype: 'Data',
						description: __('e.g., RLR151000')
					},
					{
						fieldtype: 'Section Break',
						label: __('What to Check')
					},
					{
						fieldname: 'check_invoices',
						label: __('Missing Invoices'),
						fieldtype: 'Check',
						default: 1
					},
					{
						fieldname: 'check_payments',
						label: __('Missing Payments'),
						fieldtype: 'Check',
						default: 1
					},
					{
						fieldtype: 'Column Break'
					},
					{
						fieldname: 'check_delivery_notes',
						label: __('Missing Delivery Notes'),
						fieldtype: 'Check',
						default: 1
					},
					{
						fieldname: 'check_tracking',
						label: __('Missing Tracking'),
						fieldtype: 'Check',
						default: 1
					}
				], function(values) {
					frappe.call({
						method: 'ecommerce_integrations_multistore.shopify.reconciliation.reconcile_shopify_orders',
						args: {
							store_name: frm.doc.name,
							date_from: values.date_from,
							date_to: values.date_to,
							order_from: values.order_from,
							order_to: values.order_to,
							check_invoices: values.check_invoices,
							check_payments: values.check_payments,
							check_delivery_notes: values.check_delivery_notes,
							check_tracking: values.check_tracking
						},
						freeze: true,
						freeze_message: __('Fetching orders from Shopify and comparing...'),
						callback: function(r) {
							if (!r.exc && r.message) {
								show_reconciliation_results(frm, r.message);
							}
						}
					});
				}, __('Reconcile Shopify Orders'), __('Run'));
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

// Reconciliation results dialog
function show_reconciliation_results(frm, data) {
	let summary = data.summary;
	let total_issues = summary.missing_invoices_count + summary.missing_payments_count + 
		summary.missing_delivery_notes_count + summary.missing_tracking_count;
	
	if (total_issues === 0) {
		frappe.msgprint({
			title: __('Reconciliation Complete'),
			indicator: 'green',
			message: __('All {0} orders are in sync! No issues found.', [summary.total_orders_checked])
		});
		return;
	}
	
	// Build HTML for results
	let html = `
		<div class="reconciliation-results">
			<div class="alert alert-info">
				<strong>${__('Orders Checked')}:</strong> ${summary.total_orders_checked}<br>
				<strong>${__('Date Range')}:</strong> ${data.date_from} to ${data.date_to}
			</div>
			
			<h5>${__('Issues Found')}</h5>
			<table class="table table-bordered">
				<thead>
					<tr>
						<th>${__('Issue Type')}</th>
						<th>${__('Count')}</th>
						<th>${__('Action')}</th>
					</tr>
				</thead>
				<tbody>
	`;
	
	if (summary.missing_invoices_count > 0) {
		html += `
			<tr>
				<td><strong>${__('Missing Invoices')}</strong><br>
					<small class="text-muted">${__('Paid orders without Sales Invoice')}</small></td>
				<td class="text-center"><span class="badge badge-danger">${summary.missing_invoices_count}</span></td>
				<td><button class="btn btn-xs btn-primary fix-invoices-btn">${__('Create All')}</button>
					<button class="btn btn-xs btn-default view-invoices-btn">${__('View')}</button></td>
			</tr>
		`;
	}
	
	// NEW: Draft invoices that need full processing
	if (summary.draft_invoices_count > 0) {
		html += `
			<tr>
				<td><strong>${__('Draft Invoices')}</strong><br>
					<small class="text-muted">${__('Paid/shipped orders with unsubmitted invoices - will submit, create payment & DN')}</small></td>
				<td class="text-center"><span class="badge badge-danger">${summary.draft_invoices_count}</span></td>
				<td><button class="btn btn-xs btn-primary fix-drafts-btn">${__('Process All')}</button>
					<button class="btn btn-xs btn-default view-drafts-btn">${__('View')}</button></td>
			</tr>
		`;
	}
	
	if (summary.missing_payments_count > 0) {
		html += `
			<tr>
				<td><strong>${__('Missing Payments')}</strong><br>
					<small class="text-muted">${__('Submitted invoices with outstanding balance')}</small></td>
				<td class="text-center"><span class="badge badge-warning">${summary.missing_payments_count}</span></td>
				<td><button class="btn btn-xs btn-primary fix-payments-btn">${__('Create All')}</button>
					<button class="btn btn-xs btn-default view-payments-btn">${__('View')}</button></td>
			</tr>
		`;
	}
	
	if (summary.missing_delivery_notes_count > 0) {
		html += `
			<tr>
				<td><strong>${__('Missing Delivery Notes')}</strong><br>
					<small class="text-muted">${__('Fulfilled orders without Delivery Note')}</small></td>
				<td class="text-center"><span class="badge badge-info">${summary.missing_delivery_notes_count}</span></td>
				<td><button class="btn btn-xs btn-primary fix-dns-btn">${__('Create All')}</button>
					<button class="btn btn-xs btn-default view-dns-btn">${__('View')}</button></td>
			</tr>
		`;
	}
	
	if (summary.missing_tracking_count > 0) {
		html += `
			<tr>
				<td><strong>${__('Missing Tracking')}</strong><br>
					<small class="text-muted">${__('Delivery Notes without tracking info')}</small></td>
				<td class="text-center"><span class="badge badge-secondary">${summary.missing_tracking_count}</span></td>
				<td><button class="btn btn-xs btn-primary fix-tracking-btn">${__('Sync All')}</button>
					<button class="btn btn-xs btn-default view-tracking-btn">${__('View')}</button></td>
			</tr>
		`;
	}
	
	html += `
				</tbody>
			</table>
		</div>
	`;
	
	let d = new frappe.ui.Dialog({
		title: __('Reconciliation Results'),
		size: 'large',
		fields: [
			{
				fieldtype: 'HTML',
				fieldname: 'results_html'
			}
		]
	});
	
	d.fields_dict.results_html.$wrapper.html(html);
	
	// Store data for fix buttons
	d.reconciliation_data = data;
	d.store_name = frm.doc.name;
	
	// Bind button events
	d.$wrapper.find('.fix-invoices-btn').on('click', function() {
		fix_missing_items(d, 'invoices', data.missing_invoices);
	});
	
	d.$wrapper.find('.fix-drafts-btn').on('click', function() {
		fix_missing_items(d, 'draft_invoices', data.draft_invoices);
	});
	
	d.$wrapper.find('.fix-payments-btn').on('click', function() {
		fix_missing_items(d, 'payments', data.missing_payments);
	});
	
	d.$wrapper.find('.fix-dns-btn').on('click', function() {
		fix_missing_items(d, 'delivery_notes', data.missing_delivery_notes);
	});
	
	d.$wrapper.find('.fix-tracking-btn').on('click', function() {
		fix_missing_items(d, 'tracking', data.missing_tracking);
	});
	
	// View buttons
	d.$wrapper.find('.view-invoices-btn').on('click', function() {
		show_details_dialog('Missing Invoices', data.missing_invoices);
	});
	
	d.$wrapper.find('.view-drafts-btn').on('click', function() {
		show_details_dialog('Draft Invoices', data.draft_invoices);
	});
	
	d.$wrapper.find('.view-payments-btn').on('click', function() {
		show_details_dialog('Missing Payments', data.missing_payments);
	});
	
	d.$wrapper.find('.view-dns-btn').on('click', function() {
		show_details_dialog('Missing Delivery Notes', data.missing_delivery_notes);
	});
	
	d.$wrapper.find('.view-tracking-btn').on('click', function() {
		show_details_dialog('Missing Tracking', data.missing_tracking);
	});
	
	d.show();
}

function fix_missing_items(dialog, fix_type, items) {
	if (!items || items.length === 0) {
		frappe.msgprint(__('No items to fix'));
		return;
	}
	
	let order_ids = items.map(item => item.order_id);
	let method_map = {
		'invoices': 'ecommerce_integrations_multistore.shopify.reconciliation.fix_missing_invoices',
		'draft_invoices': 'ecommerce_integrations_multistore.shopify.reconciliation.fix_draft_invoices',
		'payments': 'ecommerce_integrations_multistore.shopify.reconciliation.fix_missing_payments',
		'delivery_notes': 'ecommerce_integrations_multistore.shopify.reconciliation.fix_missing_delivery_notes',
		'tracking': 'ecommerce_integrations_multistore.shopify.reconciliation.fix_missing_tracking'
	};
	
	let type_labels = {
		'invoices': __('Invoices'),
		'draft_invoices': __('Draft Invoices'),
		'payments': __('Payments'),
		'delivery_notes': __('Delivery Notes'),
		'tracking': __('Tracking')
	};
	
	frappe.confirm(
		__('This will process {0} orders. Continue?', [order_ids.length]),
		function() {
			frappe.call({
				method: method_map[fix_type],
				args: {
					store_name: dialog.store_name,
					order_ids: JSON.stringify(order_ids)
				},
				freeze: true,
				freeze_message: __('Processing {0}...', [type_labels[fix_type]]),
				callback: function(r) {
					if (!r.exc && r.message) {
						let result = r.message;
						let indicator = result.failed === 0 ? 'green' : (result.success > 0 ? 'orange' : 'red');
						
						// Build message with failure details
						let msg = __('Success: {0}, Failed: {1}, Skipped: {2}', 
							[result.success, result.failed, result.skipped || 0]);
						
						// Add failure details if any
						if (result.failed > 0 && result.details) {
							msg += '<br><br><strong>' + __('Failure Details') + ':</strong><br>';
							msg += '<table class="table table-sm table-bordered" style="margin-top:10px">';
							msg += '<thead><tr><th>Order</th><th>Reason</th></tr></thead><tbody>';
							result.details.forEach(function(d) {
								if (d.status === 'failed') {
									msg += '<tr><td>' + (d.order_number || d.order_id) + '</td>';
									msg += '<td>' + (d.reason || 'Unknown error') + '</td></tr>';
								}
							});
							msg += '</tbody></table>';
						}
						
						frappe.msgprint({
							title: __('Fix Complete'),
							indicator: indicator,
							message: msg
						});
						
						// Close the dialog if all successful
						if (result.failed === 0) {
							dialog.hide();
						}
					}
				}
			});
		}
	);
}

function show_details_dialog(title, items) {
	if (!items || items.length === 0) {
		frappe.msgprint(__('No items to display'));
		return;
	}
	
	let html = '<table class="table table-bordered table-sm"><thead><tr>';
	html += '<th>' + __('Order #') + '</th>';
	html += '<th>' + __('Date') + '</th>';
	html += '<th>' + __('Customer') + '</th>';
	html += '<th>' + __('Status') + '</th>';
	html += '<th>' + __('Total') + '</th>';
	html += '</tr></thead><tbody>';
	
	items.forEach(function(item) {
		html += '<tr>';
		html += '<td>' + (item.order_number || '') + '</td>';
		html += '<td>' + (item.order_date || '') + '</td>';
		html += '<td>' + (item.customer || '') + '</td>';
		html += '<td>' + (item.financial_status || '') + ' / ' + (item.fulfillment_status || 'unfulfilled') + '</td>';
		html += '<td>$' + (item.total || '0') + '</td>';
		html += '</tr>';
	});
	
	html += '</tbody></table>';
	
	let d = new frappe.ui.Dialog({
		title: __(title) + ' (' + items.length + ')',
		size: 'extra-large',
		fields: [
			{
				fieldtype: 'HTML',
				fieldname: 'details_html'
			}
		]
	});
	
	d.fields_dict.details_html.$wrapper.html(html);
	d.show();
}