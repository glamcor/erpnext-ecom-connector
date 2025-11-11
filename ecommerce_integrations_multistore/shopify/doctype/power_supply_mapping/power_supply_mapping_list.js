frappe.listview_settings['Power Supply Mapping'] = {
	add_fields: ['is_active'],
	
	get_indicator: function(doc) {
		if (doc.is_active) {
			return [__('Active'), 'green', 'is_active,=,1'];
		} else {
			return [__('Inactive'), 'grey', 'is_active,=,0'];
		}
	},
	
	onload: function(listview) {
		listview.page.add_inner_button(__('Import Mappings'), function() {
			frappe.msgprint(__('Import functionality can be implemented here'));
		});
	}
};
