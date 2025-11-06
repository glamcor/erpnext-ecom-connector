# Privacy Policy

**Multi-Store Ecommerce Integrations for ERPNext**

## Overview

This privacy policy explains how the Multi-Store Ecommerce Integrations app handles data. This app is a technical integration that runs within your ERPNext instance and connects to your ecommerce platforms.

## Data Collection by the App Developers

**We do not collect, store, or transmit any of your data to our servers.**

This is an open-source integration that runs entirely within your own ERPNext environment. All data synchronization occurs directly between your ERPNext instance and your connected ecommerce platforms (Shopify, Amazon, Unicommerce, Zenoti).

## Data Flow

The app facilitates data synchronization between:

1. **Your ERPNext Instance** ← → **Your Shopify Store(s)**
2. **Your ERPNext Instance** ← → **Your Amazon Seller Account**
3. **Your ERPNext Instance** ← → **Your Unicommerce Account**
4. **Your ERPNext Instance** ← → **Your Zenoti Account**

All data flows directly between systems you control or have accounts with. No data passes through our servers.

## Data Stored

The app stores the following in your ERPNext database:

- API credentials for your ecommerce platforms (encrypted)
- Synchronization settings and mappings
- Cached data from ecommerce platforms (orders, products, customers, inventory levels)
- Sync logs and error logs

All of this data is stored in **your** ERPNext database on **your** server (or your Frappe Cloud instance).

## Third-Party Services

This app connects to third-party services. Each service has its own privacy policy:

- **Shopify**: [https://www.shopify.com/legal/privacy](https://www.shopify.com/legal/privacy)
- **Amazon**: [https://www.amazon.com/gp/help/customer/display.html?nodeId=468496](https://www.amazon.com/gp/help/customer/display.html?nodeId=468496)
- **Unicommerce**: Please refer to Unicommerce's privacy policy
- **Zenoti**: Please refer to Zenoti's privacy policy

## Your Responsibilities

As the operator of this integration, you are responsible for:

- Securing your ERPNext instance and API credentials
- Complying with data protection regulations (GDPR, CCPA, etc.) in your jurisdiction
- Informing your customers about how their data is processed
- Managing data retention and deletion in accordance with your policies
- Securing the connection between your ERPNext instance and ecommerce platforms

## API Usage

The app uses official APIs provided by each platform:

- Shopify API (REST and GraphQL)
- Amazon SP-API
- Unicommerce API
- Zenoti API

API rate limits are respected, and the app includes built-in rate limiting to prevent exceeding platform limits.

## Security

We recommend:

- Using HTTPS/TLS for all connections
- Regularly updating the app to receive security patches
- Following ERPNext security best practices
- Restricting API permissions to only what is necessary
- Regularly reviewing integration logs for suspicious activity

## Open Source Transparency

This is open-source software. You can review the complete source code at:
[https://github.com/glamcor/erpnext-ecom-connector](https://github.com/glamcor/erpnext-ecom-connector)

## Changes to This Policy

We may update this privacy policy from time to time. We will notify users of significant changes through the GitHub repository.

## Questions or Concerns

If you have questions about this privacy policy or how the app handles data, please:

- Open an issue on GitHub: [https://github.com/glamcor/erpnext-ecom-connector/issues](https://github.com/glamcor/erpnext-ecom-connector/issues)
- Review the source code to understand exactly how data is processed

---

**Last Updated**: November 2025

