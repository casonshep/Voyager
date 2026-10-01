"""TypeSafe (Jev) integration: structured state, a guarded client, and judgment gates.

Every gate degrades to a no-op when the client is disabled, so Voyager runs
unchanged without a ``TYPESAFE_API_KEY``.
"""

from .state import build_state
from .client import JevClient
from .gates import critic_shadow, qa_rank, task_fanout

__all__ = ["build_state", "JevClient", "critic_shadow", "qa_rank", "task_fanout"]
