# Copyright (c) 2025, Frappe and contributors
# For license information, please see LICENSE

"""
Per-store rate limiter for Shopify API calls.
Implements token bucket algorithm to respect Shopify's rate limits:
- REST API: 2 requests/second, burst of 40
- GraphQL API: Cost-based, 1000 points per 10 seconds
"""

import time
from typing import Literal

import frappe


class ShopifyRateLimiter:
	"""Token bucket rate limiter for Shopify API calls."""

	def __init__(self, store_name: str, api_type: Literal["rest", "graphql"] = "rest"):
		"""
		Initialize rate limiter for a specific store and API type.
		
		Args:
		    store_name: Name of the Shopify Store doctype
		    api_type: "rest" for REST API (2/sec, burst 40) or "graphql" for GraphQL (cost-based)
		"""
		self.store_name = store_name
		self.api_type = api_type
		self.cache_key = f"shopify_rate_limit:{store_name}:{api_type}"
		
		# Rate limit parameters
		if api_type == "rest":
			self.rate = 2  # requests per second
			self.capacity = 40  # max burst
		else:  # graphql
			self.rate = 100  # points per second (1000 points per 10 sec)
			self.capacity = 1000  # max cost

	def wait_if_needed(self, cost: int = 1) -> None:
		"""
		Block until rate limit allows the request.
		Implements token bucket algorithm.
		
		Args:
		    cost: Cost of the operation (1 for REST, calculated for GraphQL)
		"""
		while not self._can_proceed(cost):
			# Sleep for a short interval and retry
			time.sleep(0.1)
		
		self.record_request(cost)

	def _can_proceed(self, cost: int) -> bool:
		"""Check if we have enough tokens for this request."""
		bucket = self._get_bucket()
		return bucket["tokens"] >= cost

	def record_request(self, cost: int = 1) -> None:
		"""Record an API call and update the token bucket."""
		bucket = self._get_bucket()
		
		# Refill tokens based on elapsed time
		now = time.time()
		elapsed = now - bucket["last_refill"]
		tokens_to_add = elapsed * self.rate
		
		bucket["tokens"] = min(self.capacity, bucket["tokens"] + tokens_to_add)
		bucket["last_refill"] = now
		
		# Consume tokens for this request
		bucket["tokens"] -= cost
		
		self._save_bucket(bucket)

	def _get_bucket(self) -> dict:
		"""Get current token bucket state from cache."""
		bucket = frappe.cache().get_value(self.cache_key)
		
		if not bucket:
			# Initialize bucket with full capacity
			bucket = {
				"tokens": self.capacity,
				"last_refill": time.time(),
			}
		
		return bucket

	def _save_bucket(self, bucket: dict) -> None:
		"""Save token bucket state to cache."""
		# Cache for 1 hour (tokens will refill naturally)
		frappe.cache().set_value(self.cache_key, bucket, expires_in_sec=3600)

	def get_available_tokens(self) -> float:
		"""Get current number of available tokens."""
		bucket = self._get_bucket()
		return bucket["tokens"]

	def reset(self) -> None:
		"""Reset the rate limiter (useful for testing)."""
		frappe.cache().delete_value(self.cache_key)


def get_rate_limiter(store_name: str, api_type: Literal["rest", "graphql"] = "rest") -> ShopifyRateLimiter:
	"""
	Factory function to get a rate limiter for a store.
	
	Args:
	    store_name: Name of the Shopify Store
	    api_type: API type ("rest" or "graphql")
	    
	Returns:
	    ShopifyRateLimiter instance
	"""
	return ShopifyRateLimiter(store_name, api_type)

