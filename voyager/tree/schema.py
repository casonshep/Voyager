"""Tree data model: nodes, verify specs, and the tree container (PLAN.md section 1).

Everything is plain JSON underneath so Node and Python share one shape. The
field names in ``Node`` are the frozen contract from section 1.1; ``failed``
is the one addition (section 8 marks nodes failed), defaulting to False.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator

NODE_KINDS = ("craft", "mine", "smelt", "kill", "place", "locate", "collect", "goal")
WORK_KINDS = ("craft", "smelt", "place", "mine", "kill", "collect", "locate", "none")


# --------------------------------------------------------------------------- #
# VerifySpec
# --------------------------------------------------------------------------- #
@dataclass
class VerifySpec:
    """A JSON expression over catalog predicates (section 1.2).

    ``spec`` is exactly the dict sent to ``POST /agent/verify``:
    ``{"has_item": {"name": "stick", "count": 2}}`` or a combinator
    ``{"all": [...]}``, ``{"any": [...]}``, ``{"not": {...}}``.
    """

    spec: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.spec))

    @classmethod
    def from_json(cls, data: dict[str, Any] | "VerifySpec") -> "VerifySpec":
        if isinstance(data, VerifySpec):
            return cls(data.to_json())
        return cls(dict(data))

    @property
    def op(self) -> str:
        """The single top-level key: a predicate name or a combinator."""
        keys = list(self.spec.keys())
        if len(keys) != 1:
            raise ValueError(f"a verify spec has exactly one top-level key, got {keys}")
        return keys[0]

    def predicates(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield (predicate_name, params) for every leaf predicate in the expression."""
        yield from _walk(self.spec)

    def items_mentioned(self) -> set[str]:
        """Item or block names referenced anywhere in the spec."""
        out: set[str] = set()
        for _, params in self.predicates():
            for key in ("name", "tool"):
                if isinstance(params.get(key), str):
                    out.add(params[key])
        return out

    @staticmethod
    def all_of(*specs: "VerifySpec | dict") -> "VerifySpec":
        flat = [s.to_json() if isinstance(s, VerifySpec) else s for s in specs]
        return VerifySpec({"all": flat}) if len(flat) != 1 else VerifySpec.from_json(flat[0])

    @staticmethod
    def any_of(*specs: "VerifySpec | dict") -> "VerifySpec":
        flat = [s.to_json() if isinstance(s, VerifySpec) else s for s in specs]
        return VerifySpec({"any": flat}) if len(flat) != 1 else VerifySpec.from_json(flat[0])

    def negate(self) -> "VerifySpec":
        return VerifySpec({"not": self.to_json()})


def _walk(spec: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    if not isinstance(spec, dict) or len(spec) != 1:
        return
    op, body = next(iter(spec.items()))
    if op in ("all", "any"):
        for child in body or []:
            yield from _walk(child)
    elif op == "not":
        yield from _walk(body)
    else:
        yield op, (body if isinstance(body, dict) else {})


# --------------------------------------------------------------------------- #
# Node
# --------------------------------------------------------------------------- #
@dataclass
class Node:
    id: str
    description: str
    kind: str
    verify: dict[str, Any]
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    requires: dict[str, int] = field(default_factory=dict)
    acquire: int | None = None
    work: dict[str, Any] = field(default_factory=lambda: {"kind": "none"})
    done: bool = False
    done_at: float | None = None
    attempts: int = 0
    actions_spent: int = 0
    budget_actions: int = 60
    trace: list[dict[str, Any]] = field(default_factory=list)
    failed: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Node":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def item(self) -> str | None:
        """The item this node produces or gathers, if any."""
        return self.work.get("item") if isinstance(self.work, dict) else None

    @property
    def count(self) -> int:
        if self.acquire is not None:
            return int(self.acquire)
        return int(self.work.get("count", 1)) if isinstance(self.work, dict) else 1

    def verify_spec(self) -> VerifySpec:
        return VerifySpec.from_json(self.verify)

    def mark_done(self, trace: list[dict[str, Any]] | None = None) -> None:
        self.done = True
        self.done_at = time.time()
        if trace is not None:
            self.trace = list(trace)


# --------------------------------------------------------------------------- #
# Tree
# --------------------------------------------------------------------------- #
def goal_id_for(goal_text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", goal_text.lower()).strip("_")[:40] or "goal"
    return f"{slug}_{time.strftime('%Y%m%d_%H%M%S')}"


@dataclass
class Tree:
    goal_id: str
    goal_text: str
    root: str | None = None
    nodes: dict[str, Node] = field(default_factory=dict)
    target: dict[str, Any] | None = None  # the parsed root target ({"item", "count"} or a non-item target)
    created_at: float = field(default_factory=time.time)
    _counter: int = 0

    # ---- construction ---------------------------------------------------- #
    def new_id(self) -> str:
        self._counter += 1
        return f"n{self._counter}"

    def add(self, node: Node, parent: str | None = None) -> Node:
        if node.id in self.nodes:
            raise ValueError(f"duplicate node id {node.id}")
        node.parent = parent
        self.nodes[node.id] = node
        if parent is None:
            if self.root is None:
                self.root = node.id
        else:
            self.nodes[parent].children.append(node.id)
        return node

    def remove(self, node_id: str) -> None:
        """Remove a node and its whole subtree."""
        node = self.nodes.get(node_id)
        if node is None:
            return
        for child in list(node.children):
            self.remove(child)
        if node.parent and node.parent in self.nodes:
            siblings = self.nodes[node.parent].children
            if node_id in siblings:
                siblings.remove(node_id)
        del self.nodes[node_id]
        if self.root == node_id:
            self.root = None

    def get(self, node_id: str) -> Node:
        return self.nodes[node_id]

    # ---- queries --------------------------------------------------------- #
    def post_order(self, start: str | None = None) -> list[Node]:
        """Children in order, then the node: the traversal order (section 5.1)."""
        out: list[Node] = []

        def visit(nid: str) -> None:
            node = self.nodes[nid]
            for child in node.children:
                visit(child)
            out.append(node)

        root = start or self.root
        if root is not None:
            visit(root)
        return out

    def leaves(self) -> list[Node]:
        return [n for n in self.post_order() if n.is_leaf]

    def pending(self) -> list[Node]:
        """Nodes still to run, in traversal order."""
        return [n for n in self.post_order() if not n.done and not n.failed]

    def next_pending(self) -> Node | None:
        """The next node to run: post-order puts every child before its parent."""
        for node in self.post_order():
            if not node.done and not node.failed:
                return node
        return None

    def items(self) -> set[str]:
        """Every item name the tree mentions: produced, gathered, required, or verified."""
        out: set[str] = set()
        for node in self.nodes.values():
            if node.item:
                out.add(node.item)
            out.update(node.requires.keys())
            out.update(node.verify_spec().items_mentioned())
        return out

    def path_to_root(self, node_id: str) -> list[Node]:
        out: list[Node] = []
        cur: str | None = node_id
        while cur is not None:
            node = self.nodes[cur]
            out.append(node)
            cur = node.parent
        return out

    def allowed_blocks(self, knowledge: dict[str, Any]) -> list[str]:
        """Block types the snapshot should show for this tree (section 1.3 caps).

        Leaf mine targets are mapped through the inverse of ``ore_drops`` and the
        ``families`` table; any family member collapses to the family key
        ``*_<family>``. Landmarks the tree verifies against (crafting table,
        furnace) are always included so ``near_block`` has something to see.
        """
        drops_inverse: dict[str, list[str]] = {}
        for block, drop in (knowledge.get("ore_drops") or {}).items():
            drops_inverse.setdefault(drop, []).append(block)
        families: dict[str, list[str]] = knowledge.get("families") or {}
        natural = re.compile(knowledge.get("natural_block_re") or r"$^")

        out: set[str] = set()

        def add_block(name: str) -> None:
            for fam, members in families.items():
                if name in members and fam in ("log", "planks"):
                    out.add(f"*_{fam}")
                    return
            out.add(name)

        for node in self.nodes.values():
            work = node.work or {}
            if work.get("kind") == "mine":
                for block in work.get("blocks") or ([work.get("block")] if work.get("block") else []):
                    add_block(block)
                item = work.get("item")
                if item:
                    dropped_by = drops_inverse.get(item, [])
                    for block in dropped_by:
                        add_block(block)
                    if not dropped_by and natural.search(item):
                        add_block(item)
            for pred, params in node.verify_spec().predicates():
                if pred in ("near_block", "block_placed_by_me", "block_count_in_region") and params.get("name"):
                    out.add(params["name"])
        return sorted(out)

    # ---- persistence ----------------------------------------------------- #
    def to_json(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "goal_text": self.goal_text,
            "root": self.root,
            "target": self.target,
            "created_at": self.created_at,
            "counter": self._counter,
            "nodes": {nid: n.to_json() for nid, n in self.nodes.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Tree":
        tree = cls(
            goal_id=data["goal_id"],
            goal_text=data.get("goal_text", ""),
            root=data.get("root"),
            target=data.get("target"),
            created_at=data.get("created_at", time.time()),
        )
        tree._counter = int(data.get("counter", 0))
        for nid, nd in (data.get("nodes") or {}).items():
            tree.nodes[nid] = Node.from_json(nd)
        if tree._counter == 0 and tree.nodes:
            tree._counter = max(int(re.sub(r"\D", "", nid) or 0) for nid in tree.nodes)
        return tree

    def path_in(self, ckpt_dir: str) -> str:
        return os.path.join(ckpt_dir, "tree", f"{self.goal_id}.json")

    def save(self, ckpt_dir: str) -> str:
        path = self.path_in(ckpt_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, indent=1)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str) -> "Tree":
        with open(path, encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))

    @classmethod
    def latest(cls, ckpt_dir: str) -> "Tree | None":
        folder = os.path.join(ckpt_dir, "tree")
        if not os.path.isdir(folder):
            return None
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".json")]
        if not files:
            return None
        return cls.load(max(files, key=os.path.getmtime))

    # ---- debugging ------------------------------------------------------- #
    def render(self, verify_fn: Callable[[Node], str] | None = None) -> str:
        lines: list[str] = []

        def visit(nid: str, depth: int) -> None:
            n = self.nodes[nid]
            flag = "x" if n.done else ("!" if n.failed else " ")
            acq = f" acquire={n.acquire}" if n.acquire is not None else ""
            extra = f" {verify_fn(n)}" if verify_fn else ""
            lines.append(f"{'  ' * depth}[{flag}] {n.id} {n.kind}: {n.description}{acq}{extra}")
            for c in n.children:
                visit(c, depth + 1)

        if self.root:
            visit(self.root, 0)
        return "\n".join(lines)
