// Copyright (c) 2025, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Shopify Store", {
	onload: function (frm) {
		frm.set_query("customer_group", function () {
			return {
				filters: {
					is_group: 0,
				},
			};
		});

		// Naming series
		frappe.db.get_single_value("Selling Settings", "selling_price_list").then((price_list) => {
			frm.add_fetch("company", "cost_center", "cost_center");
			frm.add_fetch("company", "default_cash_account", "cash_bank_account");

			if (!frm.doc.price_list) {
				frm.set_value("price_list", price_list);
			}
		});
	},

	refresh: function (frm) {
		if (frm.doc.enabled) {
			frm.trigger("add_sync_buttons");
		}
	},

	add_sync_buttons: function (frm) {
		// Inventory sync
		if (frm.doc.update_erpnext_stock_levels_to_shopify) {
			frm.add_custom_button(
				__("Sync Stock to Shopify Now"),
				function () {
					frappe.call({
						method: "ecommerce_integrations_multistore.shopify.inventory.update_inventory_for_store",
						args: {
							store_name: frm.doc.name,
						},
						freeze: true,
						freeze_message: __("Syncing Stock..."),
						callback: function () {
							frappe.msgprint(__("Stock sync has been queued"));
						},
					});
				},
				__("Actions")
			);
		}

		// Order sync
		if (frm.doc.sync_old_orders) {
			frm.add_custom_button(
				__("Sync Old Orders Now"),
				function () {
					frappe.call({
						method: "ecommerce_integrations_multistore.shopify.order.sync_old_orders_for_store",
						args: {
							store_name: frm.doc.name,
						},
						freeze: true,
						freeze_message: __("Syncing Orders..."),
						callback: function () {
							frappe.msgprint(__("Order sync has been queued"));
						},
					});
				},
				__("Actions")
			);
		}
	},

	enabled: function (frm) {
		if (frm.doc.enabled) {
			frappe.msgprint({
				title: __("Note"),
				message: __(
					"Save the document to register webhooks with Shopify and setup custom fields in ERPNext"
				),
				indicator: "blue",
			});
		}
	},

	fetch_shopify_locations: function (frm) {
		frm.call("update_location_table").then(() => {
			frm.refresh_field("shopify_warehouse_mapping");
			frm.save();
		});
	},
});

