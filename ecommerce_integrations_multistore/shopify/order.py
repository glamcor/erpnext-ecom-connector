import json
from typing import Literal, Optional

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate
from shopify.collection import PaginatedIterator
from shopify.resources import Order

from ecommerce_integrations_multistore.shopify.connection import temp_shopify_session
from ecommerce_integrations_multistore.shopify.constants import (
	CUSTOMER_ID_FIELD,
	EVENT_MAPPER,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
	STORE_DOCTYPE,
	STORE_LINK_FIELD,
)
from ecommerce_integrations_multistore.shopify.customer import ShopifyCustomer
from ecommerce_integrations_multistore.shopify.product import create_items_if_not_exist, get_item_code
from ecommerce_integrations_multistore.shopify.utils import create_shopify_log
from ecommerce_integrations_multistore.utils.price_list import get_dummy_price_list
from ecommerce_integrations_multistore.utils.taxation import get_dummy_tax_category

DEFAULT_TAX_FIELDS = {
	"sales_tax": "default_sales_tax_account",
	"shipping": "default_shipping_charges_account",
}


def sync_sales_order(payload, request_id=None, store_name=None):
	"""Sync sales order from Shopify webhook to ERPNext.
	
	Args:
	    payload: Shopify order data
	    request_id: Integration log ID
	    store_name: Shopify Store name (multi-store support)
	"""
	order = payload
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	if frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: cstr(order["id"])}):
		create_shopify_log(
			status="Invalid", 
			message="Sales order already exists, not synced",
			store_name=store_name
		)
		return
	try:
		# Get store-specific settings
		if store_name:
			store = frappe.get_doc(STORE_DOCTYPE, store_name)
		else:
			# Backward compatibility: fall back to singleton
			store = frappe.get_doc(SETTING_DOCTYPE)
		
		# Sync customer with store context
		shopify_customer = order.get("customer") if order.get("customer") is not None else {}
		shopify_customer["billing_address"] = order.get("billing_address", "")
		shopify_customer["shipping_address"] = order.get("shipping_address", "")
		customer_id = shopify_customer.get("id")
		if customer_id:
			customer = ShopifyCustomer(customer_id=customer_id, store_name=store_name)
			if not customer.is_synced():
				customer.sync_customer(customer=shopify_customer)
			else:
				customer.update_existing_addresses(shopify_customer)

		# Sync items with store context
		create_items_if_not_exist(order, store_name=store_name)

		create_order(order, store)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True, store_name=store_name)
	else:
		create_shopify_log(status="Success", store_name=store_name)


def create_order(order, setting, company=None):
	# local import to avoid circular dependencies
	from ecommerce_integrations_multistore.shopify.fulfillment import create_delivery_note
	from ecommerce_integrations_multistore.shopify.invoice import create_sales_invoice

	so = create_sales_order(order, setting, company)
	if so:
		if order.get("financial_status") == "paid":
			create_sales_invoice(order, setting, so)

		if order.get("fulfillments"):
			create_delivery_note(order, setting, so)


def create_sales_order(shopify_order, setting, company=None):
	"""Create Sales Order from Shopify order data.
	
	Args:
	    shopify_order: Shopify order data
	    setting: Store or Setting doc (backward compatible)
	    company: Optional company override
	"""
	customer = setting.default_customer
	store_name = setting.name if setting.doctype == STORE_DOCTYPE else None
	
	# Multi-store customer lookup
	if shopify_order.get("customer", {}):
		if customer_id := shopify_order.get("customer", {}).get("id"):
			if store_name:
				# Look up customer using multi-store child table
				customer_name = frappe.db.sql(
					"""
					SELECT parent 
					FROM `tabShopify Customer Store Link`
					WHERE store = %s AND shopify_customer_id = %s
					LIMIT 1
					""",
					(store_name, customer_id),
					as_dict=True,
				)
				if customer_name:
					customer = customer_name[0].parent
			else:
				# Backward compatibility: single-store lookup
				customer = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")

	so = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")

	if not so:
		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
			store_name=store_name,
		)

		if not items:
			message = (
				"Following items exists in the shopify order but relevant records were"
				" not found in the shopify Product master"
			)
			product_not_exists = []  # TODO: fix missing items
			message += "\n" + ", ".join(product_not_exists)

			create_shopify_log(status="Error", exception=message, rollback=True, store_name=store_name)

			return ""

		taxes = get_order_taxes(shopify_order, setting, items, store_name=store_name)
		
		# Get cost center and bank account based on sales channel
		cost_center, cash_bank_account = _get_channel_financials(
			shopify_order, setting
		)
		
		so_dict = {
			"doctype": "Sales Order",
			"naming_series": setting.sales_order_series or "SO-Shopify-",
			ORDER_ID_FIELD: str(shopify_order.get("id")),
			ORDER_NUMBER_FIELD: shopify_order.get("name"),
			"customer": customer,
			"transaction_date": getdate(shopify_order.get("created_at")) or nowdate(),
			"delivery_date": getdate(shopify_order.get("created_at")) or nowdate(),
			"company": setting.company,
			"selling_price_list": get_dummy_price_list(),
			"ignore_pricing_rule": 1,
			"items": items,
			"taxes": taxes,
			"tax_category": get_dummy_tax_category(),
		}
		
		# Add store reference for multi-store
		if store_name:
			so_dict[STORE_LINK_FIELD] = store_name
		
		so = frappe.get_doc(so_dict)

		if company:
			so.update({"company": company, "status": "Draft"})
		
		so.flags.ignore_mandatory = True
		so.flags.ignore_validate = True
		so.flags.ignore_validate_update_after_submit = True
		
		# Apply channel-specific cost center to all line items
		if cost_center:
			for item in so.items:
				item.cost_center = cost_center
		
		# Note: Bank account from channel mapping is used for financial reporting/reconciliation
		# It's tracked at the mapping level for identifying which account money flows to
		so.flags.shopiy_order_json = json.dumps(shopify_order)
		
		# Set UOM conversion factor to 1 for all items to avoid validation errors
		for item in so.items:
			if not item.conversion_factor or item.conversion_factor == 0:
				item.conversion_factor = 1.0
		
		so.save(ignore_permissions=True)
		so.submit()

		if shopify_order.get("note"):
			so.add_comment(text=f"Order Note: {shopify_order.get('note')}")
		
		# Sync Shopify tags to ERPNext native tagging system
		if shopify_order.get("tags"):
			_sync_order_tags(so, shopify_order.get("tags"))

	else:
		so = frappe.get_doc("Sales Order", so)

	return so


def get_order_items(order_items, setting, delivery_date, taxes_inclusive, store_name=None):
	"""Get line items for Sales Order.
	
	Args:
	    order_items: Shopify line items
	    setting: Store or Setting doc
	    delivery_date: Delivery date
	    taxes_inclusive: Whether taxes are included
	    store_name: Store name for multi-store item lookup
	"""
	items = []
	all_product_exists = True
	product_not_exists = []

	for shopify_item in order_items:
		item_code = None
		
		# Try to get item code even if product_exists is false
		# For Duoplane and similar integrations, product_id might be null but SKU exists
		if shopify_item.get("product_exists"):
			item_code = get_item_code(shopify_item, store_name=store_name)
		elif shopify_item.get("sku"):
			# Product doesn't exist in Shopify catalog but has SKU - try to match by SKU
			# This handles Duoplane, draft orders, and custom line items
			sku = shopify_item.get("sku")
			
			# Try multiple fields where SKU might be stored
			item_code = (
				frappe.db.get_value("Item", {"item_code": sku}) or
				frappe.db.get_value("Item", {"sku": sku}) or  # Variant SKU field
				frappe.db.get_value("Item", {"item_name": sku})
			)
		
		if not item_code:
			# Item not found - track for error reporting
			all_product_exists = False
			product_not_exists.append(
				{"title": shopify_item.get("title"), "sku": shopify_item.get("sku"), ORDER_ID_FIELD: shopify_item.get("id")}
			)
			continue
		
		# Get income account from Item, Item Group, or Company
		income_account = _get_income_account(item_code, setting.company)
		
		items.append(
			{
				"item_code": item_code,
				"item_name": shopify_item.get("name") or shopify_item.get("title"),
				"rate": _get_item_price(shopify_item, taxes_inclusive),
				"delivery_date": delivery_date,
				"qty": shopify_item.get("quantity"),
				"stock_uom": shopify_item.get("uom") or "Nos",
				"warehouse": setting.warehouse,
				"income_account": income_account,
				ORDER_ITEM_DISCOUNT_FIELD: (
					_get_total_discount(shopify_item) / cint(shopify_item.get("quantity"))
				),
			}
		)

	return items


def _get_item_price(line_item, taxes_inclusive: bool) -> float:
	price = flt(line_item.get("price"))
	qty = cint(line_item.get("quantity"))

	# remove line item level discounts
	total_discount = _get_total_discount(line_item)

	if not taxes_inclusive:
		return price - (total_discount / qty)

	total_taxes = 0.0
	for tax in line_item.get("tax_lines"):
		total_taxes += flt(tax.get("price"))

	return price - (total_taxes + total_discount) / qty


def _get_total_discount(line_item) -> float:
	discount_allocations = line_item.get("discount_allocations") or []
	return sum(flt(discount.get("amount")) for discount in discount_allocations)


def get_order_taxes(shopify_order, setting, items, store_name=None):
	"""Get tax lines for Sales Order.
	
	Args:
	    shopify_order: Shopify order data
	    setting: Store or Setting doc
	    items: Sales Order items
	    store_name: Store name for multi-store tax account lookup
	"""
	taxes = []
	line_items = shopify_order.get("line_items")

	for line_item in line_items:
		item_code = get_item_code(line_item, store_name=store_name)
		for tax in line_item.get("tax_lines"):
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(tax, charge_type="sales_tax", setting=setting),
					"description": (
						get_tax_account_description(tax, setting=setting)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax.get("price"),
					"included_in_print_rate": 0,
					"cost_center": setting.cost_center,
					"item_wise_tax_detail": {item_code: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]},
					"dont_recompute_tax": 1,
				}
			)

	update_taxes_with_shipping_lines(
		taxes,
		shopify_order.get("shipping_lines"),
		setting,
		items,
		taxes_inclusive=shopify_order.get("taxes_included"),
		store_name=store_name,
	)

	if cint(setting.consolidate_taxes):
		taxes = consolidate_order_taxes(taxes)

	for row in taxes:
		tax_detail = row.get("item_wise_tax_detail")
		if isinstance(tax_detail, dict):
			row["item_wise_tax_detail"] = json.dumps(tax_detail)

	return taxes


def consolidate_order_taxes(taxes):
	tax_account_wise_data = {}
	for tax in taxes:
		account_head = tax["account_head"]
		tax_account_wise_data.setdefault(
			account_head,
			{
				"charge_type": "Actual",
				"account_head": account_head,
				"description": tax.get("description"),
				"cost_center": tax.get("cost_center"),
				"included_in_print_rate": 0,
				"dont_recompute_tax": 1,
				"tax_amount": 0,
				"item_wise_tax_detail": {},
			},
		)
		tax_account_wise_data[account_head]["tax_amount"] += flt(tax.get("tax_amount"))
		if tax.get("item_wise_tax_detail"):
			tax_account_wise_data[account_head]["item_wise_tax_detail"].update(tax["item_wise_tax_detail"])

	return tax_account_wise_data.values()


def get_tax_account_head(tax, charge_type: Literal["shipping", "sales_tax"] | None = None, setting=None):
	"""Get tax account head for a tax line.
	
	Args:
	    tax: Tax line data
	    charge_type: Type of charge ("shipping" or "sales_tax")
	    setting: Store or Setting doc for multi-store support
	"""
	tax_title = str(tax.get("title"))
	
	# Determine parent doctype for tax account lookup
	if setting:
		parent_doctype = setting.doctype
		parent_name = setting.name
	else:
		parent_doctype = SETTING_DOCTYPE
		parent_name = SETTING_DOCTYPE

	tax_account = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": parent_name, "parenttype": parent_doctype, "shopify_tax": tax_title},
		"tax_account",
	)

	if not tax_account and charge_type:
		# Try default tax account
		if parent_doctype == STORE_DOCTYPE:
			tax_account = setting.get(DEFAULT_TAX_FIELDS[charge_type])
		else:
			tax_account = frappe.db.get_single_value(SETTING_DOCTYPE, DEFAULT_TAX_FIELDS[charge_type])

	if not tax_account:
		frappe.throw(_("Tax Account not specified for Shopify Tax {0}").format(tax.get("title")))

	return tax_account


def get_tax_account_description(tax, setting=None):
	"""Get tax account description for a tax line.
	
	Args:
	    tax: Tax line data
	    setting: Store or Setting doc for multi-store support
	"""
	tax_title = tax.get("title")
	
	# Determine parent doctype for tax account lookup
	if setting:
		parent_doctype = setting.doctype
		parent_name = setting.name
	else:
		parent_doctype = SETTING_DOCTYPE
		parent_name = SETTING_DOCTYPE

	tax_description = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": parent_name, "parenttype": parent_doctype, "shopify_tax": tax_title},
		"tax_description",
	)

	return tax_description


def update_taxes_with_shipping_lines(taxes, shipping_lines, setting, items, taxes_inclusive=False, store_name=None):
	"""Shipping lines represents the shipping details,
	each such shipping detail consists of a list of tax_lines
	
	Args:
	    taxes: Tax lines list to update
	    shipping_lines: Shopify shipping lines
	    setting: Store or Setting doc
	    items: Sales Order items
	    taxes_inclusive: Whether taxes are included
	    store_name: Store name for multi-store support
	"""
	shipping_as_item = cint(setting.add_shipping_as_item) and setting.shipping_item
	for shipping_charge in shipping_lines:
		if shipping_charge.get("price"):
			shipping_discounts = shipping_charge.get("discount_allocations") or []
			total_discount = sum(flt(discount.get("amount")) for discount in shipping_discounts)

			shipping_taxes = shipping_charge.get("tax_lines") or []
			total_tax = sum(flt(discount.get("price")) for discount in shipping_taxes)

			shipping_charge_amount = flt(shipping_charge["price"]) - flt(total_discount)
			if bool(taxes_inclusive):
				shipping_charge_amount -= total_tax

			if shipping_as_item:
				items.append(
					{
						"item_code": setting.shipping_item,
						"rate": shipping_charge_amount,
						"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
						"qty": 1,
						"stock_uom": "Nos",
						"warehouse": setting.warehouse,
					}
				)
			else:
				taxes.append(
					{
						"charge_type": "Actual",
						"account_head": get_tax_account_head(shipping_charge, charge_type="shipping", setting=setting),
						"description": get_tax_account_description(shipping_charge, setting=setting)
						or shipping_charge["title"],
						"tax_amount": shipping_charge_amount,
						"cost_center": setting.cost_center,
					}
				)

		for tax in shipping_charge.get("tax_lines"):
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(tax, charge_type="sales_tax", setting=setting),
					"description": (
						get_tax_account_description(tax, setting=setting)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax["price"],
					"cost_center": setting.cost_center,
					"item_wise_tax_detail": {
						setting.shipping_item: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]
					}
					if shipping_as_item
					else {},
					"dont_recompute_tax": 1,
				}
			)


def get_sales_order(order_id):
	"""Get ERPNext sales order using shopify order id."""
	sales_order = frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: order_id})
	if sales_order:
		return frappe.get_doc("Sales Order", sales_order)


def cancel_order(payload, request_id=None, store_name=None):
	"""Called by order/cancelled event.

	When shopify order is cancelled there could be many different ways someone handles it.

	Updates document with custom field showing order status.

	IF sales invoice / delivery notes are not generated against an order, then cancel it.
	
	Args:
	    payload: Shopify order data
	    request_id: Integration log ID
	    store_name: Shopify Store name
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	order = payload

	try:
		order_id = order["id"]
		order_status = order["financial_status"]

		sales_order = get_sales_order(order_id)

		if not sales_order:
			create_shopify_log(status="Invalid", message="Sales Order does not exist", store_name=store_name)
			return

		sales_invoice = frappe.db.get_value("Sales Invoice", filters={ORDER_ID_FIELD: order_id})
		delivery_notes = frappe.db.get_list("Delivery Note", filters={ORDER_ID_FIELD: order_id})

		if sales_invoice:
			frappe.db.set_value("Sales Invoice", sales_invoice, ORDER_STATUS_FIELD, order_status)

		for dn in delivery_notes:
			frappe.db.set_value("Delivery Note", dn.name, ORDER_STATUS_FIELD, order_status)

		if not sales_invoice and not delivery_notes and sales_order.docstatus == 1:
			sales_order.cancel()
		else:
			frappe.db.set_value("Sales Order", sales_order.name, ORDER_STATUS_FIELD, order_status)

	except Exception as e:
		create_shopify_log(status="Error", exception=e, store_name=store_name)
	else:
		create_shopify_log(status="Success", store_name=store_name)


@temp_shopify_session
def sync_old_orders():
	"""Backward compatibility: sync old orders for singleton setting."""
	shopify_setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not cint(shopify_setting.sync_old_orders):
		return

	orders = _fetch_old_orders(shopify_setting.old_orders_from, shopify_setting.old_orders_to)

	for order in orders:
		log = create_shopify_log(
			method=EVENT_MAPPER["orders/create"], request_data=json.dumps(order), make_new=True
		)
		sync_sales_order(order, request_id=log.name)

	shopify_setting = frappe.get_doc(SETTING_DOCTYPE)
	shopify_setting.sync_old_orders = 0
	shopify_setting.save()


@frappe.whitelist()
@temp_shopify_session
def sync_old_orders_for_store(store_name: str):
	"""Per-store worker: sync old orders for a specific store.
	
	Args:
	    store_name: Shopify Store name
	"""
	store = frappe.get_doc(STORE_DOCTYPE, store_name)
	
	if not cint(store.sync_old_orders):
		return

	orders = _fetch_old_orders(store.old_orders_from, store.old_orders_to)

	for order in orders:
		log = create_shopify_log(
			method=EVENT_MAPPER["orders/create"],
			request_data=json.dumps(order),
			make_new=True,
			store_name=store_name,
		)
		sync_sales_order(order, request_id=log.name, store_name=store_name)

	# Mark sync as complete
	store = frappe.get_doc(STORE_DOCTYPE, store_name)
	store.sync_old_orders = 0
	store.save()


def _get_income_account(item_code: str, company: str) -> str:
	"""Get income account for an item, falling back through Item → Item Group → Company.
	
	Args:
	    item_code: ERPNext Item code
	    company: Company name
	
	Returns:
	    Income account name
	"""
	# Try to get from Item's company-specific account
	item_doc = frappe.get_cached_doc("Item", item_code)
	
	# Check item's income account for this company
	for account in item_doc.get("item_defaults", []):
		if account.company == company and account.income_account:
			return account.income_account
	
	# Fall back to Item Group's default income account
	if item_doc.item_group:
		item_group_doc = frappe.get_cached_doc("Item Group", item_doc.item_group)
		for account in item_group_doc.get("accounts", []):
			if account.company == company and account.income_account:
				return account.income_account
	
	# Fall back to Company's default income account
	company_doc = frappe.get_cached_doc("Company", company)
	if company_doc.default_income_account:
		return company_doc.default_income_account
	
	# Last resort - get any income account for this company
	income_account = frappe.db.get_value(
		"Account",
		{
			"company": company,
			"account_type": "Income Account",
			"is_group": 0
		},
		"name"
	)
	
	return income_account or None


def _get_channel_financials(shopify_order: dict, setting) -> tuple[str | None, str | None]:
	"""Get cost center and bank account based on order's sales channel.
	
	Args:
	    shopify_order: Shopify order data dict
	    setting: Shopify Store or Setting doc
	
	Returns:
	    tuple: (cost_center, cash_bank_account) or (None, None) if using defaults
	"""
	source_name = shopify_order.get("source_name", "").lower().strip()
	
	if not source_name or not hasattr(setting, "sales_channel_mapping"):
		# No source or no mapping table - use defaults
		return None, None
	
	# Look up in sales channel mapping table
	for mapping in setting.sales_channel_mapping:
		if mapping.sales_channel_name.lower().strip() == source_name:
			return mapping.cost_center, mapping.cash_bank_account
	
	# No mapping found - use defaults
	return None, None


def _sync_order_tags(sales_order, shopify_tags: str) -> None:
	"""Parse Shopify tags and add them to Sales Order using ERPNext native tagging.
	
	Args:
	    sales_order: ERPNext Sales Order document
	    shopify_tags: Comma-separated string of tags from Shopify (e.g., "wholesale, priority")
	"""
	if not shopify_tags or not isinstance(shopify_tags, str):
		return
	
	# Parse comma-separated tags and clean them
	tags = [tag.strip() for tag in shopify_tags.split(",") if tag.strip()]
	
	# Add each tag using ERPNext's native tagging system
	from frappe.desk.doctype.tag.tag import add_tag
	for tag in tags:
		try:
			add_tag(tag, "Sales Order", sales_order.name)
		except Exception as e:
			# Don't fail the order sync if tagging fails
			frappe.log_error(
				message=f"Failed to add tag '{tag}' to Sales Order {sales_order.name}: {str(e)}",
				title="Shopify Tag Sync Error"
			)


def _fetch_old_orders(from_time, to_time):
	"""Fetch all shopify orders in specified range and return an iterator on fetched orders."""

	from_time = get_datetime(from_time).astimezone().isoformat()
	to_time = get_datetime(to_time).astimezone().isoformat()
	orders_iterator = PaginatedIterator(
		Order.find(created_at_min=from_time, created_at_max=to_time, limit=250)
	)

	for orders in orders_iterator:
		for order in orders:
			# Using generator instead of fetching all at once is better for
			# avoiding rate limits and reducing resource usage.
			yield order.to_dict()
