from collections import Counter

import frappe
from frappe.utils import cint, create_batch, now
from pyactiveresource.connection import ResourceNotFound
from shopify.resources import InventoryLevel, Variant

from ecommerce_integrations_multistore.controllers.inventory import (
	get_inventory_levels,
	update_inventory_sync_status,
)
from ecommerce_integrations_multistore.controllers.scheduling import need_to_run
from ecommerce_integrations_multistore.shopify.connection import temp_shopify_session
from ecommerce_integrations_multistore.shopify.constants import MODULE_NAME, SETTING_DOCTYPE, STORE_DOCTYPE
from ecommerce_integrations_multistore.shopify.utils import create_shopify_log


def update_inventory_on_shopify() -> None:
	"""Upload stock levels from ERPNext to Shopify (singleton/legacy).

	Called by scheduler on configured interval.
	Backward compatible with singleton Shopify Setting.
	"""
	if not frappe.db.exists("DocType", SETTING_DOCTYPE):
		return
		
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_shopify:
		return

	if not need_to_run(SETTING_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
		return

	warehous_map = setting.get_erpnext_to_integration_wh_mapping()
	inventory_levels = get_inventory_levels(tuple(warehous_map.keys()), MODULE_NAME)

	if inventory_levels:
		upload_inventory_data_to_shopify(inventory_levels, warehous_map)


def update_inventory_for_store(store_name: str) -> None:
	"""Per-store worker: upload stock levels from ERPNext to Shopify for a specific store.
	
	Called by orchestrator for each enabled store with inventory sync enabled.
	
	Args:
	    store_name: Shopify Store name
	"""
	from ecommerce_integrations_multistore.shopify.rate_limiter import get_rate_limiter
	
	store = frappe.get_doc(STORE_DOCTYPE, store_name)

	if not store.is_enabled() or not store.update_erpnext_stock_levels_to_shopify:
		return

	# Check if sync is needed based on store-specific frequency
	if not _need_to_run_for_store(store):
		return

	# Get rate limiter for this store
	rate_limiter = get_rate_limiter(store_name, api_type="rest")
	
	warehouse_map = store.get_erpnext_to_integration_wh_mapping()
	inventory_levels = get_inventory_levels(tuple(warehouse_map.keys()), MODULE_NAME, store_name=store_name)

	if inventory_levels:
		upload_inventory_data_to_shopify_for_store(
			inventory_levels, warehouse_map, store, rate_limiter, store_name
		)


def _need_to_run_for_store(store) -> bool:
	"""Check if inventory sync should run for this store based on frequency setting."""
	from frappe.utils import add_to_date, cint, get_datetime, now
	
	interval = cint(store.inventory_sync_frequency, default=60)
	last_run = store.last_inventory_sync

	if last_run and get_datetime() < get_datetime(add_to_date(last_run, minutes=interval)):
		return False

	# Update last sync time
	frappe.db.set_value(STORE_DOCTYPE, store.name, "last_inventory_sync", now(), update_modified=False)
	return True


@temp_shopify_session
def upload_inventory_data_to_shopify(inventory_levels, warehous_map) -> None:
	"""Legacy: upload inventory for singleton setting."""
	synced_on = now()

	for inventory_sync_batch in create_batch(inventory_levels, 50):
		for d in inventory_sync_batch:
			d.shopify_location_id = warehous_map[d.warehouse]

			try:
				variant = Variant.find(d.variant_id)
				inventory_id = variant.inventory_item_id

				InventoryLevel.set(
					location_id=d.shopify_location_id,
					inventory_item_id=inventory_id,
					# shopify doesn't support fractional quantity
					available=cint(d.actual_qty) - cint(d.reserved_qty),
				)
				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Success"
			except ResourceNotFound:
				# Variant or location is deleted, mark as last synced and ignore.
				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Not Found"
			except Exception as e:
				d.status = "Failed"
				d.failure_reason = str(e)

			frappe.db.commit()

		_log_inventory_update_status(inventory_sync_batch)


@temp_shopify_session
def upload_inventory_data_to_shopify_for_store(inventory_levels, warehouse_map, store, rate_limiter, store_name) -> None:
	"""Per-store inventory upload with rate limiting.
	
	Args:
	    inventory_levels: Inventory data to sync
	    warehouse_map: Warehouse to location mapping
	    store: Shopify Store doc
	    rate_limiter: Rate limiter for this store
	    store_name: Store name for logging
	"""
	synced_on = now()

	for inventory_sync_batch in create_batch(inventory_levels, 50):
		for d in inventory_sync_batch:
			d.shopify_location_id = warehouse_map[d.warehouse]

			# Apply rate limiting before API call
			rate_limiter.wait_if_needed(cost=1)

			try:
				variant = Variant.find(d.variant_id)
				inventory_id = variant.inventory_item_id

				InventoryLevel.set(
					location_id=d.shopify_location_id,
					inventory_item_id=inventory_id,
					# shopify doesn't support fractional quantity
					available=cint(d.actual_qty) - cint(d.reserved_qty),
				)
				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Success"
				
				# Record successful API call
				rate_limiter.record_request(cost=1)
			except ResourceNotFound:
				# Variant or location is deleted, mark as last synced and ignore.
				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Not Found"
			except Exception as e:
				d.status = "Failed"
				d.failure_reason = str(e)

			frappe.db.commit()

		_log_inventory_update_status_for_store(inventory_sync_batch, store_name)


def _log_inventory_update_status_for_store(inventory_levels, store_name) -> None:
	"""Create log of inventory update for a specific store."""
	log_message = "variant_id,location_id,status,failure_reason\n"

	log_message += "\n".join(
		f"{d.variant_id},{d.shopify_location_id},{d.status},{d.failure_reason or ''}"
		for d in inventory_levels
	)

	stats = Counter([d.status for d in inventory_levels])

	percent_successful = stats["Success"] / len(inventory_levels) if inventory_levels else 0

	if percent_successful == 0:
		status = "Failed"
	elif percent_successful < 1:
		status = "Partial Success"
	else:
		status = "Success"

	log_message = f"Updated {percent_successful * 100}% items\n\n" + log_message

	create_shopify_log(
		method="update_inventory_for_store",
		status=status,
		message=log_message,
		store_name=store_name,
	)


def _log_inventory_update_status(inventory_levels) -> None:
	"""Create log of inventory update."""
	log_message = "variant_id,location_id,status,failure_reason\n"

	log_message += "\n".join(
		f"{d.variant_id},{d.shopify_location_id},{d.status},{d.failure_reason or ''}"
		for d in inventory_levels
	)

	stats = Counter([d.status for d in inventory_levels])

	percent_successful = stats["Success"] / len(inventory_levels)

	if percent_successful == 0:
		status = "Failed"
	elif percent_successful < 1:
		status = "Partial Success"
	else:
		status = "Success"

	log_message = f"Updated {percent_successful * 100}% items\n\n" + log_message

	create_shopify_log(method="update_inventory_on_shopify", status=status, message=log_message)
