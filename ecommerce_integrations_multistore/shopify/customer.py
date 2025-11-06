from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, validate_phone_number

from ecommerce_integrations_multistore.controllers.customer import EcommerceCustomer
from ecommerce_integrations_multistore.shopify.constants import (
	ADDRESS_ID_FIELD,
	CUSTOMER_ID_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
	STORE_DOCTYPE,
)


class ShopifyCustomer(EcommerceCustomer):
	def __init__(self, customer_id: str, store_name: str | None = None):
		"""Initialize Shopify Customer with optional store context.
		
		Args:
		    customer_id: Shopify customer ID
		    store_name: Shopify Store name for multi-store support
		"""
		self.store_name = store_name
		
		# Get store-specific or singleton settings
		if store_name:
			self.setting = frappe.get_doc(STORE_DOCTYPE, store_name)
		else:
			# Backward compatibility
			self.setting = frappe.get_doc(SETTING_DOCTYPE)
			
		super().__init__(customer_id, CUSTOMER_ID_FIELD, MODULE_NAME)

	def is_synced(self) -> bool:
		"""Check if customer is already synced for this store.
		
		For multi-store, checks the child table. For singleton, uses legacy field.
		"""
		if self.store_name:
			# Multi-store lookup via child table
			exists = frappe.db.exists(
				"Shopify Customer Store Link",
				{"store": self.store_name, "shopify_customer_id": self.customer_id}
			)
			return bool(exists)
		else:
			# Legacy single-store lookup
			return super().is_synced()

	def sync_customer(self, customer: dict[str, Any]) -> None:
		"""Create Customer in ERPNext using shopify's Customer dict."""

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		if len(customer_name.strip()) == 0:
			customer_name = customer.get("email")

		customer_group = self.setting.customer_group
		super().sync_customer(customer_name, customer_group)

		# For multi-store, add entry to child table
		if self.store_name and self.erpnext_customer_name:
			self._add_store_link()

		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		if billing_address:
			self.create_customer_address(
				customer_name, billing_address, address_type="Billing", email=customer.get("email")
			)
		if shipping_address:
			self.create_customer_address(
				customer_name, shipping_address, address_type="Shipping", email=customer.get("email")
			)

		self.create_customer_contact(customer)

	def _add_store_link(self) -> None:
		"""Add customer-store link to multi-store child table."""
		customer_doc = frappe.get_doc("Customer", self.erpnext_customer_name)
		
		# Check if link already exists
		existing = False
		for link in customer_doc.get("shopify_store_customer_links", []):
			if link.store == self.store_name and link.shopify_customer_id == self.customer_id:
				existing = True
				break
		
		if not existing:
			customer_doc.append("shopify_store_customer_links", {
				"store": self.store_name,
				"shopify_customer_id": self.customer_id,
				"last_synced_on": frappe.utils.now(),
			})
			customer_doc.save(ignore_permissions=True)

	def create_customer_address(
		self,
		customer_name,
		shopify_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		"""Create customer address(es) using Customer dict provided by shopify."""
		address_fields = _map_address_fields(shopify_address, customer_name, address_type, email)
		super().create_customer_address(address_fields)
		
		# For multi-store, add address-store link to child table
		if self.store_name and shopify_address.get("id"):
			self._add_address_store_link(shopify_address.get("id"))

	def update_existing_addresses(self, customer):
		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		email = customer.get("email")

		if billing_address:
			self._update_existing_address(customer_name, billing_address, "Billing", email)
		if shipping_address:
			self._update_existing_address(customer_name, shipping_address, "Shipping", email)

	def _update_existing_address(
		self,
		customer_name,
		shopify_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		old_address = self.get_customer_address_doc(address_type)

		if not old_address:
			self.create_customer_address(customer_name, shopify_address, address_type, email)
		else:
			exclude_in_update = ["address_title", "address_type"]
			new_values = _map_address_fields(shopify_address, customer_name, address_type, email)

			old_address.update({k: v for k, v in new_values.items() if k not in exclude_in_update})
			old_address.flags.ignore_mandatory = True
			old_address.save()
			
			# For multi-store, update address-store link
			if self.store_name and shopify_address.get("id"):
				self._add_address_store_link(shopify_address.get("id"))

	def _add_address_store_link(self, shopify_address_id: str) -> None:
		"""Add address-store link to multi-store child table."""
		# Find the address by shopify_address_id (legacy field) or create new link
		address_name = frappe.db.get_value("Address", {ADDRESS_ID_FIELD: shopify_address_id}, "name")
		
		if not address_name:
			return
		
		address_doc = frappe.get_doc("Address", address_name)
		
		# Check if link already exists
		existing = False
		for link in address_doc.get("shopify_store_address_links", []):
			if link.store == self.store_name and link.shopify_address_id == shopify_address_id:
				existing = True
				break
		
		if not existing:
			address_doc.append("shopify_store_address_links", {
				"store": self.store_name,
				"shopify_address_id": shopify_address_id,
				"last_synced_on": frappe.utils.now(),
			})
			address_doc.save(ignore_permissions=True)

	def create_customer_contact(self, shopify_customer: dict[str, Any]) -> None:
		if not (shopify_customer.get("first_name") and shopify_customer.get("email")):
			return

		contact_fields = {
			"status": "Passive",
			"first_name": shopify_customer.get("first_name"),
			"last_name": shopify_customer.get("last_name"),
			"unsubscribed": not shopify_customer.get("accepts_marketing"),
		}

		if shopify_customer.get("email"):
			contact_fields["email_ids"] = [{"email_id": shopify_customer.get("email"), "is_primary": True}]

		phone_no = shopify_customer.get("phone") or shopify_customer.get("default_address", {}).get("phone")

		if validate_phone_number(phone_no, throw=False):
			contact_fields["phone_nos"] = [{"phone": phone_no, "is_primary_phone": True}]

		super().create_customer_contact(contact_fields)


def _map_address_fields(shopify_address, customer_name, address_type, email):
	"""returns dict with shopify address fields mapped to equivalent ERPNext fields"""
	address_fields = {
		"address_title": customer_name,
		"address_type": address_type,
		ADDRESS_ID_FIELD: shopify_address.get("id"),
		"address_line1": shopify_address.get("address1") or "Address 1",
		"address_line2": shopify_address.get("address2"),
		"city": shopify_address.get("city"),
		"state": shopify_address.get("province"),
		"pincode": shopify_address.get("zip"),
		"country": shopify_address.get("country"),
		"email_id": email,
	}

	phone = shopify_address.get("phone")
	if validate_phone_number(phone, throw=False):
		address_fields["phone"] = phone

	return address_fields
