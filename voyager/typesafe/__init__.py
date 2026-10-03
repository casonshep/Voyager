"""TypeSafe (Jev) integration: a guarded client with jsonl audit logs.

``JevClient.ask`` returns ``None`` when the SDK or ``TYPESAFE_API_KEY`` is
unavailable or a request fails, so every caller degrades to plain code.
"""

from .client import JevClient

__all__ = ["JevClient"]
