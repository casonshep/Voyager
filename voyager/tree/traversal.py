"""Traversal: run a goal tree node by node through the bot's agent loop.

PLAN.md sections 5 and 8. Post-order depth-first: a node runs only after all
its children are done. Each node is posted to Node as one subtask
(``POST /agent/subtask``); Python then polls ``GET /agent/status`` and acts
on the drained triggers:

    subtask_done      -> persist ``done`` and the action ``trace`` on the node
    builder_wanted    -> ask the ActionBuilder for a new primitive
    stuck             -> if Jev thought the menu could complete it: retry the
                         same subtask with half the budget; otherwise build
    budget_exhausted  -> once: retry; twice: re-decompose this node only
    danger            -> logged; the loop handles it itself

A node whose re-decomposition fails is marked failed along with its whole
parent chain, and ``run`` returns so the caller can ask for a new goal.

The tree object is duck-typed against PLAN.md 1.1 / Package B: nodes expose
``id, parent, children, description, kind, verify, requires, acquire, work,
done, done_at, attempts, actions_spent, budget_actions, trace``; the tree
exposes ``post_order()``, ``to_json()`` and (optionally) ``items()`` and
``allowed_blocks(knowledge)``. Nodes may be objects or plain dicts.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

DEFAULT_BUDGET = 60
MIN_BUDGET = 5
HAZARD_BLOCKS = ["lava", "water", "fire", "cactus"]


# ---------------------------------------------------------------------- #
# node / tree access helpers (objects or dicts)
# ---------------------------------------------------------------------- #

def _g(node: Any, key: str, default: Any = None) -> Any:
    if isinstance(node, dict):
        return node.get(key, default)
    return getattr(node, key, default)


def _s(node: Any, key: str, value: Any) -> None:
    if isinstance(node, dict):
        node[key] = value
    else:
        try:
            setattr(node, key, value)
        except Exception:
            pass


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:48] or "goal"


class BotDisconnected(RuntimeError):
    """The bot lost its connection to the server; the tree is saved and can be resumed."""


class Traversal:
    def __init__(
        self,
        tree,
        bridge,
        jev,
        builder,
        *,
        knowledge: dict[str, Any] | None = None,
        ckpt_dir: str = "ckpt",
        poll_seconds: float = 1.0,
        node_seconds_limit: float = 900.0,
        max_retries_per_node: int = 3,
        exhaustions_before_redecompose: int = 2,
        completable_threshold: float = 0.5,
        redecompose: Callable[[Any, str], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.tree = tree
        self.bridge = bridge
        self.jev = jev
        self.builder = builder
        self.knowledge = knowledge or {}
        self.ckpt_dir = ckpt_dir
        self.poll_seconds = poll_seconds
        self.node_seconds_limit = node_seconds_limit
        self.max_retries_per_node = max_retries_per_node
        self.exhaustions_before_redecompose = exhaustions_before_redecompose
        self.completable_threshold = completable_threshold
        self._redecompose = redecompose
        self._sleep = sleep
        self._now = now
        self._on_event = on_event
        self.failed: set[str] = set()
        self.exhaustions: dict[str, int] = {}
        self.retries: dict[str, int] = {}
        self.events: list[dict[str, Any]] = []
        self.goal_id = _g(tree, "goal_id") or _g(tree, "id") or _slug(_g(tree, "goal", "goal"))
        os.makedirs(os.path.join(ckpt_dir, "tree"), exist_ok=True)
        self._load_run_state()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #

    @property
    def tree_path(self) -> str:
        return os.path.join(self.ckpt_dir, "tree", f"{self.goal_id}.json")

    @property
    def run_path(self) -> str:
        return os.path.join(self.ckpt_dir, "tree", f"{self.goal_id}.run.json")

    def save(self) -> None:
        try:
            data = self.tree.to_json() if hasattr(self.tree, "to_json") else self.tree
            if not isinstance(data, str):
                data = json.dumps(data, indent=2, default=str)
            with open(self.tree_path, "w", encoding="utf-8") as fh:
                fh.write(data)
            with open(self.run_path, "w", encoding="utf-8") as fh:
                json.dump({"failed": sorted(self.failed), "exhaustions": self.exhaustions, "retries": self.retries}, fh, indent=2)
        except Exception as exc:
            self._event("save_failed", {"error": str(exc)})

    def _load_run_state(self) -> None:
        if not os.path.exists(self.run_path):
            return
        try:
            with open(self.run_path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.failed = set(data.get("failed") or [])
            self.exhaustions = dict(data.get("exhaustions") or {})
            self.retries = dict(data.get("retries") or {})
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # tree queries
    # ------------------------------------------------------------------ #

    def nodes(self) -> list[Any]:
        return list(self.tree.post_order())

    def index(self) -> dict[str, Any]:
        return {_g(n, "id"): n for n in self.nodes()}

    def root(self) -> Any | None:
        for n in self.nodes():
            if not _g(n, "parent"):
                return n
        ns = self.nodes()
        return ns[-1] if ns else None

    def next_node(self) -> Any | None:
        """First node in post-order that is neither done nor failed."""
        for n in self.nodes():
            if _g(n, "done") or _g(n, "id") in self.failed:
                continue
            return n
        return None

    def tree_items(self) -> list[str]:
        if hasattr(self.tree, "items"):
            try:
                items = list(self.tree.items())
                if items and all(isinstance(i, str) for i in items):
                    return items
            except Exception:
                pass
        out: list[str] = []
        for n in self.nodes():
            work = _g(n, "work") or {}
            item = work.get("item") if isinstance(work, dict) else _g(work, "item")
            if item and item not in out:
                out.append(item)
            for k in (_g(n, "requires") or {}).keys():
                if k not in out:
                    out.append(k)
        return out

    def allowed_blocks(self) -> list[str]:
        if hasattr(self.tree, "allowed_blocks"):
            try:
                blocks = list(self.tree.allowed_blocks(self.knowledge))
                return sorted(set(blocks) | set(HAZARD_BLOCKS))
            except Exception:
                pass
        ore_drops: dict[str, str] = self.knowledge.get("ore_drops") or {}
        families: dict[str, list[str]] = self.knowledge.get("families") or {}
        drop_to_blocks: dict[str, list[str]] = {}
        for block, drop in ore_drops.items():
            drop_to_blocks.setdefault(drop, []).append(block)
        out: set[str] = set(HAZARD_BLOCKS)
        for item in self.tree_items():
            out.add(item)
            for b in drop_to_blocks.get(item, []):
                out.add(b)
            for fam, members in families.items():
                if item in members:
                    out.add(f"*_{fam}")
        return sorted(out)

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #

    def run(self) -> dict[str, Any]:
        """Run until the root is done or a node fails. Returns a summary dict."""
        done_count = 0
        while True:
            node = self.next_node()
            if node is None:
                break
            outcome = self.run_node(node)
            if outcome == "done":
                done_count += 1
            elif outcome == "failed":
                break
            # "redecomposed" falls through: the tree changed, pick the next node again
        root = self.root()
        status = "done" if root is not None and _g(root, "done") else "failed"
        try:
            self.bridge.agent_stop()
        except Exception:
            pass
        summary = {"status": status, "goal_id": self.goal_id, "nodes_done": done_count, "failed": sorted(self.failed)}
        self._event("traversal_finished", summary)
        return summary

    def _subtask_payload(self, node: Any, budget: int) -> dict[str, Any]:
        return {
            "id": _g(node, "id"),
            "description": _g(node, "description"),
            "verify": _g(node, "verify"),
            "allowed_blocks": self.allowed_blocks(),
            "budget_actions": int(budget),
            "kind": _g(node, "kind"),
            "work": dict(_g(node, "work") or {}),
            "requires": dict(_g(node, "requires") or {}),
            "tree_items": self.tree_items(),
        }

    def run_node(self, node: Any) -> str:
        """Returns 'done', 'failed' or 'redecomposed'."""
        node_id = _g(node, "id")
        # already satisfied by the world (a resumed run, or a sibling's side effect)
        if self._verified(node):
            self._mark_done(node, trace=[])
            return "done"

        budget = int(_g(node, "budget_actions") or DEFAULT_BUDGET)
        payload = self._subtask_payload(node, budget)
        self._post(node, payload)
        started = self._now()

        failed_polls = 0
        while True:
            self._sleep(self.poll_seconds)
            try:
                status = self.bridge.agent_status()
                failed_polls = 0
            except Exception as exc:
                self._event("status_failed", {"node": node_id, "error": str(exc)})
                failed_polls += 1
                if failed_polls >= 10:
                    raise BotDisconnected(f"status unreachable: {exc}")
                continue
            if status.get("disconnected"):
                self.save()
                raise BotDisconnected(status.get("reason") or "bot disconnected")
            if self.builder is not None and hasattr(self.builder, "record_outcomes"):
                self.builder.record_outcomes(status)
            sub = status.get("subtask") or {}
            if sub.get("id") == node_id and sub.get("actions_spent") is not None:
                _s(node, "actions_spent", sub["actions_spent"])

            triggers = list(status.get("triggers") or [])
            if self._now() - started > self.node_seconds_limit:
                triggers.append({"type": "budget_exhausted", "subtask_id": node_id, "actions_spent": _g(node, "actions_spent"), "wall_clock": True})
                started = self._now()

            for trig in triggers:
                ttype = trig.get("type")
                if trig.get("subtask_id") not in (None, node_id):
                    continue  # stale trigger from a previous subtask

                if ttype == "subtask_done":
                    self._mark_done(node, trace=trig.get("trace") or [])
                    return "done"

                if ttype == "builder_wanted":
                    reason = f"Jev judged the subtask not completable with the current menu (completable={trig.get('completable')})"
                    stale = trig.get("stale") or []
                    if stale:
                        # The previous build's menu() condition never matched the world
                        # (for example it only fired when a specific block was at the
                        # cursor). Retire it and tell GPT why.
                        reason += ". " + self.builder.note_stale(stale, node_id)
                    self._build(node, payload, status, reason=reason)

                elif ttype == "stuck":
                    completable = (status.get("last_decision") or {}).get("completable")
                    if completable is not None and completable < self.completable_threshold:
                        self._build(node, payload, status, reason=f"stuck ({trig.get('kind')}) and Jev judged the menu insufficient (completable={completable})")
                    else:
                        retries = self.retries.get(node_id, 0) + 1
                        self.retries[node_id] = retries
                        if retries > self.max_retries_per_node:
                            self._event("retries_exhausted", {"node": node_id})
                            # start counting again: otherwise every later stuck trigger
                            # re-enters the exhaustion handler (and forces a build) at once
                            self.retries[node_id] = 0
                            outcome = self._exhausted(node, payload, status)
                            if outcome:
                                return outcome
                            budget = payload["budget_actions"]
                            payload = self._subtask_payload(node, budget)
                            self._post(node, payload)
                            started = self._now()
                        else:
                            budget = max(MIN_BUDGET, payload["budget_actions"] // 2)
                            payload = self._subtask_payload(node, budget)
                            _s(node, "attempts", int(_g(node, "attempts") or 0) + 1)
                            self._event("retry", {"node": node_id, "kind": trig.get("kind"), "budget": budget, "retry": retries})
                            self._post(node, payload)
                            started = self._now()
                        self.save()

                elif ttype == "budget_exhausted":
                    outcome = self._exhausted(node, payload, status)
                    if outcome:
                        return outcome
                    payload = self._subtask_payload(node, int(_g(node, "budget_actions") or DEFAULT_BUDGET))
                    self._post(node, payload)
                    started = self._now()

                elif ttype == "danger":
                    self._event("danger", {"node": node_id, **{k: v for k, v in trig.items() if k != "type"}})

    # ------------------------------------------------------------------ #
    # routes
    # ------------------------------------------------------------------ #

    def _post(self, node: Any, payload: dict[str, Any]) -> None:
        self._event("subtask_posted", {"node": payload["id"], "description": payload["description"], "budget": payload["budget_actions"]})
        self.bridge.agent_subtask(payload)

    def _verified(self, node: Any) -> bool:
        spec = _g(node, "verify")
        if not spec:
            return False
        try:
            res = self.bridge.agent_verify(spec)
        except Exception as exc:
            self._event("verify_failed", {"node": _g(node, "id"), "error": str(exc)})
            return False
        return bool(res.get("ok"))

    def _mark_done(self, node: Any, *, trace: list) -> None:
        _s(node, "done", True)
        _s(node, "done_at", self._now())
        _s(node, "trace", list(trace))
        self.save()
        self._event("node_done", {"node": _g(node, "id"), "description": _g(node, "description"), "actions": len(trace)})

    def _build(self, node: Any, payload: dict[str, Any], status: dict[str, Any], *, reason: str, force: bool = False) -> str | None:
        if self.builder is None:
            self._event("builder_missing", {"node": _g(node, "id")})
            return None
        try:
            name = self.builder.build(payload, status, self.tree_items(), reason, force=force)
        except TypeError:  # a builder stub without the keyword
            name = self.builder.build(payload, status, self.tree_items(), reason)
        if name is not None or force:
            self._event("build", {"node": _g(node, "id"), "name": name, "reason": reason})
        if name is None:
            why = getattr(self.builder, "last_refusal", None) or "the builder produced no action (GPT drafts all rejected)"
            self._event("build_refused", {"node": _g(node, "id"), "why": why, "trigger": reason[:120]})
        return name

    def _inputs_in_hand(self, node: Any, status: dict[str, Any]) -> bool:
        """True when every item in the node's `requires` is already in the inventory."""
        requires = _g(node, "requires") or {}
        if not requires:
            return False
        inventory = ((status or {}).get("snap") or {}).get("inventory") or {}
        try:
            from voyager.tree.catalog import count_matching
        except Exception:
            count_matching = lambda inv, name: int(inv.get(name, 0))  # noqa: E731
        return all(count_matching(inventory, name) >= int(n) for name, n in requires.items())

    def _exhausted(self, node: Any, payload: dict[str, Any], status: dict[str, Any] | None = None) -> str | None:
        """Second exhaustion re-decomposes; a failed re-decomposition fails the chain.

        Returns 'redecomposed' or 'failed' when the caller must stop running this
        node, or None when the node should simply be posted again.
        """
        node_id = _g(node, "id")
        n = self.exhaustions.get(node_id, 0) + 1
        self.exhaustions[node_id] = n
        self._event("budget_exhausted", {"node": node_id, "count": n})
        self.save()
        if n < self.exhaustions_before_redecompose:
            return None
        # A craft/smelt/place step whose inputs are all in the bag has nothing to
        # gain from another recipe: the missing piece is the action itself.
        if (_g(node, "kind") in ("craft", "smelt", "place")) and self._inputs_in_hand(node, status or {}):
            self._event("inputs_in_hand", {"node": node_id, "requires": _g(node, "requires")})
            self._build(
                node, payload, status or {}, force=True,
                reason=(f"every input for '{_g(node, 'description')}' is already in the inventory "
                        f"({_g(node, 'requires')}); the bot only lacks an action that performs the step itself"),
            )
            self.exhaustions[node_id] = 1  # another full budget before re-decomposing is considered again
            return None
        if self._try_redecompose(node_id):
            self.exhaustions[node_id] = 0
            self.retries[node_id] = 0
            self.save()
            self._event("redecomposed", {"node": node_id})
            return "redecomposed"
        self._fail_chain(node)
        return "failed"

    def _try_redecompose(self, node_id: str) -> bool:
        fn = self._redecompose
        if fn is None:
            # Package B exposes redecompose as a Decomposer method; build one over the knowledge we hold.
            try:
                from voyager.tree.decomposer import Decomposer

                fn = Decomposer(self.knowledge, self.jev, None).redecompose
            except Exception as exc:
                self._event("redecompose_unavailable", {"error": str(exc)})
                return False
        try:
            result = fn(self.tree, node_id)
        except Exception as exc:
            self._event("redecompose_failed", {"node": node_id, "error": str(exc)})
            return False
        if result is None or result is False:
            return False
        if hasattr(result, "post_order"):
            self.tree = result
        return True

    def _fail_chain(self, node: Any) -> None:
        idx = self.index()
        cur = node
        while cur is not None:
            cid = _g(cur, "id")
            self.failed.add(cid)
            _s(cur, "failed", True)
            self._event("node_failed", {"node": cid, "description": _g(cur, "description")})
            parent = _g(cur, "parent")
            cur = idx.get(parent) if parent else None
        self.save()

    # ------------------------------------------------------------------ #

    def _event(self, kind: str, detail: dict[str, Any]) -> None:
        rec = {"ts": self._now(), "type": kind, **detail}
        self.events.append(rec)
        print(f"\033[35m[traversal] {kind} {json.dumps(detail, default=str)[:300]}\033[0m")
        if self._on_event:
            try:
                self._on_event(kind, detail)
            except Exception:
                pass
        if self.jev is not None and hasattr(self.jev, "log_only"):
            try:
                self.jev.log_only("traversal", rec)
            except Exception:
                pass
