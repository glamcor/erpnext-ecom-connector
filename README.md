<div align="center">
    <img src="https://frappecloud.com/files/ERPNext%20-%20Ecommerce%20Integrations.png" height="128">
    <h2>Multi-Store Ecommerce Integrations for ERPNext</h2>
  
</div>

### 🎉 Multi-Store Support

This enhanced version adds **multi-store capabilities** to the Shopify integration, allowing you to manage multiple Shopify stores from a single ERPNext instance.

**Key Features:**
- ✅ Connect unlimited Shopify stores to one ERPNext instance
- ✅ Per-store configuration (warehouses, tax accounts, price lists)
- ✅ Intelligent webhook routing by store domain
- ✅ Per-store API rate limiting for optimal performance
- ✅ Parallel background job processing for each store
- ✅ Multi-store customer, item, and order management
- ✅ Store-tagged logging for easy troubleshooting

### Currently supported integrations:

- **Shopify** (with multi-store support) - [User documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/shopify_integration)
- Unicommerce - [User Documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/unicommerce_integration)
- Zenoti - [User documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/zenoti_integration)
- Amazon - [User documentation](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/amazon_integration)


### Installation

- Frappe Cloud Users can install from the Frappe Marketplace by searching for "Multi-Store Ecommerce Integrations"
- Self Hosted users can install using Bench:

```bash
# Production installation
$ bench get-app https://github.com/glamcor/erpnext-ecom-connector --branch main

# OR development install
$ bench get-app https://github.com/glamcor/erpnext-ecom-connector --branch develop

# install on site
$ bench --site sitename install-app ecommerce_integrations
```

**Note:** The module/folder name remains `ecommerce_integrations` but the app is published as `ecommerce_integrations_multistore` in the marketplace.

After installation follow user documentation for each integration to set it up.

### Contributing

- Follow general [ERPNext contribution guideline](https://github.com/frappe/erpnext/wiki/Contribution-Guidelines)
- Send PRs to `develop` branch only.

### Development setup

- Enable developer mode.
- If you want to use a tunnel for local development. Set `localtunnel_url` parameter in your site_config file with ngrok / localtunnel URL. This will be used in most places to register webhooks. Likewise, use this parameter wherever you're sending current site URL to integrations in development mode.


#### License

GNU GPL v3.0
