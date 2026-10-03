"""Goal tree: decomposition into verifiable subtasks, traversal, and the action builder.

Data side (Package B): ``schema``, ``catalog``, ``decomposer``, ``llm``.
Runtime side (Package C): ``traversal``, ``builder`` (imported when present).
"""

from .schema import Node, Tree, VerifySpec, goal_id_for
from .catalog import PREDICATES, validate, is_valid, normalize, evaluate, describe_catalog
from .decomposer import Decomposer, DecomposeError, Knowledge, verify_fn_from_facts
from .llm import LLM, LLMError

__all__ = [
    "Node", "Tree", "VerifySpec", "goal_id_for",
    "PREDICATES", "validate", "is_valid", "normalize", "evaluate", "describe_catalog",
    "Decomposer", "DecomposeError", "Knowledge", "verify_fn_from_facts",
    "LLM", "LLMError",
]

try:  # Package C
    from .traversal import Traversal  # noqa: F401

    __all__.append("Traversal")
except Exception:  # pragma: no cover - not yet present
    pass

try:  # Package C
    from .builder import ActionBuilder  # noqa: F401

    __all__.append("ActionBuilder")
except Exception:  # pragma: no cover - not yet present
    pass
