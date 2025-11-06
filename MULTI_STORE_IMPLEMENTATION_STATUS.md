# Multi-Store Shopify Implementation Status

## Overview
This document tracks the implementation of the multi-store Shopify architecture refactor.

## ✅ Completed Components

### Epic A: Data Model & Core DocTypes (COMPLETE)
- [x] Created `Shopify Store` DocType to replace singleton
- [x] Created `Shopify Customer Store Link` child table for multi-store customer mapping
- [x] Created `Shopify Address Store Link` child table for multi-store address mapping  
- [x] Created `Ecommerce Item Store Link` child table for multi-store item mapping
- [x] Updated `Ecommerce Item` doctype with `store_links` field
- [x] Updated `constants.py` with `STORE_DOCTYPE` and `STORE_LINK_FIELD`
- [x] Created custom fields for transactional docs (Sales Order, Invoice, DN) with `shopify_store` link

### Epic B: Webhook Dispatcher & HMAC Verification (COMPLETE)
- [x] Updated `store_request_data()` to route by `X-Shopify-Shop-Domain` header
- [x] Added `get_store_by_domain()` helper function
- [x] Updated `_validate_request()` to use store-specific `shared_secret`
- [x] Updated `process_request()` to pass `store_name` to webhook handlers
- [x] Updated `temp_shopify_session` decorator to accept `store_name` kwarg
- [x] Updated `register_webhooks()` and `unregister_webhooks()` for per-store context
- [x] Added `update_store_locations()` helper function

### Epic C: Orchestrator & Workers with Rate Limiting (COMPLETE)
- [x] Created `orchestrator.py` with:
  - `orchestrate_inventory_sync()` - dispatches per-store inventory jobs
  - `orchestrate_order_sync()` - dispatches per-store order sync jobs
  - `orchestrate_product_sync()` - dispatches per-store product jobs
- [x] Created `rate_limiter.py` with:
  - `ShopifyRateLimiter` class implementing token bucket algorithm
  - Support for REST (2/sec, burst 40) and GraphQL (cost-based) APIs
  - Per-store rate limit isolation using cache
- [x] Updated `hooks.py` to call orchestrator functions in scheduler
- [x] Maintained backward compatibility with singleton functions

### Epic D: Business Logic Refactoring (MOSTLY COMPLETE)

#### order.py (COMPLETE)
- [x] `sync_sales_order()` accepts `store_name` parameter
- [x] `create_sales_order()` uses store-specific settings and sets `shopify_store` field
- [x] Multi-store customer lookup via `Shopify Customer Store Link` child table
- [x] `get_order_items()` accepts `store_name` for item lookups
- [x] `get_order_taxes()` accepts `store_name` for tax account lookups
- [x] `get_tax_account_head()` and `get_tax_account_description()` use store-specific tax accounts
- [x] `update_taxes_with_shipping_lines()` accepts `store_name`
- [x] `cancel_order()` accepts `store_name` parameter
- [x] Added `sync_old_orders_for_store()` per-store worker function
- [x] Maintained `sync_old_orders()` for backward compatibility

#### product.py (COMPLETE)
- [x] `ShopifyProduct.__init__()` accepts `store_name` parameter
- [x] `is_synced()` and `get_erpnext_item()` use store context
- [x] `create_items_if_not_exist()` accepts `store_name`
- [x] `get_item_code()` accepts `store_name` for multi-store lookups

#### ecommerce_item.py (COMPLETE)
- [x] `is_synced()` supports multi-store via `Ecommerce Item Store Link` lookup
- [x] `_is_sku_synced()` supports multi-store SKU lookups
- [x] `get_erpnext_item_code()` supports multi-store child table queries
- [x] `get_erpnext_item()` supports multi-store lookups
- [x] Maintains backward compatibility for single-store/legacy mode

#### inventory.py (COMPLETE)
- [x] Added `update_inventory_for_store()` per-store worker function
- [x] Added `upload_inventory_data_to_shopify_for_store()` with rate limiting
- [x] Added `_need_to_run_for_store()` for store-specific sync frequency
- [x] Added `_log_inventory_update_status_for_store()` for store-tagged logs
- [x] Maintained `update_inventory_on_shopify()` for backward compatibility

#### utils.py (COMPLETE)
- [x] `create_shopify_log()` accepts `store_name` and tags logs with store reference

#### connection.py (COMPLETE)
- [x] All webhook and session management functions support multi-store

## ⚠️ Remaining Work

### Epic D: Business Logic (COMPLETE)

#### customer.py (COMPLETE ✅)
- [x] `ShopifyCustomer.__init__()` accepts `store_name` parameter
- [x] `is_synced()` uses `Shopify Customer Store Link` child table for multi-store
- [x] `sync_customer()` adds entries to `Shopify Customer Store Link` child table
- [x] `_add_store_link()` helper manages customer-store associations
- [x] Address sync uses `Shopify Address Store Link` child table
- [x] `_add_address_store_link()` helper manages address-store associations
- [x] Backward compatibility for singleton lookups maintained

#### invoice.py (COMPLETE ✅)
- [x] `prepare_sales_invoice()` accepts `store_name` parameter
- [x] Gets store from parent Sales Order's `shopify_store` field
- [x] `create_sales_invoice()` uses store-specific settings
- [x] Sets `shopify_store` field on Sales Invoice for multi-store tracking
- [x] Store-tagged logging for all operations

#### fulfillment.py (COMPLETE ✅)
- [x] `prepare_delivery_note()` accepts `store_name` parameter
- [x] Gets store from parent Sales Order's `shopify_store` field
- [x] `create_delivery_note()` uses store-specific settings
- [x] Sets `shopify_store` field on Delivery Note for multi-store tracking
- [x] `get_fulfillment_items()` uses store context for item lookups
- [x] Store-tagged logging for all operations

### Epic E: Observability (PARTIALLY COMPLETE)
- [x] Store-tagged logs via `create_shopify_log(store_name=...)`
- [ ] Add `shopify_store` custom field to `Ecommerce Integration Log` doctype
- [ ] Create dashboard for per-store metrics
- [ ] Add list view filters for store-based grouping

### Epic F: Testing & Documentation
- [ ] Update existing tests for multi-store scenarios
- [ ] Add tests for webhook routing by domain
- [ ] Add tests for rate limiting per store
- [ ] Add tests for parallel job orchestration
- [ ] Add tests for multi-store customer/item lookups
- [ ] Update README with multi-store setup guide
- [ ] Document rate limiting behavior
- [ ] Document per-store configuration options

## Architecture Highlights

### Multi-Store Data Model
- **Transactional docs** (Sales Order, Invoice, DN): Single `shopify_store` link field
- **Master data** (Item, Customer): Multi-value child tables for cross-store reuse
- **Settings**: Per-store configuration via `Shopify Store` doctype

### Webhook Architecture
- Single endpoint: `/api/method/ecommerce_integrations.shopify.connection.store_request_data`
- Routes by `X-Shopify-Shop-Domain` header to correct store
- Per-store HMAC validation using store-specific `shared_secret`
- Enqueues per-store background jobs with store context

### Job Orchestration
- Orchestrator functions dispatch parallel per-store jobs
- Each store gets isolated worker with dedicated rate limiter
- Token bucket algorithm prevents API rate limit violations
- Independent failure isolation (one store's issues don't block others)

### Rate Limiting
- Per-store token bucket with configurable rates
- REST API: 2 requests/sec, burst capacity of 40
- GraphQL API: Cost-based, 1000 points per 10 seconds
- Cache-based state management

### Backward Compatibility
- Singleton `Shopify Setting` still supported (deprecated)
- Legacy functions maintained alongside new multi-store functions
- Gradual migration path for existing installations

## Next Steps

1. ~~**Complete remaining business logic** (customer.py, invoice.py, fulfillment.py)~~ ✅ **DONE!**
2. **Add observability** (custom field on logs, dashboard, metrics)
3. **Comprehensive testing** (unit tests, integration tests, multi-store scenarios)
4. **Documentation** (setup guide, migration guide, API docs)
5. **Performance optimization** (query optimization, caching strategies)
6. **Production deployment** (staged rollout, monitoring, rollback plan)

## Key Files Created

- `shopify/doctype/shopify_store/` - Main multi-store doctype
- `shopify/doctype/shopify_customer_store_link/` - Customer multi-store mapping
- `shopify/doctype/shopify_address_store_link/` - Address multi-store mapping
- `ecommerce_integrations/doctype/ecommerce_item_store_link/` - Item multi-store mapping
- `shopify/orchestrator.py` - Job dispatcher
- `shopify/rate_limiter.py` - Per-store rate limiting

## Key Files Modified

- `shopify/constants.py` - Added STORE_DOCTYPE constant ✅
- `shopify/connection.py` - Webhook routing and session management ✅
- `shopify/order.py` - Multi-store order sync ✅
- `shopify/product.py` - Multi-store product sync ✅
- `shopify/inventory.py` - Per-store inventory workers ✅
- `shopify/customer.py` - Multi-store customer and address sync ✅
- `shopify/invoice.py` - Multi-store invoice creation ✅
- `shopify/fulfillment.py` - Multi-store delivery note creation ✅
- `shopify/utils.py` - Store-tagged logging ✅
- `ecommerce_integrations/doctype/ecommerce_item/ecommerce_item.py` - Multi-store lookups ✅
- `ecommerce_integrations/doctype/ecommerce_item/ecommerce_item.json` - Added store_links field ✅
- `hooks.py` - Orchestrator integration ✅

## Testing Checklist

- [ ] Webhook routing with multiple stores
- [ ] HMAC validation per store
- [ ] Parallel job execution
- [ ] Rate limiting isolation
- [ ] Multi-store item lookup
- [ ] Multi-store customer lookup
- [ ] Store-specific tax accounts
- [ ] Store-specific warehouse mappings
- [ ] Backward compatibility with singleton
- [ ] Migration from singleton to multi-store
- [ ] Error handling and logging
- [ ] Performance under load (10+ stores)

