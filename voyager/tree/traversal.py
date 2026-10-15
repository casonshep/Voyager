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
from concurrent.futures import ThreadPoolExecutor
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
        max_forced_build_failures: int = 2,
        max_grace_rounds: int = 2,
        completable_threshold: float = 0.5,
        redecompose: Callable[[Any, str], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        async_builds: bool = False,
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
        self.max_forced_build_failures = max_forced_build_failures
        # A node is not given up while help for it is on its way: a build for it still
        # running, or an action built for it that has not been tried yet. Each such
        # exhaustion buys one more round (with the full budget), up to this many.
        self.max_grace_rounds = max_grace_rounds
        self.completable_threshold = completable_threshold
        self._redecompose = redecompose
        self._sleep = sleep
        self._now = now
        self._on_event = on_event
        # Builds (one GPT conversation, up to a minute) run on one worker thread so the
        # poll loop keeps reading triggers while the bot keeps acting. One slot: a build
        # request while one is running is skipped, not queued. Forced builds stay
        # synchronous because their result decides what happens to the node next.
        self.async_builds = async_builds
        self._executor: ThreadPoolExecutor | None = None
        self._pending: tuple[Any, str, str] | None = None  # (future, node id, reason)
        self.failed: set[str] = set()
        self.exhaustions: dict[str, int] = {}
        self.retries: dict[str, int] = {}
        self.forced_failures: dict[str, int] = {}  # node id -> forced builds in a row that produced nothing
        self.grace: dict[str, int] = {}  # node id -> extra rounds given while a build or an untested action was pending
        self.events: list[dict[str, Any]] = []
        self.goal_id = _g(tree, "goal_id") or _g(tree, "id") or _slug(_g(tree, "goal", "goal"))
        os.makedirs(os.path.join(ckpt_dir, "tree"), exist_ok=True)
        self._load_run_state()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #

    @property
    def decision_log(self) -> str:
        return os.path.abspath(os.path.join(self.ckpt_dir, "decisions.jsonl"))

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
                json.dump({"failed": sorted(self.failed), "exhaustions": self.exhaustions, "retries": self.retries,
                           "forced_failures": self.forced_failures, "grace": self.grace}, fh, indent=2)
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
            self.forced_failures = dict(data.get("forced_failures") or {})
            self.grace = dict(data.get("grace") or {})
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
        self._collect_build(wait=True)
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
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
            # Node appends one record per action here; the goal id lets the post-goal
            # review and the metrics pick out this goal's records (node ids repeat)
            "goal_id": self.goal_id,
            "decision_log": self.decision_log,
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
            self._collect_build()
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
                    records = trig.get("records") or []
                    if records and self.builder is not None and hasattr(self.builder, "remember"):
                        try:
                            self.builder.remember(payload, records, goal_id=self.goal_id)
                        except Exception as exc:
                            self._event("trace_save_failed", {"node": node_id, "error": str(exc)})
                    return "done"

                if ttype == "builder_wanted":
                    if trig.get("reason") == "none_of_these":
                        reason = (f"Jev's top answer was that no action on the menu can move the subtask forward "
                                  f"(none_of_these {trig.get('none')}, completable={trig.get('completable')}); the bot is holding still")
                    else:
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
                            # a fresh round gets the node's full budget: carrying the halved
                            # one forward made every re-post exhaust within a few actions (F3)
                            budget = int(_g(node, "budget_actions") or DEFAULT_BUDGET)
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
        node_id = _g(node, "id")
        if self.builder is None:
            self._event("builder_missing", {"node": node_id})
            return None
        if self.async_builds and not force:
            self._collect_build()
            if self._pending is not None:
                self._event("build_skipped", {"node": node_id, "why": "a build is already running", "trigger": reason[:120]})
                return None
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="builder")
            future = self._executor.submit(self._call_builder, dict(payload), status, list(self.tree_items()), reason, force)
            self._pending = (future, node_id, reason)
            self._event("build_started", {"node": node_id, "trigger": reason[:120]})
            return None
        try:
            name = self._call_builder(payload, status, self.tree_items(), reason, force)
        except Exception as exc:
            self._event("build_failed", {"node": node_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return None
        self._report_build(node_id, name, reason, force)
        return name

    def _call_builder(self, payload: dict[str, Any], status: dict[str, Any], tree_items: list[str], reason: str, force: bool) -> str | None:
        try:
            return self.builder.build(payload, status, tree_items, reason, force=force)
        except TypeError:  # a builder stub without the keyword
            return self.builder.build(payload, status, tree_items, reason)

    def _report_build(self, node_id: str, name: str | None, reason: str, force: bool) -> None:
        if name is not None or force:
            self._event("build", {"node": node_id, "name": name, "reason": reason})
        if name is None:
            why = getattr(self.builder, "last_refusal", None) or "the builder produced no action (no reason recorded)"
            self._event("build_refused", {"node": node_id, "why": why, "trigger": reason[:120]})

    def _collect_build(self, wait: bool = False) -> None:
        """Report a finished background build (or wait for the running one)."""
        if self._pending is None:
            return
        future, node_id, reason = self._pending
        if not wait and not future.done():
            return
        self._pending = None
        try:
            name = future.result()
        except Exception as exc:
            self._event("build_failed", {"node": node_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return
        self._report_build(node_id, name, reason, False)

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
        # Help is on its way: do not re-decompose or fail the node yet. In both runs of
        # 2026-10-13 the repaired action registered 7 to 33 s after its node had already
        # failed, so it was never tried.
        if self._grant_grace(node, status):
            self.exhaustions[node_id] = n - 1  # this exhaustion does not count
            self.save()
            return None
        # A craft/smelt/place step whose inputs are all in the bag has nothing to
        # gain from another recipe: the missing piece is the action itself.
        forced_failures = self.forced_failures.get(node_id, 0)
        if (
            (_g(node, "kind") in ("craft", "smelt", "place"))
            and self._inputs_in_hand(node, status or {})
            and forced_failures < self.max_forced_build_failures
        ):
            self._event("inputs_in_hand", {"node": node_id, "requires": _g(node, "requires")})
            name = self._build(
                node, payload, status or {}, force=True,
                reason=(f"every input for '{_g(node, 'description')}' is already in the inventory "
                        f"({_g(node, 'requires')}); the bot only lacks an action that performs the step itself"),
            )
            # A forced build that produced nothing counts toward a cap; without one the node
            # cycled exhaust -> build(None) -> re-post every few seconds (F3).
            self.forced_failures[node_id] = 0 if name else forced_failures + 1
            # the next round starts from zero exhaustions with the full budget, so the new
            # action gets a real chance before another forced build or a re-decomposition
            self.exhaustions[node_id] = 0
            self.save()
            return None
        if forced_failures >= self.max_forced_build_failures:
            self._event("forced_builds_exhausted", {"node": node_id, "failures": forced_failures})
        if self._try_redecompose(node_id):
            self.exhaustions[node_id] = 0
            self.retries[node_id] = 0
            self.save()
            self._event("redecomposed", {"node": node_id})
            return "redecomposed"
        self._fail_chain(node)
        return "failed"

    def _grant_grace(self, node: Any, status: dict[str, Any] | None) -> bool:
        """One more round when a build for this node is running (wait for it to finish
        first) or an action built for it is still untested. Capped per node."""
        node_id = _g(node, "id")
        used = self.grace.get(node_id, 0)
        if used >= self.max_grace_rounds:
            return False
        pending = self._pending is not None and self._pending[1] == node_id
        if pending:
            self._event("waiting_for_build", {"node": node_id, "round": used + 1})
            self._collect_build(wait=True)
        untested: list[str] = []
        if self.builder is not None and hasattr(self.builder, "untested_for"):
            fresh = status or {}
            if pending:
                try:  # the build just registered a new action: read it from a fresh status
                    fresh = self.bridge.agent_status()
                except Exception:
                    fresh = status or {}
            try:
                untested = list(self.builder.untested_for(node_id, fresh) or [])
            except Exception:
                untested = []
        if not pending and not untested:
            return False
        self.grace[node_id] = used + 1
        self._event("grace_round", {"node": node_id, "round": used + 1, "waited_for_build": pending, "untested": untested})
        return True

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
