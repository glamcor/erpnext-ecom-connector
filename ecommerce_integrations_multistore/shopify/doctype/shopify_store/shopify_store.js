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
				show_reconciliation_dialog(frm);
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

// Unified Reconciliation Dialog - search and results in one place
function show_reconciliation_dialog(frm) {
	let d = new frappe.ui.Dialog({
		title: __('Reconcile Shopify Orders'),
		size: 'extra-large',
		fields: [
			// Search Section
			{
				fieldtype: 'Section Break',
				label: __('Search Criteria')
			},
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
				label: __('Order Number From'),
				fieldtype: 'Data',
				description: __('e.g., RLR150000 (optional)')
			},
			{
				fieldname: 'order_to',
				label: __('Order Number To'),
				fieldtype: 'Data',
				description: __('e.g., RLR151000 (optional)')
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
			},
			{
				fieldtype: 'Section Break',
				label: __('Results'),
				fieldname: 'results_section',
				hidden: 1
			},
			{
				fieldtype: 'HTML',
				fieldname: 'results_html'
			}
		],
		primary_action_label: __('Search'),
		primary_action: function() {
			run_reconciliation_search(d, frm);
		}
	});
	
	// Store reference
	d.store_name = frm.doc.name;
	d.reconciliation_data = null;
	
	d.show();
}

function run_reconciliation_search(dialog, frm) {
	let values = dialog.get_values();
	
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
				display_reconciliation_results(dialog, r.message);
			}
		}
	});
}

function display_reconciliation_results(dialog, data) {
	let summary = data.summary;
	let total_issues = (summary.missing_invoices_count || 0) + 
		(summary.draft_invoices_count || 0) +
		(summary.missing_payments_count || 0) + 
		(summary.missing_delivery_notes_count || 0) + 
		(summary.missing_tracking_count || 0);
	
	// Show results section
	dialog.set_df_property('results_section', 'hidden', 0);
	
	// Store data for fix buttons
	dialog.reconciliation_data = data;
	
	let html = '';
	
	if (total_issues === 0) {
		html = `
			<div class="alert alert-success">
				<strong>${__('All Clear!')}</strong> ${__('All {0} orders are in sync. No issues found.', [summary.total_orders_checked])}
			</div>
		`;
	} else {
		html = `
			<div class="alert alert-info" style="margin-bottom: 15px;">
				<strong>${__('Orders Checked')}:</strong> ${summary.total_orders_checked}
				${data.order_from ? ' | <strong>' + __('Order Range') + ':</strong> ' + data.order_from + ' - ' + (data.order_to || data.order_from) : ''}
			</div>
			
			<table class="table table-bordered">
				<thead>
					<tr>
						<th>${__('Issue Type')}</th>
						<th style="width: 80px; text-align: center;">${__('Count')}</th>
						<th style="width: 180px;">${__('Action')}</th>
					</tr>
				</thead>
				<tbody>
		`;
		
		if (summary.missing_invoices_count > 0) {
			html += `
				<tr data-type="invoices">
					<td><strong>${__('Missing Invoices')}</strong><br>
						<small class="text-muted">${__('Paid orders without Sales Invoice')}</small></td>
					<td class="text-center"><span class="badge badge-danger">${summary.missing_invoices_count}</span></td>
					<td><button class="btn btn-xs btn-primary fix-btn" data-type="invoices">${__('Create All')}</button>
						<button class="btn btn-xs btn-default view-btn" data-type="invoices">${__('View')}</button></td>
				</tr>
			`;
		}
		
		if (summary.draft_invoices_count > 0) {
			html += `
				<tr data-type="draft_invoices">
					<td><strong>${__('Draft Invoices')}</strong><br>
						<small class="text-muted">${__('Paid/shipped orders stuck as drafts')}</small></td>
					<td class="text-center"><span class="badge badge-danger">${summary.draft_invoices_count}</span></td>
					<td><button class="btn btn-xs btn-primary fix-btn" data-type="draft_invoices">${__('Process All')}</button>
						<button class="btn btn-xs btn-default view-btn" data-type="draft_invoices">${__('View')}</button></td>
				</tr>
			`;
		}
		
		if (summary.missing_payments_count > 0) {
			html += `
				<tr data-type="payments">
					<td><strong>${__('Missing Payments')}</strong><br>
						<small class="text-muted">${__('Submitted invoices with outstanding balance')}</small></td>
					<td class="text-center"><span class="badge badge-warning">${summary.missing_payments_count}</span></td>
					<td><button class="btn btn-xs btn-primary fix-btn" data-type="payments">${__('Create All')}</button>
						<button class="btn btn-xs btn-default view-btn" data-type="payments">${__('View')}</button></td>
				</tr>
			`;
		}
		
		if (summary.missing_delivery_notes_count > 0) {
			html += `
				<tr data-type="delivery_notes">
					<td><strong>${__('Missing Delivery Notes')}</strong><br>
						<small class="text-muted">${__('Fulfilled orders without Delivery Note')}</small></td>
					<td class="text-center"><span class="badge badge-info">${summary.missing_delivery_notes_count}</span></td>
					<td><button class="btn btn-xs btn-primary fix-btn" data-type="delivery_notes">${__('Create All')}</button>
						<button class="btn btn-xs btn-default view-btn" data-type="delivery_notes">${__('View')}</button></td>
				</tr>
			`;
		}
		
		if (summary.missing_tracking_count > 0) {
			html += `
				<tr data-type="tracking">
					<td><strong>${__('Missing Tracking')}</strong><br>
						<small class="text-muted">${__('Delivery Notes without tracking info')}</small></td>
					<td class="text-center"><span class="badge badge-secondary">${summary.missing_tracking_count}</span></td>
					<td><button class="btn btn-xs btn-primary fix-btn" data-type="tracking">${__('Sync All')}</button>
						<button class="btn btn-xs btn-default view-btn" data-type="tracking">${__('View')}</button></td>
				</tr>
			`;
		}
		
		html += `
				</tbody>
			</table>
		`;
	}
	
	dialog.fields_dict.results_html.$wrapper.html(html);
	
	// Bind fix buttons
	dialog.$wrapper.find('.fix-btn').off('click').on('click', function() {
		let fix_type = $(this).data('type');
		fix_reconciliation_items(dialog, fix_type);
	});
	
	// Bind view buttons
	dialog.$wrapper.find('.view-btn').off('click').on('click', function() {
		let fix_type = $(this).data('type');
		let type_labels = {
			'invoices': 'Missing Invoices',
			'draft_invoices': 'Draft Invoices',
			'payments': 'Missing Payments',
			'delivery_notes': 'Missing Delivery Notes',
			'tracking': 'Missing Tracking'
		};
		let data_keys = {
			'invoices': 'missing_invoices',
			'draft_invoices': 'draft_invoices',
			'payments': 'missing_payments',
			'delivery_notes': 'missing_delivery_notes',
			'tracking': 'missing_tracking'
		};
		show_details_dialog(type_labels[fix_type], dialog.reconciliation_data[data_keys[fix_type]]);
	});
}

function fix_reconciliation_items(dialog, fix_type) {
	let data_keys = {
		'invoices': 'missing_invoices',
		'draft_invoices': 'draft_invoices',
		'payments': 'missing_payments',
		'delivery_notes': 'missing_delivery_notes',
		'tracking': 'missing_tracking'
	};
	
	let items = dialog.reconciliation_data[data_keys[fix_type]];
	
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
						
						// Show toast notification
						frappe.show_alert({
							message: __('Success: {0}, Failed: {1}, Skipped: {2}', 
								[result.success, result.failed, result.skipped || 0]),
							indicator: indicator
						}, 5);
						
						// Update the row in the results table
						let $row = dialog.$wrapper.find('tr[data-type="' + fix_type + '"]');
						let new_count = items.length - result.success;
						
						if (new_count <= 0) {
							$row.fadeOut(300, function() { $(this).remove(); });
						} else {
							$row.find('.badge').text(new_count);
						}
						
						// Update the stored data
						dialog.reconciliation_data[data_keys[fix_type]] = items.slice(result.success);
						if (dialog.reconciliation_data.summary[data_keys[fix_type] + '_count'] !== undefined) {
							dialog.reconciliation_data.summary[data_keys[fix_type] + '_count'] = new_count;
						}
						
						// Show detailed results if there were failures or for draft invoices
						if (result.failed > 0 || (fix_type === 'draft_invoices' && result.success > 0)) {
							let msg = '';
							
							// Show what was created for draft invoices
							if (result.success > 0 && result.details && fix_type === 'draft_invoices') {
								msg += '<strong>' + __('Processed') + ':</strong><br>';
								msg += '<table class="table table-sm table-bordered" style="margin-top:10px">';
								msg += '<thead><tr><th>Order</th><th>Created</th></tr></thead><tbody>';
								result.details.forEach(function(d) {
									if (d.status === 'success') {
										msg += '<tr><td>' + (d.order_number || d.order_id) + '</td>';
										msg += '<td>' + (d.created || '') + '</td></tr>';
									}
								});
								msg += '</tbody></table>';
							}
							
							// Show failures
							if (result.failed > 0 && result.details) {
								msg += '<br><strong>' + __('Failures') + ':</strong><br>';
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
							
							if (msg) {
								frappe.msgprint({
									title: __('Processing Details'),
									indicator: indicator,
									message: msg
								});
							}
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