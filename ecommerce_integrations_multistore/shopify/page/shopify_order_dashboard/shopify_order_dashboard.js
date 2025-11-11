frappe.pages['shopify-order-dashboard'].on_page_load = function(wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Shopify Order Dashboard',
		single_column: true
	});

	page.set_title('Shopify Order Dashboard');
	page.set_indicator('Loading...', 'orange');

	// Add refresh button
	page.set_primary_action('Refresh', () => {
		page.dashboard.refresh();
	}, 'refresh');

	// Add check incomplete orders button
	page.set_secondary_action('Check Incomplete Orders', () => {
		frappe.call({
			method: 'ecommerce_integrations_multistore.shopify.bulk_operations.check_incomplete_orders_for_updates',
			args: {
				store_name: page.dashboard.store_filter
			},
			callback: (r) => {
				if (r.message) {
					frappe.show_alert({
						message: r.message.message,
						indicator: 'green'
					});
					page.dashboard.refresh();
				}
			}
		});
	});

	// Create dashboard
	page.dashboard = new ShopifyOrderDashboard(page);
}

class ShopifyOrderDashboard {
	constructor(page) {
		this.page = page;
		this.store_filter = null;
		this.make();
		this.refresh();
	}

	make() {
		// Add store filter
		this.make_filters();
		
		// Create main layout
		this.$wrapper = $('<div class="shopify-order-dashboard"></div>').appendTo(this.page.main);
		
		// Add summary cards
		this.$summary = $('<div class="row"></div>').appendTo(this.$wrapper);
		
		// Add lists section
		this.$lists = $('<div class="row mt-4"></div>').appendTo(this.$wrapper);
	}

	make_filters() {
		// Add store filter
		this.page.add_field({
			fieldname: 'store',
			label: __('Shopify Store'),
			fieldtype: 'Link',
			options: 'Shopify Store',
			change: () => {
				this.store_filter = this.page.fields_dict.store.value;
				this.refresh();
			}
		});
	}

	refresh() {
		this.page.set_indicator('Loading...', 'orange');
		
		frappe.call({
			method: 'ecommerce_integrations_multistore.shopify.bulk_operations.get_shopify_order_summary',
			args: {
				store_name: this.store_filter
			},
			callback: (r) => {
				if (r.message) {
					this.render_summary(r.message);
					this.render_lists();
					this.page.set_indicator('Updated', 'green');
				}
			}
		});
	}

	render_summary(data) {
		this.$summary.empty();
		
		const cards = [
			{
				title: 'Incomplete Orders',
				value: data.incomplete_orders,
				color: 'orange',
				description: 'Awaiting customer info'
			},
			{
				title: 'Draft Invoices',
				value: data.draft_invoices,
				color: 'blue',
				description: 'Ready for review'
			},
			{
				title: 'Submitted Today',
				value: data.submitted_today,
				color: 'green',
				description: 'Invoices submitted'
			},
			{
				title: 'Pending Delivery',
				value: data.pending_delivery,
				color: 'red',
				description: 'Awaiting delivery note'
			}
		];

		cards.forEach(card => {
			const $card = $(`
				<div class="col-md-3">
					<div class="card">
						<div class="card-body">
							<h5 class="card-title text-${card.color}">${card.title}</h5>
							<h2 class="mt-3 mb-3">${card.value}</h2>
							<p class="text-muted small">${card.description}</p>
						</div>
					</div>
				</div>
			`);
			this.$summary.append($card);
		});
	}

	render_lists() {
		this.$lists.empty();
		
		// Draft invoices list
		const $draft_list = $(`
			<div class="col-md-6">
				<div class="card">
					<div class="card-header">
						<h5>Draft Invoices
							<button class="btn btn-sm btn-primary float-right" id="bulk-submit-btn">
								Bulk Submit Selected
							</button>
						</h5>
					</div>
					<div class="card-body">
						<div id="draft-invoices-list"></div>
					</div>
				</div>
			</div>
		`);
		this.$lists.append($draft_list);
		
		// Incomplete orders list
		const $incomplete_list = $(`
			<div class="col-md-6">
				<div class="card">
					<div class="card-header">
						<h5>Incomplete Orders</h5>
					</div>
					<div class="card-body">
						<div id="incomplete-orders-list"></div>
					</div>
				</div>
			</div>
		`);
		this.$lists.append($incomplete_list);
		
		// Render draft invoices
		this.render_draft_invoices();
		
		// Render incomplete orders
		this.render_incomplete_orders();
		
		// Bind bulk submit button
		$('#bulk-submit-btn').on('click', () => {
			this.bulk_submit_invoices();
		});
	}

	render_draft_invoices() {
		const filters = {
			docstatus: 0,
			shopify_order_id: ['is', 'set']
		};
		
		if (this.store_filter) {
			filters.shopify_store = this.store_filter;
		}
		
		frappe.call({
			method: 'frappe.client.get_list',
			args: {
				doctype: 'Sales Invoice',
				fields: ['name', 'customer', 'grand_total', 'posting_date', 'shopify_order_number'],
				filters: filters,
				limit: 20,
				order_by: 'creation desc'
			},
			callback: (r) => {
				if (r.message && r.message.length > 0) {
					const $list = $('<div class="list-group"></div>');
					
					r.message.forEach(inv => {
						const $item = $(`
							<div class="list-group-item">
								<input type="checkbox" class="mr-2" data-invoice="${inv.name}">
								<a href="/app/sales-invoice/${inv.name}" class="font-weight-bold">
									${inv.name}
								</a>
								<span class="ml-2">${inv.shopify_order_number || ''}</span>
								<div class="small text-muted">
									${inv.customer} - ${format_currency(inv.grand_total)}
								</div>
							</div>
						`);
						$list.append($item);
					});
					
					$('#draft-invoices-list').html($list);
				} else {
					$('#draft-invoices-list').html('<p class="text-muted">No draft invoices found</p>');
				}
			}
		});
	}

	render_incomplete_orders() {
		const filters = {
			status: 'Incomplete Order',
			method: ['like', '%sync_sales_order%']
		};
		
		if (this.store_filter) {
			filters.shopify_store = this.store_filter;
		}
		
		frappe.call({
			method: 'frappe.client.get_list',
			args: {
				doctype: 'Ecommerce Integration Log',
				fields: ['name', 'creation', 'shopify_store'],
				filters: filters,
				limit: 20,
				order_by: 'creation desc'
			},
			callback: (r) => {
				if (r.message && r.message.length > 0) {
					const $list = $('<div class="list-group"></div>');
					
					r.message.forEach(log => {
						const $item = $(`
							<div class="list-group-item">
								<a href="/app/ecommerce-integration-log/${log.name}" class="font-weight-bold">
									${log.name}
								</a>
								<div class="small text-muted">
									${frappe.datetime.str_to_user(log.creation)}
									${log.shopify_store ? ' - ' + log.shopify_store : ''}
								</div>
							</div>
						`);
						$list.append($item);
					});
					
					$('#incomplete-orders-list').html($list);
				} else {
					$('#incomplete-orders-list').html('<p class="text-muted">No incomplete orders found</p>');
				}
			}
		});
	}

	bulk_submit_invoices() {
		// Get selected invoices
		const selected = [];
		$('#draft-invoices-list input[type="checkbox"]:checked').each(function() {
			selected.push($(this).data('invoice'));
		});
		
		if (selected.length === 0) {
			frappe.msgprint(__('Please select invoices to submit'));
			return;
		}
		
		frappe.confirm(
			__('Are you sure you want to submit {0} invoice(s)?', [selected.length]),
			() => {
				frappe.call({
					method: 'ecommerce_integrations_multistore.shopify.bulk_operations.bulk_submit_invoices',
					args: {
						names: selected
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
							
							this.refresh();
						}
					}
				});
			}
		);
	}
}
