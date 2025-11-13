"""Document locking utilities to prevent concurrent modifications"""
import frappe
import time
from contextlib import contextmanager

@contextmanager
def document_lock(doctype, name, timeout=30):
    """Context manager to lock a document during updates.
    
    Usage:
        with document_lock("Customer", customer_name):
            # Update customer safely
            customer = frappe.get_doc("Customer", customer_name)
            customer.some_field = value
            customer.save()
    """
    lock_key = f"document_lock:{doctype}:{name}"
    lock_acquired = False
    start_time = time.time()
    
    try:
        # Try to acquire lock with timeout
        while time.time() - start_time < timeout:
            # Check if lock exists
            existing_lock = frappe.cache().get(lock_key)
            if not existing_lock:
                # Try to set lock atomically
                frappe.cache().setex(lock_key, timeout, frappe.session.user or "System")
                # Verify we got the lock (in case of race condition)
                if frappe.cache().get(lock_key) == (frappe.session.user or "System"):
                    lock_acquired = True
                    break
            # Wait a bit before retrying
            time.sleep(0.1)
        
        if not lock_acquired:
            frappe.throw(f"Could not acquire lock for {doctype} {name} after {timeout} seconds")
        
        yield
        
    finally:
        # Release lock if we acquired it
        if lock_acquired:
            frappe.cache().delete(lock_key)


def safe_document_update(doctype, name, updates, ignore_permissions=True):
    """Safely update a document with proper locking and error handling.
    
    Args:
        doctype: Document type
        name: Document name
        updates: Dict of field updates or callable that takes doc and modifies it
        ignore_permissions: Whether to ignore permissions
    
    Returns:
        Updated document or None if update failed
    """
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            with document_lock(doctype, name):
                # Get fresh copy of document
                doc = frappe.get_doc(doctype, name)
                
                # Apply updates
                if callable(updates):
                    updates(doc)
                else:
                    for field, value in updates.items():
                        setattr(doc, field, value)
                
                # Save with version check
                doc.flags.ignore_version = False
                doc.save(ignore_permissions=ignore_permissions)
                
                return doc
                
        except frappe.TimestampMismatchError:
            # Document was modified, retry
            retry_count += 1
            if retry_count >= max_retries:
                frappe.log_error(
                    message=f"Failed to update {doctype} {name} after {max_retries} retries due to concurrent modifications",
                    title="Document Update Conflict"
                )
                raise
            time.sleep(0.5 * retry_count)  # Exponential backoff
            
        except Exception as e:
            frappe.log_error(
                message=f"Error updating {doctype} {name}: {str(e)}",
                title="Document Update Error"
            )
            raise
    
    return None
