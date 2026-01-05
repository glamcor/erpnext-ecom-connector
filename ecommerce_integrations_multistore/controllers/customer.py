import frappe
from frappe import _
from frappe.utils.nestedset import get_root_of


class EcommerceCustomer:
	def __init__(self, customer_id: str, customer_id_field: str, integration: str):
		self.customer_id = customer_id
		self.customer_id_field = customer_id_field
		self.integration = integration

	def is_synced(self) -> bool:
		"""Check if customer on Ecommerce site is synced with ERPNext"""

		return bool(frappe.db.exists("Customer", {self.customer_id_field: self.customer_id}))

	def get_customer_doc(self):
		"""Get ERPNext customer document."""
		if self.is_synced():
			return frappe.get_last_doc("Customer", {self.customer_id_field: self.customer_id})
		else:
			raise frappe.DoesNotExistError()

	def sync_customer(self, customer_name: str, customer_group: str) -> None:
		"""Create customer in ERPNext if one does not exist already.
		
		Returns the customer document (newly created or existing).
		"""
		if frappe.db.exists("Customer", self.customer_id):
			return frappe.get_doc("Customer", self.customer_id)
			
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"name": self.customer_id,
				self.customer_id_field: self.customer_id,
				"customer_name": customer_name,
				"customer_group": customer_group,
				"territory": get_root_of("Territory"),
				"customer_type": _("Individual"),
			}
		)

		customer.flags.ignore_mandatory = True
		customer.insert(ignore_permissions=True)
		return customer

	def get_customer_address_doc(self, address_type: str):
		try:
			customer = self.get_customer_doc().name
			addresses = frappe.get_all("Address", {"link_name": customer, "address_type": address_type})
			if addresses:
				address = frappe.get_last_doc("Address", {"name": addresses[0].name})
				return address
		except frappe.DoesNotExistError:
			return None

	def create_customer_address(self, address: dict[str, str]) -> None:
		"""Create address from dictionary containing fields used in Address doctype of ERPNext."""

		customer_doc = self.get_customer_doc()
		
		# Check if address already exists
		address_name = address.get("address_title")
		if address_name and frappe.db.exists("Address", address_name):
			# Update existing address instead of creating new one
			existing_address = frappe.get_doc("Address", address_name)
			
			# Update fields
			for key, value in address.items():
				if key != "address_title" and hasattr(existing_address, key):
					setattr(existing_address, key, value)
			
			# Ensure customer link exists
			customer_linked = False
			for link in existing_address.links:
				if link.link_doctype == "Customer" and link.link_name == customer_doc.name:
					customer_linked = True
					break
			
			if not customer_linked:
				existing_address.append("links", {
					"link_doctype": "Customer",
					"link_name": customer_doc.name
				})
			
			existing_address.save(ignore_permissions=True)
		else:
			# Create new address
			try:
				frappe.get_doc(
					{
						"doctype": "Address",
						**address,
						"links": [{"link_doctype": "Customer", "link_name": customer_doc.name}],
					}
				).insert(ignore_mandatory=True)
			except frappe.DuplicateEntryError:
				# Another process created this address, or naming collision
				# Try multiple strategies to find the existing address
				address_title = address.get("address_title")
				address_type = address.get("address_type")
				existing_address = None
				
				# Strategy 1: Try exact address_title
				if address_title and frappe.db.exists("Address", address_title):
					existing_address = frappe.get_doc("Address", address_title)
				
				# Strategy 2: Try with double suffix (naming collision case)
				# e.g., "Name-Shipping-Shipping" when "Name-Shipping" was the customer name
				if not existing_address and address_title and address_type:
					double_suffix_name = f"{address_title}-{address_type}"
					if frappe.db.exists("Address", double_suffix_name):
						existing_address = frappe.get_doc("Address", double_suffix_name)
				
				# Strategy 3: Find by customer link and address type
				if not existing_address:
					linked_addresses = frappe.db.sql("""
						SELECT a.name 
						FROM `tabAddress` a
						INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name
						WHERE dl.link_doctype = 'Customer' 
						AND dl.link_name = %s
						AND a.address_type = %s
						ORDER BY a.modified DESC
						LIMIT 1
					""", (customer_doc.name, address_type), as_dict=True)
					
					if linked_addresses:
						existing_address = frappe.get_doc("Address", linked_addresses[0].name)
				
				# Strategy 4: Find any address with similar title pattern
				if not existing_address and address_title:
					# Search for addresses starting with the base customer name
					base_name = address_title.rsplit("-", 1)[0] if "-" in address_title else address_title
					similar_addresses = frappe.db.get_all(
						"Address",
						filters={"name": ["like", f"{base_name}%"], "address_type": address_type},
						order_by="modified desc",
						limit=1
					)
					if similar_addresses:
						existing_address = frappe.get_doc("Address", similar_addresses[0].name)
				
				if existing_address:
					# Ensure customer link exists
					customer_linked = False
					for link in existing_address.links:
						if link.link_doctype == "Customer" and link.link_name == customer_doc.name:
							customer_linked = True
							break
					
					if not customer_linked:
						existing_address.append("links", {
							"link_doctype": "Customer",
							"link_name": customer_doc.name
						})
						existing_address.save(ignore_permissions=True)
				else:
					# Log the issue but don't fail the entire order sync
					frappe.log_error(
						message=f"Could not find or create address for customer {customer_doc.name}. "
								f"Attempted title: {address_title}, type: {address_type}",
						title="Address Creation - Unable to Resolve Duplicate"
					)

	def create_customer_contact(self, contact: dict[str, str]) -> None:
		"""Create contact from dictionary containing fields used in Address doctype of ERPNext."""

		customer_doc = self.get_customer_doc()

		frappe.get_doc(
			{
				"doctype": "Contact",
				**contact,
				"links": [{"link_doctype": "Customer", "link_name": customer_doc.name}],
			}
		).insert(ignore_mandatory=True)
