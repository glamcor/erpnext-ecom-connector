# Copyright (c) 2025, Frappe and contributors
# For license information, please see LICENSE

"""
Orchestrator for multi-store Shopify operations.
Dispatches per-store jobs in parallel for isolation and rate limiting.
"""

import frappe
from frappe import _

from ecommerce_integrations.shopify.constants import STORE_DOCTYPE


def orchestrate_inventory_sync():
	"""Main entry point called by scheduler. Enqueues per-store inventory sync jobs."""
	stores = frappe.get_all(
		STORE_DOCTYPE,
		filters={"enabled": 1, "update_erpnext_stock_levels_to_shopify": 1},
		pluck="name",
	)

	for store_name in stores:
		# Deduplicate prevents multiple jobs for same store running simultaneously
		frappe.enqueue(
			method="ecommerce_integrations.shopify.inventory.update_inventory_for_store",
			queue="short",
			store_name=store_name,
			deduplicate=True,
			timeout=600,
			enqueue_after_commit=True,
		)


def orchestrate_order_sync():
	"""Sync old orders per store in parallel."""
	stores = frappe.get_all(
		STORE_DOCTYPE,
		filters={"enabled": 1, "sync_old_orders": 1},
		pluck="name",
	)

	for store_name in stores:
		frappe.enqueue(
			method="ecommerce_integrations.shopify.order.sync_old_orders_for_store",
			queue="long",
			store_name=store_name,
			deduplicate=True,
			timeout=7200,  # 2 hours for large order syncs
			enqueue_after_commit=True,
		)


def orchestrate_product_sync():
	"""Sync products to Shopify per store (if upload is enabled)."""
	stores = frappe.get_all(
		STORE_DOCTYPE,
		filters={"enabled": 1, "upload_erpnext_items": 1},
		pluck="name",
	)

	for store_name in stores:
		frappe.enqueue(
			method="ecommerce_integrations.shopify.product.sync_products_for_store",
			queue="long",
			store_name=store_name,
			deduplicate=True,
			timeout=3600,
			enqueue_after_commit=True,
		)

