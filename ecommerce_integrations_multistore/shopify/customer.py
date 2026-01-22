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
from ecommerce_integrations_multistore.utils.document_locking import document_lock, safe_document_update


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

	def get_customer_doc(self):
		"""Get ERPNext customer document.
		
		For multi-store, looks up via the child table link.
		For singleton, uses legacy field lookup.
		Uses cached reference if available (during sync_customer flow).
		"""
		# Use cached reference if available (set during sync_customer)
		if hasattr(self, '_customer_doc') and self._customer_doc:
			return self._customer_doc
			
		if self.store_name:
			# Multi-store lookup via child table
			link = frappe.db.get_value(
				"Shopify Customer Store Link",
				{"store": self.store_name, "shopify_customer_id": self.customer_id},
				"parent"
			)
			if link:
				return frappe.get_doc("Customer", link)
			else:
				raise frappe.DoesNotExistError()
		else:
			# Legacy single-store lookup
			return super().get_customer_doc()

	def sync_customer(self, customer: dict[str, Any]) -> None:
		"""Create Customer in ERPNext using shopify's Customer dict."""

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		if len(customer_name.strip()) == 0:
			customer_name = customer.get("email")

		customer_group = self.setting.customer_group
		
		# Try to find existing customer by email first (if enabled in settings)
		email = customer.get("email")
		existing_customer = None
		
		if email and getattr(self.setting, "match_customers_by_email", True):
			# Check if customer exists with this email
			existing_customer = frappe.db.get_value(
				"Customer", 
				{"email_id": email}, 
				["name", self.customer_id_field],
				as_dict=True
			)
			
			if existing_customer and not existing_customer.get(self.customer_id_field):
				# Customer exists but doesn't have Shopify ID - try to update it
				try:
					# Use a quick update without locking for this simple field update
					frappe.db.set_value(
						"Customer", 
						existing_customer.name, 
						self.customer_id_field, 
						self.customer_id,
						update_modified=False
					)
					frappe.db.commit()
					customer_doc = frappe.get_doc("Customer", existing_customer.name)
				except Exception as e:
					# If update fails (likely due to concurrent update), just get the customer
					# Another process probably already set the Shopify ID
					frappe.log_error(
						message=f"Failed to set Shopify ID for customer {existing_customer.name}: {str(e)}. Will proceed with existing customer.",
						title="Customer Shopify ID Update"
					)
					customer_doc = frappe.get_doc("Customer", existing_customer.name)
			elif existing_customer:
				# Customer exists and has Shopify ID
				customer_doc = frappe.get_doc("Customer", existing_customer.name)
			else:
				# No existing customer found by email, create new
				customer_doc = super().sync_customer(customer_name, customer_group)
		else:
			# No email provided, use standard sync
			customer_doc = super().sync_customer(customer_name, customer_group)

		# For multi-store, add entry to child table if not already linked
		if self.store_name and customer_doc:
			# Check if this store link already exists
			link_exists = frappe.db.exists(
				"Shopify Customer Store Link",
				{"store": self.store_name, "shopify_customer_id": self.customer_id}
			)
			if not link_exists:
				self._add_store_link_direct(customer_doc)
		
		# Store customer doc reference for address creation
		# This avoids lookup issues during the same transaction
		self._customer_doc = customer_doc

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

	def _add_store_link_direct(self, customer_doc) -> None:
		"""Add customer-store link to multi-store child table using customer doc directly."""
		
		# First, do a quick database check to see if link already exists
		link_exists = frappe.db.exists({
			"doctype": "Shopify Customer Store Link",
			"parent": customer_doc.name,
			"parenttype": "Customer",
			"store": self.store_name,
			"shopify_customer_id": self.customer_id
		})
		
		if link_exists:
			# Link already exists, no need to update
			return
		
		# Try to add the link with minimal locking
		try:
			# Create the child record directly
			link_doc = frappe.get_doc({
				"doctype": "Shopify Customer Store Link",
				"parent": customer_doc.name,
				"parenttype": "Customer",
				"parentfield": "shopify_store_customer_links",
				"store": self.store_name,
				"shopify_customer_id": self.customer_id,
				"last_synced_on": frappe.utils.now(),
			})
			link_doc.insert(ignore_permissions=True)
			
			# Update the modified timestamp of parent
			frappe.db.set_value(
				"Customer", 
				customer_doc.name, 
				"modified", 
				frappe.utils.now(),
				update_modified=False
			)
			
			frappe.db.commit()
			
		except frappe.DuplicateEntryError:
			# Another process already added this link, that's fine
			pass
		except Exception as e:
			# Fall back to the safe update method if direct insert fails
			frappe.log_error(
				message=f"Direct link insert failed for {customer_doc.name}, falling back to safe update: {str(e)}",
				title="Customer Link Insert Warning"
			)
			
			def update_customer_links(doc):
				# Check again if link exists (in case it was added while we were trying)
				for link in doc.get("shopify_store_customer_links", []):
					if link.store == self.store_name and str(link.shopify_customer_id) == str(self.customer_id):
						return  # Already exists
				
				doc.append("shopify_store_customer_links", {
					"store": self.store_name,
					"shopify_customer_id": self.customer_id,
					"last_synced_on": frappe.utils.now(),
				})
			
			# Use skip_if_locked to avoid long waits
			safe_document_update("Customer", customer_doc.name, update_customer_links, skip_if_locked=True)

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
		
		frappe.log_error(
			message=f"update_existing_addresses called for customer: '{customer_name}', has billing: {bool(billing_address)}, has shipping: {bool(shipping_address)}",
			title="Update Addresses Debug"
		)

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
		"""Update or create customer address.
		
		Strategy:
		- If Shopify address has ID: Look up by shopify_address_id (saved customer address)
		- If no ID (inline order address): Look up by EXACT CONTENT MATCH
		  - If content matches: Use existing address (no update needed)
		  - If content differs: CREATE NEW address (critical for drop-ship customers!)
		
		This is critical for drop-ship customers like "Knocking Orders" who place many orders
		with different shipping addresses. Each order's shipping address must be preserved
		as a separate Address document.
		"""
		shopify_address_id = shopify_address.get("id")
		
		# Extract address content for matching
		addr_line1 = shopify_address.get("address1") or ""
		addr_city = shopify_address.get("city") or ""
		addr_zip = shopify_address.get("zip") or ""
		
		# Try to find existing address
		old_address = None
		if shopify_address_id:
			# Saved customer address - lookup by Shopify ID
			frappe.log_error(
				message=f"Looking for {address_type} address by Shopify ID: {shopify_address_id}",
				title="Address Lookup - By ID"
			)
			old_address = frappe.db.get_value(
				"Address",
				{ADDRESS_ID_FIELD: shopify_address_id},
				"name"
			)
			if old_address:
				old_address = frappe.get_doc("Address", old_address)
				frappe.log_error(
					message=f"Found {address_type} address by ID: {old_address.name}",
					title="Address Lookup - Found By ID"
				)
		else:
			# Inline order address (no Shopify ID) - lookup by EXACT CONTENT MATCH
			# This is critical for drop-ship customers with many different shipping addresses
			frappe.log_error(
				message=f"Looking for {address_type} address by CONTENT for customer {customer_name}: {addr_line1}, {addr_city}, {addr_zip}",
				title="Address Lookup - By Content"
			)
			
			# Get customer doc to find linked addresses
			try:
				customer_doc = self.get_customer_doc()
				
				# Find address with matching content
				matching_address = frappe.db.sql("""
					SELECT a.name 
					FROM `tabAddress` a
					INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name
					WHERE dl.link_doctype = 'Customer' 
					AND dl.link_name = %s
					AND dl.parenttype = 'Address'
					AND a.address_type = %s
					AND a.address_line1 = %s
					AND a.city = %s
					AND a.pincode = %s
					LIMIT 1
				""", (customer_doc.name, address_type, addr_line1, addr_city, addr_zip), as_dict=True)
				
				if matching_address:
					old_address = frappe.get_doc("Address", matching_address[0].name)
					frappe.log_error(
						message=f"Found {address_type} address by content match: {old_address.name}",
						title="Address Lookup - Found By Content"
					)
				else:
					frappe.log_error(
						message=f"No content match found for {address_type} address. Will create new.",
						title="Address Lookup - No Content Match"
					)
			except frappe.DoesNotExistError:
				frappe.log_error(
					message=f"Customer doc not found during address lookup",
					title="Address Lookup - Customer Not Found"
				)

		if not old_address:
			# Address doesn't exist with this content - create new
			frappe.log_error(
				message=f"Creating new {address_type} address for {customer_name}. Address: {addr_line1}, {addr_city}, {addr_zip}",
				title="Customer Address Creation"
			)
			try:
				self.create_customer_address(customer_name, shopify_address, address_type, email)
				frappe.log_error(
					message=f"Successfully created {address_type} address for {customer_name}",
					title="Customer Address Created"
				)
			except Exception as addr_error:
				frappe.log_error(
					message=f"FAILED to create {address_type} address for {customer_name}: {str(addr_error)}\n{frappe.get_traceback()}",
					title="Customer Address Creation FAILED"
				)
				raise
		else:
			# Address exists with matching content
			if shopify_address_id:
				# Saved address - only update if data changed
				# For now, skip update to preserve historical data
				# (User can manage saved addresses in Shopify)
				pass
			else:
				# Inline address with matching content - update recipient name and company
				# (These might change even if address is the same)
				new_values = _map_address_fields(shopify_address, customer_name, address_type, email)
				
				# Only update recipient name and company, not the address itself
				fields_to_update = ["custom_recipient_name", "custom_company_name", "phone", "email_id"]
				for field in fields_to_update:
					if field in new_values and new_values[field]:
						setattr(old_address, field, new_values[field])
				
				old_address.flags.ignore_mandatory = True
				old_address.save()
				
				frappe.log_error(
					message=f"Updated recipient info on existing address {old_address.name}: {new_values.get('custom_recipient_name')}",
					title="Address Recipient Updated"
				)
			
			# For multi-store, update address-store link
			if self.store_name and shopify_address_id:
				self._add_address_store_link(shopify_address_id)

	def _add_address_store_link(self, shopify_address_id: str) -> None:
		"""Add address-store link to multi-store child table."""
		# Find the address by shopify_address_id (legacy field) or create new link
		address_name = frappe.db.get_value("Address", {ADDRESS_ID_FIELD: shopify_address_id}, "name")
		
		if not address_name:
			return
		
		# First, do a quick database check to see if link already exists
		link_exists = frappe.db.exists({
			"doctype": "Shopify Store Address Link",
			"parent": address_name,
			"parenttype": "Address",
			"store": self.store_name,
			"shopify_address_id": shopify_address_id
		})
		
		if link_exists:
			# Link already exists, no need to update
			return
		
		# Try to add the link with minimal locking
		try:
			# Create the child record directly
			link_doc = frappe.get_doc({
				"doctype": "Shopify Store Address Link",
				"parent": address_name,
				"parenttype": "Address",
				"parentfield": "shopify_store_address_links",
				"store": self.store_name,
				"shopify_address_id": shopify_address_id,
				"last_synced_on": frappe.utils.now(),
			})
			link_doc.insert(ignore_permissions=True)
			
			# Update the modified timestamp of parent
			frappe.db.set_value(
				"Address", 
				address_name, 
				"modified", 
				frappe.utils.now(),
				update_modified=False
			)
			
			frappe.db.commit()
			
		except frappe.DuplicateEntryError:
			# Another process already added this link, that's fine
			pass
		except Exception as e:
			# Fall back to the safe update method if direct insert fails
			frappe.log_error(
				message=f"Direct address link insert failed for {address_name}, falling back to safe update: {str(e)}",
				title="Address Link Insert Warning"
			)
			
			# Get fresh copy of address
			address_doc = frappe.get_doc("Address", address_name)
			
			# Check again if link exists (in case it was added while we were trying)
			for link in address_doc.get("shopify_store_address_links", []):
				if link.store == self.store_name and str(link.shopify_address_id) == str(shopify_address_id):
					return  # Already exists
			
			def update_address_links(doc):
				# Double-check before adding
				for link in doc.get("shopify_store_address_links", []):
					if link.store == self.store_name and str(link.shopify_address_id) == str(shopify_address_id):
						return  # Already exists
				
				doc.append("shopify_store_address_links", {
					"store": self.store_name,
					"shopify_address_id": shopify_address_id,
					"last_synced_on": frappe.utils.now(),
				})
			
			# Use skip_if_locked to avoid long waits
			safe_document_update("Address", address_name, update_address_links, skip_if_locked=True)

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
	"""returns dict with shopify address fields mapped to equivalent ERPNext fields
	
	Shopify address fields:
	- name: Full recipient name (e.g., "Marty Slayton Jordan")
	- first_name, last_name: Separate name fields
	- company: Company name if provided
	- address1, address2: Street address lines
	- city, province, zip, country: Location fields
	- phone: Phone number
	
	For drop-ship orders (like Knocking Orders), the customer in ERPNext is the 
	reseller, but the shipping recipient is different. We need to store the 
	recipient name from the shipping address.
	"""
	import hashlib
	
	# Build unique address title
	# Option A: Use Shopify address ID if available (customer saved addresses)
	# Option B: Use content hash for inline order addresses (critical for drop-ship!)
	shopify_address_id = shopify_address.get("id")
	if shopify_address_id:
		# Customer saved address - use ID for uniqueness
		address_title = f"{customer_name}-{shopify_address_id}"
	else:
		# Order inline address - use content hash for uniqueness
		# This is critical for drop-ship customers with many different addresses
		addr_content = f"{shopify_address.get('address1', '')}-{shopify_address.get('city', '')}-{shopify_address.get('zip', '')}"
		content_hash = hashlib.md5(addr_content.encode()).hexdigest()[:8]
		address_title = f"{customer_name}-{address_type}-{content_hash}"
	
	# Get recipient name from Shopify address
	# Priority: 'name' field, then first_name + last_name, then customer_name
	recipient_name = shopify_address.get("name")
	if not recipient_name:
		first = shopify_address.get("first_name", "")
		last = shopify_address.get("last_name", "")
		recipient_name = f"{first} {last}".strip()
	if not recipient_name:
		recipient_name = customer_name
	
	# Get company name (important for B2B/drop-ship)
	company_name = shopify_address.get("company", "")
	
	address_fields = {
		"address_title": address_title,
		"address_type": address_type,
		ADDRESS_ID_FIELD: shopify_address_id,
		"address_line1": shopify_address.get("address1") or "Address 1",
		"address_line2": shopify_address.get("address2"),
		"city": shopify_address.get("city"),
		"state": shopify_address.get("province"),
		"pincode": shopify_address.get("zip"),
		"country": shopify_address.get("country"),
		"email_id": email,
		# Store recipient name and company for ShipStation
		# These are custom fields that need to be created in ERPNext
		"custom_recipient_name": recipient_name,
		"custom_company_name": company_name,
	}

	phone = shopify_address.get("phone")
	if validate_phone_number(phone, throw=False):
		address_fields["phone"] = phone

	return address_fields
