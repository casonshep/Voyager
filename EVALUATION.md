# Evaluation: system-overhaul branch, and what the October 2026 papers say we should change

Written 2026-10-13 against commit 6a4d842 (`system-overhaul`). Design record: [PLAN.md](PLAN.md).
Evidence: the 40 checkpoint directories `ckpt_20261002_*` and `ckpt_20261003_*`, their
`typesafe/*.jsonl` audit logs, primitive sidecars, `skill_library/tree`, and the offline test suites
(44 Python tests pass; `verify_test.js` passes).

The aim of the project, as stated by the user: a self-learning Minecraft agent that uses LLMs plus
Jev to simulate a real-time player.

---

## 1. Verdict in one paragraph

The three-system design (decomposer, Jev action loop, action builder) works end to end: the agent
completed "obtain a wooden pickaxe" (7/7 nodes, 58 actions in `ckpt_20261003_123915`) and "obtain a
stone pickaxe" (10/10 nodes, 181 actions in `ckpt_20261003_142810`) from a movement-only menu,
writing its own mine, craft, place and explore actions. The weak part is the **learning**. Nearly
everything the agent learns is thrown away or duplicated: 163 generated primitives survive on disk
(123 in per-run checkpoints, 40 in the shared library); 66 ever succeeded, 40 were retired after
three failures, 43 were never tried, and the shared library holds seven near-identical
`approach_nearby_logs` files with zero trials. The traversal logs record 193 successful builds, 170
forced builds that produced nothing, and 138 refusals. No raw decision history is kept anywhere, so nothing in the system
can look back at *why* a primitive failed beyond the last three outcome strings. The four papers
all point at the same gap from different angles: keep raw evidence, make every learned artefact
revisable from that evidence, and measure learning across episodes rather than within one.

---

## 2. What the runs show (numbers first)

### 2.1 Outcomes per run (Oct 2 to Oct 3)

| Run | Goal | Nodes done | Primitives built / proven / retired / never tried | Build triggers / refused |
| --- | --- | --- | --- | --- |
| `ckpt_20261003_123915` | wooden pickaxe | 7 / 7 | 0 (library only) | 2 / 0 |
| `ckpt_20261003_135631` | wooden pickaxe | 7 / 7 | 14 / 8 / 4 / 1 | 14 / 21 |
| `ckpt_20261003_142810` | stone pickaxe | 10 / 10 | 13 / 11 / 2 / 0 | 13 / 24 |
| `ckpt_20261003_131443` | wooden pickaxe | 4 / 7 | 11 / 3 / 6 / 1 | 27 / 24 |
| `ckpt_20261003_145002` | equip iron helmet | 0 / 1 | 3 / 0 / 2 / 0 | 3 / 18 |
| `ckpt_20261003_150551` | iron helmet + boots | 9 / 20, **6 failed** | 18 / 12 / 5 / 3 | 18 / 31 |
| all 40 runs, per-run checkpoints only | | | 123 / 56 / 39 / 21 | |
| plus `skill_library/tree` (runs with the default `--library`) | | | 40 / 10 / 1 / 22 | |
| **surviving on disk** | | | **163 / 66 / 40 / 43** | |

Per-run rows count "build triggers" as every `build` event in `traversal.jsonl`, which includes
forced builds that returned nothing. Across all logs: **193 successful builds, 170 forced builds
that produced no action** (most of them the F3 loop below), and **138 refusals** (73 "the newest
trial has not had its chance", 47 "a build finished moments ago", 16 "produced no action", 2 "all
drafts rejected"). The builder's own GPT calls are not timed (the curriculum's are, via
`timing.timed`), so build latency is not in `timing.jsonl`.

Kinds of primitives in the per-run checkpoints: craft 54, mine 19, place 14, other 11, explore 9,
goto_nearest 7, collect 4, equip 4, use 1. Fifty-four craft actions for what is, in the game, one
recipe-driven operation.

### 2.2 Real-time cost

From `ckpt_20261003_131443/typesafe/traversal.jsonl`: node n5 ("obtain 3 logs") took 29 actions in
71 s, about 2.4 s per action; node n4 ("craft 9 planks") took 20 actions in 93 s, about 4.7 s per
action. Each action is one Jev request (four questions) plus the action itself, with a 500 ms floor
(`voyager/env/mineflayer/lib/agentLoop.js:16`). Jev latency is accumulated in `stats.jev_ms`
(`agentLoop.js:399-400`) but never persisted, so the split between thinking time and acting time is
unmeasured. Builds run in Python while Node keeps acting, so a build does not pause the bot, but a
bad trial action on the menu costs real actions (every retired primitive cost at least three).

For "simulate a real-time player": a human's micro-actions (turn, step, swing) happen several
times a second, and the documented Jev latency is about 100 ms per request. The measured 2.4 to
4.7 s per action is therefore 10 to 40 times slower than the goal, and most of that time is not
Jev: it is the 500 ms floor, 2.5 s walk steps (`baseActions.js:15`), 20 s action timeouts, and
pathfinder calls inside generated actions. Without per-decision timing (R2) the split cannot be
diagnosed.

### 2.3 Six failure patterns the logs prove

**F1. The repair loop does not repair.** In `ckpt_20261003_131443` the builder wrote
`place_crafting_table` seven times for the same node. Versions 2 and 6 differ only in comments.
Versions 3 and 7 both call `bot.equip("crafting_table", "hand")` with a string, although the
builder's own contract says `equip` takes an inventory Item object
(`voyager/tree/builder.py:134-135`), and version 7's menu guard `snap.inventory.crafting_table < 1`
is false when the key is absent. The attempt history shown to GPT is capped at the last four
attempts, 900 characters of code each, and the last three outcome strings
(`builder.py:913-938`, `voyager/env/mineflayer/lib/primitiveRegistry.js:290`).

*Correction (2026-10-13, found while implementing R1).* The first version of this report said the
author model "defaults to gpt-4o-mini" and called that the cheapest lever. That is wrong for the
current setup: `.env` sets `OPENAI_MODEL=gpt-6.1-sol`, and `.env` was last changed at 14:27 on
Oct 3. The seven-rewrite run (13:14) predates that change and probably did run on a weaker
model, but the 10/10 stone-pickaxe run (14:28) and the iron-armour runs (14:50, 15:05) ran on
gpt-6.1-sol and still showed F1 and F2. The root cause is in the repair path itself:
`_failing_trial` skipped any retired action, and Node retires at the third failure, so repair
could only fire between failures two and three. Across 163 primitives, repair fired 4 times, and
each time it produced a differently named action instead of fixing the old one.

**F2. Kind exclusion pushes Jev into bad designs.** `_open_kinds` (`builder.py:732-751`) removes
any kind that already has a proven action anywhere, plus every kind already tried for the subtask.
In `ckpt_20261003_150551/typesafe/builder_design.jsonl` the "obtain 11 cobblestone" node asked Jev
to design with `mine`, `craft` and `place` missing from the criteria; Jev answered `goto_nearest`
at 0.29 confidence, then `use` at 0.50, then `place` at 0.26, with target `stone` every time. The
resulting primitives (`uncover_buried_resource`, `place_mining_foothold`,
`access_and_harvest_shallow_resou`) were retired, the node failed, and the failure chained through
furnace, smelting, helmet and the root goal (six `node_failed` events). The exclusion exists to
stop GPT rewriting the same action, which is the right goal, but the fix belongs in F1 (repair),
not in hiding the correct answer from Jev.

**F3. A tight re-post loop with no recorded reason.** In the same `ckpt_20261003_131443` run, node
n3 ("craft 1 crafting_table") cycled `retries_exhausted -> budget_exhausted(count=2) ->
inputs_in_hand -> build(name=None) -> build_refused -> subtask_posted(budget=7)` twenty times
between 1791058899 and 1791058961, one cycle every 3 to 4 s. Three things combine:

* the stuck branch carries the halved budget (7 actions) into every re-post
  (`voyager/tree/traversal.py:324-327`), so each cycle exhausts in a few actions;
* `_exhausted` resets the exhaustion count to 1 after a forced build (`traversal.py:423`), so the
  next exhaustion immediately qualifies for another forced build;
* the forced build returned `None` without setting `last_refusal` (the event shows the fallback
  text "the builder produced no action"). No `action_rejected` or `action_generation_failed`
  records exist in that window, so the three-draft loop never completed. The two code paths that
  return `None` silently are "no LLM handle" (`builder.py:941-943`) and an exception from
  `bridge.agent_primitive` (`builder.py:982-987`). The ~1.2 s cycle time fits one short GPT call
  followed by a failed post to Node, but that is inferred, not logged.

**F4. `completable` is structurally stuck low while a trial exists.** The Noul is asked over
`proven_menu` only (`agentLoop.js:317-323, 389-394`), so as long as the relevant action is still a
trial, the answer sits at 0.06 to 0.11 and the builder fires at every checkpoint. The cooldown rules
in `cooling_down` (`builder.py:539-586`) then refuse most of those triggers. That is where the 138
refusals come from. It is a framing choice, not a Jev error: Jev is answering the question asked.

**F5. No raw history survives.** Node keeps `recent` capped at 40 records and clears it on a new
subtask (`agentLoop.js:236, 642-643`); decisions and Jev probabilities are printed to the console
only (`agentLoop.js:419-422`); the library sidecar keeps trial/success counts and the last three
outcomes. Nothing persists the sequence (state summary, menu, Jev distribution, chosen action,
outcome, delta) that would let a later run, or a reflection step, see what actually happened.

**F6. Library pollution.** `skill_library/tree` holds 40+ entries. Proven ones are few:
`craft_crafting_table` 25/25, `craft_planks_2` 14/16, `explore_for_logs` 37/60, `mine_nearby_log`
28/72. The rest are duplicates with zero trials (`approach_nearby_logs` through `_7`, all kind
`other`). `reload(proven_only=True)` (`builder.py:374-426`) hides them but nothing merges, dedupes
or deletes, and the `_unique_name` suffixing (`builder.py:718-730`) guarantees growth.

**F7. The decomposer accepted an empty plan.** `ckpt_20261003_145002`, goal "obtain and equip an
iron helmet", produced a **one-node tree** ("obtain and equip an iron helmet", kind equip, no item
subtree). The loop spent 120 actions on it, built three `equip` primitives and retired two. The
next run (`ckpt_20261003_150551`) got a 20-node tree for nearly the same goal. *Correction:* that
tree used an older target format; the plan validator committed at 15:36 on Oct 3 already adds the
item for an `equip` final step. The same gap remained for `place`, `eat` and `use` final steps with
no items, and is now closed. A related glitch: `ckpt_20261003_124450` ran the goal text "obtain a
stone pickaxe pickaxe", a curriculum parse artefact (`voyager/agents/curriculum.py:96-103`).

### 2.4 Things that are working and should be kept

* **Verifiable subtasks.** Every node has a code-checked `VerifySpec`; `done` is persisted on first
  pass and never re-evaluated. This is what made resume-after-crash and done-node skipping work.
* **Grounded success scoring for generated actions.** An "ok" only counts if the declared purpose
  item went up, or the bot moved or placed a block (`agentLoop.js:618-641`). This is the right
  instinct and matches the "invalid action" framing in EmbodiedCLBench.
* **One Jev request per decision with parallel questions.** Latency is bounded and the four answers
  are reused by code.
* **Sampling from Jev's distribution with a temperature that flattens when the bot is repeating
  itself** (`agentLoop.js:31-32, 406-414`). Learn2Play's finding that agents over-exploit (action
  sequence similarity 0.66 vs 0.54 for humans) says this knob matters; it is currently untuned and
  unmeasured.
* **Jev never sees code.** The menu line is the whole interface, which keeps the decision input
  small and the library inspectable.

---

## 3. What the papers say, mapped to the failures above

Full notes with sources are in section 5. The mapping:

| Paper finding | Our failure | What it implies |
| --- | --- | --- |
| **Learn2Play:** raw interaction histories (MEMORY) learn better than Reflexion, ReasoningBank, AWM, EvoTest; distilled rules turn one failure into a wrong permanent rule | F5 (no raw history), F1 (repair sees 3 outcome strings) | Persist raw per-decision records; make them the evidence every lesson and primitive cites |
| **Learn2Play:** agents over-exploit (sequence similarity 0.66 vs 0.54 for humans); "always serve index 0" | F1 (same design rewritten 7 times), untuned sampling temperature | Measure attempt-to-attempt similarity and bias against it; the temperature knob exists but is unmeasured |
| **Learn2Play:** coding-agent harnesses beat scripted self-evolving methods, at lower cost, because the model decides when to review and runs several actions per call | The builder is one-shot prompt-and-parse; the retry / re-decompose ladder is rigid (F3) | Give the builder a repair loop with the evidence in hand and a fake-world dry run, not three blind drafts |
| **EmbodiedCLBench:** one learned sample gives most of the gain; the gain comes from fewer invalid actions; skill summaries help but shift behaviour less than raw trajectories | `proven_only` reload is the right instinct; invalid actions are already scored (NO EFFECT) but not tracked | Adopt invalid-action rate and invalid-build rate as the learning metrics; retrieve one matching prior trace into context |
| **AgentGarten:** post-round review writes lessons as "another attempt's claims" with evidence IDs, "what I still doubt" and "what to test next"; fresh-context inheritance; lessons tested before passed on | There is no post-goal reflection anywhere; the only cross-run artefact is primitive counts (F5, F6) | Add a post-goal review that writes per-family lessons with evidence IDs; promote a lesson only after a replay confirms it |
| **SuperNav:** Markdown skills loaded on demand are worth more than the tool interface (35.0 to 68.3 SR); tools return the next observation; text history pruned, never dropped | The builder contract is a hand-written skill doc that never updates from experience; actions return a string and the snapshot is taken separately | Turn the contract into per-family skill files the review step maintains; attach the post-action observation delta to every record |
| **MineEvolve / RESKILL / SkillRevise:** patch skills with a typed failure label and a repair budget instead of retiring and rewriting; structured remedy store beats free-text reflection (35.0 vs 29.6) | F1, F2, F6 | Replace retire-after-3 with repair-under-budget; store remedies with `failure_type, repair_action, scope, evidence` |
| **MrSteve / WISE / Mindcraft / MineExplorer:** count-based exploration plus a landmark memory; navigation is the dominant failure class | Exploration primitives are rebuilt per run (`explore_for_logs` 37/60); nothing remembers where a tree or placed table was | Add a landmark table and visitation grid to the snapshot (observation, not action) |
| **Parallel Planning-Acting / Optimus-3 / Latent Bridge:** slow planner and fast actor run concurrently through a one-slot buffer | Builds are synchronous in the Python poll loop; Node keeps acting but triggers queue up for 30 s or more | Move builds off the poll loop |
| **TypeSafe docs:** precise Noul definitions, a none option, shuffled option order, pre-digested facts, speculative fan-out is almost free, ~100 ms typical latency | F4 (`completable` framing), vague `progress` Noul, dx/dy/dz in state, 500 ms floor | Rework the four questions; measure latency before tuning the floor |
| **MC-EvoBench:** reference agents showed no stable transfer because early wood and crafting bottlenecks masked everything downstream | Our iron-armour run failed at cobblestone (F2) | Fix the wood-to-stone ladder reliability first, then measure transfer with a fixed protocol |

---

## 4. Recommendations, in order

Ordered by evidence strength and cost. Each one keeps the two constraints in PLAN.md and the
project memory: Jev never sees code, and stalls are fixed by improving the learning loop, not by
hand-writing actions. R1 to R3 are fixes to the loop as built; R4 to R7 are the additions the
papers argue for; R8 is how to know whether any of it worked.

**R1. Make repair the default and retire-and-rewrite the exception** (F1, F2, F6).
* Make the repair path reach retired actions (see the F1 correction); the `.env` model is
  already gpt-6.1-sol. Align the `llm.py` fallback with it and record the model on every build.
* When a trial fails, the builder should receive the complete failing source, every outcome
  string it produced, the Node error text, and a typed failure label (API_MISUSE, NO_TARGET_IN_REACH,
  NO_FREE_SPOT, NOT_APPLICABLE, TIMEOUT), and be asked for a local edit. Today it gets 900
  characters of code and three outcomes (`builder.py:913-938`; `primitiveRegistry.js:290`).
* Replace `RETIRE_AFTER_FAILURES = 3` (`primitiveRegistry.js:25`) with a per-skill repair budget;
  keep the best-scoring version; stop minting `_2` to `_7` suffixes (`builder.py:718-730`).
* Remove the proven-kind exclusion in `_open_kinds` (`builder.py:732-751`). When the right kind
  already has a proven action that is not applicable, the gap is a precondition (reach, tool,
  free spot), so route to repair or to a precondition action, and tell Jev that in the criteria
  rather than deleting the option.
* Dedupe the library on `(kind, target, purpose_spec)` at reload and drop entries with zero trials
  after N runs.

**R2. Persist a raw decision record for every action** (F5; Learn2Play, AgentGarten).
* In Node, append one JSON line per decision to `<ckpt>/decisions.jsonl`: subtask id, a digest of
  the snapshot, the menu ids, Jev's probabilities and confidence, the chosen action and its
  source (jev / sampled / fallback), the outcome string, the delta, and `jev_ms`. Node currently
  prints this to the console only (`agentLoop.js:419-422`).
* Give every record an id. Primitives, lessons and remedies cite the ids that justify them, so a
  later run can re-read the evidence instead of trusting the conclusion.
* Keep these traces in the shared library next to the primitives, keyed by subtask description,
  and retrieve the single most similar prior trace into the builder prompt. One exemplar is enough
  (EmbodiedCLBench); more did not help.
* This also gives the first real-time measurement: Jev latency per decision versus action time.

**R3. Reframe the builder trigger and fix the re-post loop** (F3, F4).
* Ask `completable` over the whole menu including trials, or better, add a `none_of_these`
  option to the action Choice ("no option on this menu can move this subtask forward") and fire
  the builder when that option wins at a checkpoint. The TypeSafe docs recommend the none option
  over a separate multi-hop Noul. Today the Noul is pinned at 0.06 to 0.11 whenever a trial exists
  (`agentLoop.js:317-323, 389-394`), which is the source of the 138 refusals.
* Do not carry the halved budget into forced-build re-posts (`traversal.py:324-327`) and do not
  reset exhaustions to 1 after a forced build (`traversal.py:423`); otherwise a node can cycle
  every 3 s.
* Set `last_refusal` on every `None` return in `build` and `_write_and_register`
  (`builder.py:941-943, 982-987`), log a `build_failed` record with the exception text, and wrap
  every builder LLM call in `timing.timed` as the curriculum already does, so F3 cannot happen
  silently again.
* Reject an empty item plan in the decomposer's plan validation (F7): a `final` step with no items
  and a verify spec that is not already true must go back to GPT, as PLAN.md 4.1 step 1 specifies.
* Move builds off the Python poll loop into a worker thread with a one-slot result buffer
  (Parallel Planning-Acting). A 30 s build currently stops trigger handling.

**R4. Add a post-goal review that writes lessons as claims with evidence** (AgentGarten,
EmbodiedCLBench, MineEvolve, SuperNav).
* After each goal, one GPT call in a fresh context reads only the raw traces for that goal (R2)
  and writes, per subtask family (find-X, acquire-X, craft-X, place-X), a short Markdown file
  with four required sections: tried, observed, still doubt, test next. Keep it under 1000 words
  (EmbodiedCLBench's limit) and label it "reference guidance, not ground truth".
* Alongside the prose, write structured remedies `{failure_type, repair_action, scope,
  confidence, evidence_ids}` (MineEvolve's schema). Mark anything backed by fewer than two
  outcomes as a hypothesis.
* The builder loads only the family file relevant to the subtask it is building for (SuperNav's
  on-demand loading), in place of the growing hand-written `CONTRACT` (`builder.py:71-227`),
  which then shrinks to the API facts.
* "Test next" items become candidate subtasks for the decomposer; "still doubt" items go into the
  builder prompt as assumptions the new action must check inside `execute()`.
* Promote a lesson from hypothesis to confirmed only after a later run's trace agrees. Raw traces
  stay authoritative (Learn2Play's reason MEMORY won).

**R5. Rework the four Jev questions against the docs** (TypeSafe docs; F4).
* Turn `progress` into a Score with concrete situation levels, or drop it: the code already has
  `madeProgress` (`agentLoop.js:708-718`) and the hard stuck triggers; the Noul is vague by the
  docs' own definition.
* Pre-digest facts before they reach Jev: near / far / out of reach instead of dx,dy,dz; blocked /
  open per direction instead of a 30-block ray; visited / new per horizon direction. Jev is weak
  at arithmetic and counting.
* Shuffle menu order per request (first-option bias) and keep `wait` and `look:*` from
  dominating by giving each a one-line consequence.
* Use the free fan-out: per-option "would this option change the world right now" Nouls and
  per-direction exploration Scores in the same request cost only their tokens.
* Measure Jev latency (R2) before changing the 500 ms floor; the docs say ~100 ms is typical.

**R6. Observation-side spatial memory** (MrSteve, WISE, Mindcraft, MineExplorer).
* Add a landmark table to the snapshot: for every block type the tree cares about, the nearest
  position ever seen, how long ago, and whether it is still there; and a coarse visitation grid
  so horizon directions can be labelled visited or new.
* This is observation, not action, so it respects the movement-only base menu. Learned explore
  and approach actions (`explore_for_logs` at 37/60, `approach_needed_block` at 5/10) get the
  facts they need, and the review step can explain why they failed.

**R7. Let the builder compose, and dry-run before trial** (Odyssey, JARVIS-1, Learn2Play).
* Proven primitives in the library should be callable from new primitives (the `loop` object
  already exposes helpers; expose `loop.run("x:craft_planks", ctx)`), so a craft-table action can
  call the proven planks action instead of re-implementing it. Fifty-four craft actions were
  written for one recipe operation.
* Before a draft becomes a trial that costs in-game actions, run its `menu()` and `execute()`
  against the fake world (`FAKE_WORLD=1`, `scripts/fakeWorld.js`) with the inventory set to the
  subtask's inputs. Today the only pre-trial check is static and applicability.
* The field's alternative is a hand-verified set of parameterised primitives (Odyssey's 40,
  Mindcraft's `skills.js`). That conflicts with this project's rule that actions are learned, not
  written, so it is recorded here as the option the literature takes, not as a recommendation.

**R8. Measure learning across runs, not within one** (EmbodiedCLBench, MC-EvoBench, Learn2Play).
* Fix a ladder: log, planks, table, wooden pickaxe, stone pickaxe, furnace, iron ingot, iron
  armour. Run it with no library and with the carried library, five seeds each, matched budgets.
* Report per run: nodes done within budget, actions per node, invalid-action rate (failed or NO
  EFFECT outcomes over all actions), builds per goal, repairs versus rewrites, library reuse count,
  attempt-to-attempt action-sequence similarity, Jev ms per decision, navigation failures as a
  separate class. `scripts/progress_report.py` is the place to extend.
* MC-EvoBench's warning applies directly: until the wood-to-stone ladder is reliable (it was not
  in `ckpt_20261003_150551`), no transfer measurement will show anything.

---

## 5. Research notes

### 5.1 AgentGarten: Code Worlds for Evolving Agents (arXiv 2610.12374, MirroS, 8 Oct 2026)

Most of the paper is a neural renderer (a video model that draws the world from engine geometry);
that part is irrelevant to us. Section 5 is the agent loop, and it is the part worth copying.

* **Repo status.** `github.com/MirroS-Lab/AgentGarten` is public but contains only the renderer.
  The README's TODO says the code worlds and "the agent practice loop ... rounds of play, review,
  and playbooks" are unreleased. No play or review prompt is published. Everything below is from
  the paper text and Appendix D of the mirros.ai report PDF, which holds the verbatim task files.
* **The loop.** "A round begins with a task file that states the goal, the available actions, and
  the limits, but no solution. One or more agents then play in parallel, each within a fixed
  budget of steps or simulated time. Afterwards each agent writes a playbook that records what it
  tried, what it observed, what it still doubts, and what to test next. Playbooks are archived,
  and the next round's agents, started in fresh conversations, receive the task file and the
  playbooks of earlier rounds." Nothing is merged or pruned; round N gets the task file plus every
  earlier playbook. The agent is a frontier model (their ref [32] is GPT-6 Astra) in a fresh
  conversation per round, with no weight updates.
* **Hide-and-seek variant.** Agents act by submitting short Python programs (`def policy():
  step(turn=-1, steps=2); pull(1); goto(180, 2)`) and then observing. "Each role keeps its own
  playbook, organized as a library of short skill files, Markdown notes that may contain code,
  with one lesson per file" (named examples: `check_shelter.md`, `object_control.md`,
  `ramp_landing.md`). After each round the role "reviews its own action programs and the visual
  evidence it was permitted to see, identifies failures, and adds skill files." Both successful
  and failed attempts feed revisions.
* **Task file format** (Appendix D): a title, the goal and observation limits in prose, a Markdown
  table `| Action | Fixed duration | Meaning |`, optional cue section, and an "Evaluation and
  experience" section giving the score rule while withholding the solution. Every task file
  carries an epistemic block: "distinguish uncertain visual evidence from conclusions. Record
  evidence IDs, failures and hypotheses in your playbook. An inherited playbook is another
  attempt's claims, not ground truth." and "do not mistake a guess about an unseen area for an
  observation."
* **Why it differs from Voyager/Reflexion** (their Section 6): "playbooks pass between independent
  agents in fresh conversations, so each lesson has to be stated well enough for another reader to
  check it." Section 7: because a world can be reset and replayed exactly, "a lesson can be tested
  before it is passed on."
* **Evidence.** Hide-and-seek: shelters built by round 4, ramps used by round 10 (the RL baseline
  needed about 25M and 100M episodes; the paper itself says this is not a sample-efficiency
  ratio). Four other worlds over four rounds: companion-dog score 13, 14, 19, 19; one-lane bridge
  71, 68, 45, 41 s; herding 60, 90.1, 87.6, 88.3; quarry loader 30, 0, 30, 90.9. One episode per
  round, no no-playbook ablation. Suggestive, not controlled.
* **What maps onto us.** (a) Lessons as claims with evidence IDs, promoted only after replay
  confirms them: our primitives are already trials; prose lessons should be too. (b) One lesson
  per Markdown file, keyed by subtask family (mining, movement, crafting), reviewed after each
  goal, loaded only when relevant. (c) A required review schema: tried / observed / still doubt /
  test next; "test next" items become candidate subtasks for the decomposer, "still doubt" items
  become uncertainty notes in the builder prompt. (d) The task-file discipline (goal, action table
  with cost and meaning, budget, evaluation rule, no solution) is almost a spec for what Jev's menu
  and the verifier already are. (e) Fresh-context inheritance: start the builder and the post-goal
  reviewer from the task file plus archived lessons, never the transcript, so lessons must be
  stated well enough for another reader. (f) Open-loop guard: keep `execute()` bodies short and
  re-observe between batches, which our 20 s action timeout already approximates.

Sources: https://arxiv.org/abs/2610.12374 , https://mirros.ai/report/agent-garten.pdf (Appendix D),
https://github.com/MirroS-Lab/AgentGarten , https://mirros-lab.github.io/agent-garten ,
https://mirros.ai/blog/worlds-for-evolving-agents

### 5.2 Learn2Play Bench (arXiv 2610.08215, NUS, 8 Oct 2026)

Twenty newly designed text games with hidden, sometimes counter-intuitive rules, played for 5 or
10 episodes; this isolates "learned from interaction" from "already knew". Their five findings:

1. **Raw histories beat processed experience.** MEMORY (the complete, unmodified interaction
   histories of earlier episodes added to each prompt, oldest dropped when over budget) leads
   Mean, Learning Gain and Learning Slope with Kimi K3 and beats Reflexion, ReasoningBank, AWM and
   EvoTest on average (Reflexion keeps a slightly higher Max). Reason given: "reflection and
   memory processing can turn an incomplete understanding into rules or workflows that later
   episodes continue to follow, while raw histories keep the original evidence available for
   revising those interpretations." Concrete case (B.1.3): after one failed gem fusion, an
   EvoTest prompt told the agent "never to attempt tier-2 or tier-3 fusion, ruling out
   combinations it has not tested."
2. **Humans explore more.** Consecutive-episode action-sequence similarity (difflib
   SequenceMatcher ratio) is 0.54 for humans, 0.66 for agents; after a worse episode, humans set a
   new personal best 33% of the time, agents 22%. Failure mode named in B.1.4: GPT-5 Mini "serves
   dish 0 at all three banquets ... its saved notes describe a policy of always serving the same
   index."
3. **Harness beats method.** Coding-agent harnesses (OpenCode, Claude Code, Codex) with the same
   backbone beat every scripted self-evolving method, and cost less: OpenCode "executes several
   actions from one model response and removes repeated game feedback" (8.1 model calls per episode
   vs 24 to 30 for scripted methods; 56% fewer input tokens than MEMORY). One trace batches nine
   game actions into one call and returns only the last 60 output lines. Costs per episode:
   Claude Code + Opus 5 $0.65 (Max 81.8), OpenCode + GPT-6 Astra $0.25 (76.2), MEMORY + Opus 5
   $3.09 (60.9). EvoTest's between-episode updates alone cost 11.5K tokens per episode, more than
   OpenCode's whole episode.
4. **Transfer to re-shuffled instances is slower**, more so for weaker backbones; agents that
   remember *what* worked but not *why* repeat episode-specific choices such as a menu position.
5. **Agents learn rules but fail to plan with them** (Haunted Inn: knows the taboos, assigns rooms
   greedily).

Repo (`liushiliushi/Learn2Play-Bench`) holds the games only; no prompts, no MEMORY serialisation,
no agent loop. OpenCode runs were wired through a black-box command interface (`play.py` with
actions on stdin, a JSON replay endpoint returning `{observation, valid_actions, score, done}`),
shell and file tools, and a fresh session per trial.

### 5.3 EmbodiedCLBench (arXiv 2610.13021, PKU/BIGAI, 9 Oct 2026)

Continual learning measured as **compositional generalisation**: learn basic tasks (2 atomic
actions, 6-step shortest path), then solve unseen advanced tasks (4 atomic actions from 2 to 4
categories, 12 steps) within shortest + 3 steps. Offline state-transition graphs over TongSim
household scenes; each state offers optimal, suboptimal and invalid candidate actions written so
that none stands out linguistically.

* **Numbers.** Zero-shot advanced success 3 to 12%; after two basic tasks, In-Context Learning 16
  to 52% and Skill Learning 22 to 38%, across OpenClaw, Claude Code, Codex and Hermes harnesses
  with Qwen3.8-Flash and GPT-5.6-Terra. The gain over zero-shot stays near 30 points as task
  complexity rises from 3 to 6 atomic actions. **One** learning sample gives almost all of the
  gain (8 to 10% to 28 to 40%); k = 2 to 6 adds nothing consistent. Experience cuts invalid action
  selections by 13 to 20 points; that is where the success comes from.
* **How ICL was done.** Not trajectories pasted into a prompt: the harness runs the basic tasks in
  a native session, exports a checkpoint, and the advanced task *resumes the session* with "Use
  the preceding interactions as learning examples ... They are reference experience, not the
  current task, and their ending does not mean that the new task is complete." History is
  compacted only by dropping duplicate task and choice blocks.
* **How Skill Learning was done.** One tool-less turn after the basic tasks: "write a skill
  description in Markdown format ... at or below 1000 English words"; injected into a fresh
  session as "reference guidance, not a task episode or a completion result. The current task
  goal, current image, previous feedback, and listed choices remain the source of truth." Skill
  summaries shift behaviour less than raw trajectories but are cheaper and sometimes equal.
* **Feedback strings are precondition-specific**: "could not be executed because the required
  object was not currently in view / reachable / being held"; "was allowed but made no progress."

Repo: `PKU-VaLuE-Lab/EmbodiedCLBench` (prompts in
`public/eval/src/tongbench_eval/cli/framework/dialogue_runtime/parts/`, compaction in
`frameworks/hermes_impl/task_history_compact.py`).

Sources: https://arxiv.org/abs/2610.08215 , https://arxiv.org/src/2610.08215 (TeX appendices),
https://github.com/liushiliushi/Learn2Play-Bench , https://arxiv.org/abs/2610.13021 ,
https://github.com/PKU-VaLuE-Lab/EmbodiedCLBench ,
https://huggingface.co/datasets/PKU-VaLuE-Lab/EmbodiedCLBench

### 5.4 Minecraft LLM agents, 2024 to October 2026 (survey)

| System (year) | Control | Planner | Memory / skills | Code |
| --- | --- | --- | --- | --- |
| Voyager (2023) | GPT-4 JS mineflayer programs | auto-curriculum, critic | code library, embedding retrieval | MineDojo/Voyager |
| DEPS (2023) | goal-conditioned controllers | describe-explain-plan-select | none persistent | CraftJarvis/MC-Planner |
| JARVIS-1 (2023) | STEVE-1 + craft/smelt | self-check (dry-run plan), self-explain | (task, situation) to plan memory | CraftJarvis/JARVIS-1 |
| Odyssey (IJCAI25) | mineflayer JS, 40 parameterised primitives + 183 compositional skills | planner-actor-critic on LLaMA-3-8B | embedding top-5 retrieval of skill descriptions | zju-vipa/Odyssey |
| Optimus-1 (NeurIPS24) | STEVE-1 | knowledge-graph planner | multimodal experience pool; reflector COMPLETE / CONTINUE / REPLAN | JiuTian-VL/Optimus-1 |
| Optimus-3 (2025, v2 2026) | MoE VLA, "System 1 / System 2" | task-routed experts | - | cybertronagent.github.io |
| MrSteve (ICLR25) | STEVE-1 + VPT-Nav | LLM subgoals | Place Event Memory; count-based exploration | frechele/MrSteve (unverified) |
| Mindcraft (2024 to 26) | mineflayer discrete `!command(args)` menu; `!newAction` code off by default | self-prompted goals | `skills.js` JSDoc library, `!rememberHere`, embedding example selection, Andy-4 local models | mindcraft-bots/mindcraft |
| Parallel Planning-Acting (2025/26) | mineflayer skill API, recursive | planner + actor threads, single-slot buffer, interrupt flag | weighted prerequisite DAG | in zju-vipa/Odyssey |
| MineEvolve (Mar 2026) | STEVE-1 | LLM subgoals; suffix-only replanning | schema-validated skill + remedy store | xzw-ustc/MC-MineEvolve |
| Echo (Apr 2026) | MLLM plan + executor | analogy retrieval | structured task descriptors, success-only | CatworldLee/Echo |
| WISE (Jun 2026) | STEVE-1 + VPT-Nav | opportunistic scheduler | causal event graph; quadtree / frontier / Voronoi exploration | no repo |
| MineExplorer (2026) | frames, 0.1 s steps | benchmark | - | meituan-longcat/MineExplorer |
| MC-EvoBench (Oct 2026) | MineRL | benchmark, task-state graph | - | xzw-ustc/mc-bench-open-source |
| MineStudio / MCU (ICML25) | any | benchmark toolkit, 3,452 atomic tasks | - | CraftJarvis/MineStudio |

Also relevant outside Minecraft: RESKILL and SkillRevise (attribution-driven *patching* of
skills instead of rewriting), Latent Bridge (a slow VLM steering a millisecond-latency fast
model), and the DOOM-1.3M study (LLMs at 646 ms to 13 s per decision vs 31 ms for a small model).

**The eight ideas that transfer:**

1. **Patch, don't retire-and-rewrite** (RESKILL, SkillRevise, MineEvolve). A typed failure label
   (NAV_STUCK, NO_CRAFT_GRID, NO_PLACE_SURFACE, API_MISUSE), the old code, the trace and the label
   go to the builder, which makes a local edit; versions compete under a per-skill repair budget
   and the best is kept. The seven `place_crafting_table` versions are exactly the pathology this
   addresses.
2. **Parameterised primitives plus recursive compositional skills over a prerequisite DAG**
   (Odyssey: 40 + 183; Parallel Planning-Acting: `obtainItem` calls `mineItem` and `craftItem`;
   Mindcraft: `craftRecipe` calls `getNearestFreeSpace` before `placeBlock`). Verify a small set of
   parameterised primitives once; have the builder *compose* them rather than author raw
   mineflayer calls every time.
3. **Structured skill and remedy store with verification conditions** (MineEvolve: skills carry
   `trigger_context, preconditions, steps, verification, confidence, supporting_feedback`;
   remedies carry `failure_type, repair_action, scope`). Hard-task success rose 25.1 to 35.0 over
   400 episodes, beating free-text reflection at 29.6.
4. **Replan only the unfinished suffix** (MineEvolve; JARVIS-1 self-check). Freeze the done
   prefix of the tree; regenerate only the failing subtree, after a dry-run inventory simulation.
5. **Count-based exploration plus landmark memory** (MrSteve: least-visited grid cell, 84% map
   coverage; WISE; Mindcraft `!searchForBlock` and `!rememberHere`). MineExplorer finds
   navigation is the dominant failure class, so measure it separately.
6. **Concurrent slow planner / fast actor with a single-slot buffer and an interrupt flag**
   (Parallel Planning-Acting; Optimus-3; Latent Bridge). GPT never blocks the fast loop.
7. **Retrieve skills by description embedding, keyed on situation** (Voyager, Odyssey, JARVIS-1,
   Echo), so the menu stays small as the library grows.
8. **A periodic reflector with three verdicts** (Optimus-1: COMPLETE / CONTINUE / REPLAN plus
   predicament labels), which is a drop-in Jev Choice.

**Metrics the field uses:** tech-tree ladder (Optimus-1 / MineEvolve: 67 to 70 tasks across
wood, stone, iron, gold, redstone, diamond, armour; success rate, average steps, average time,
30 or more seeds); MC-EvoBench's transfer protocol (reset world, carry only the declared learned
artefact, compare no-prior / related / unrelated / shuffled experience at matched step budgets,
5 seeds; report stages passed, weighted DAG progress, milestones; their finding is that reference
agents showed no stable transfer because early wood and crafting bottlenecks masked everything
downstream); continual curves (Echo Success@0 to 10 and 0 to 30); exploration coverage and revisit
counts (MrSteve, Odyssey); MineExplorer's failure taxonomy (navigation, gathering, action, goal
misidentification).

Sources (abs ids): 2603.13131 MineEvolve, 2610.13829 MC-EvoBench, 2606.12852 WISE, 2605.30931
MineExplorer, 2604.05533 Echo, 2503.03505 Parallel Planning-Acting, 2407.15325 Odyssey,
2411.06736 MrSteve, 2408.03615 Optimus-1, 2506.10357 Optimus-3, 2311.05997 JARVIS-1,
2504.17950 Mindcraft/MineCollab, 2609.15684 RESKILL, 2606.01139 SkillRevise, 2606.24470 Latent
Bridge, 2604.07385 DOOM small-model study; https://github.com/mindcraft-bots/mindcraft ,
https://github.com/CraftJarvis . Provenance caveat: Optimus-3 v2, LARM, ROCKET-2 and the Andy-4
diamond-armour claims come from abstracts only; WISE and Luban have no public code.

### 5.5 SuperNav (arXiv 2610.12126, ZJU, 8 Oct 2026)

A navigation harness around an unmodified multimodal LLM. The vision and robot parts do not
apply to us; the harness design does. Code: `github.com/zju3dv/SuperNav`.

* **Skills are Markdown, loaded on demand.** "Each Skill is a Markdown package describing its
  applicable situations, evidence to inspect, recommended steps, and linked references." The
  harness exposes only names and descriptions; the model loads a skill's instructions when
  relevant. Skills "neither add environment capabilities nor programmatically enforce action
  sequences." In the repo, `skills/global-navigation/SKILL.md` has rules with stable IDs and
  MUST/SHOULD/MAY wording, plus a "load the needed rules" table saying *when* to read each
  reference (spatial memory before the first junction, recovery "at the first stall, or before
  any blocked close").
* **Tool design principle.** "Inputs easy for the MLLM to specify and returns useful for the next
  decision." A motion tool returns the new four-view observation, execution status and concise
  diagnostics, so there is no separate observe call after every move.
* **Progress records and pruning.** Goal records hold pending and completed items; text history
  keeps inspected areas, candidate clues and failed attempts. Pruning is media-only: old images
  are dropped from later requests, text stays. Recovery is a fixed ladder; a "blocked" close is
  refused unless every alternative was tried ("evidence, not likelihood").
* **Ablation** (HM3D-OVON, 120 episodes, success at 0.25 m / 1 m): without skills 35.0 / 54.2;
  without the tool design 52.5 / 65.8; without both 31.7 / 54.2; full 68.3 / 73.3. The Markdown
  guidance is worth more than the interface.

### 5.6 TypeSafe docs, as read on 2026-10-13 (live docs are the source of truth)

* **Limits and cost** (models page): 64k tokens per request for state plus all questions; 32k for
  state plus the single longest question; a Choice accepts up to 255 options; Score has 2 to 10
  levels; pricing is per input token only ($42 per billion tokens, output free); rate limits 100K
  tokens per second and 80 requests per second. Latency: "most queries complete in about 100 ms"
  (building guide); 0.16 to 0.31 s for a 182-option Choice (skill-suggestion cookbook). Our 500 ms
  decision floor is above the documented latency.
* **Guidance our questions violate.** Question IDs are not sent to the model, so `progress` and
  `completable` must be full questions (they are, but the ids carry meaning the model never
  sees). "Define Noul conditions precisely": "is the agent making progress" is the vague kind the
  docs warn about. "Multi-hop reasoning costs accuracy": `completable` asks for a plan over the
  menu. "Keep content and facts in the state, and put the judgments in the questions": our menu
  lines put live facts into criteria. "Include an 'other' or 'none of the above' option when the
  list might not cover every input": our action Choice has none. Current Jev "leans toward the
  option that comes first": we never shuffle menu order. "Jev will perform better on semantic
  questions than mathematical ones; counting is unreliable": we hand it signed dx/dy/dz offsets,
  30-block view rays and counters. "Accuracy falls as the state grows with content unrelated to
  the decision."
* **Score vs Noul.** `progress` fits a Score with concrete situation levels ("same action repeated
  3+ times with no delta" / "inventory changed but target not gained" / "target count rose"); use
  the level, not the fractional mean. For `completable`, a `none_of_these` option on the action
  Choice is the documented pattern, not a separate Noul.
* **Speculative fan-out is cheap.** "Send every question that uses the same state in one request";
  "adding more questions usually has little effect on response time"; the parallel-questions
  cookbook measured 13 questions in one call as 12.2x cheaper and 10x faster than 13 calls. So
  per-option "would this change the world" Nouls or per-direction exploration Scores cost only
  their own tokens.
* **No cookbook matches "pick the next bounded action from a menu with recent outcomes".** The
  closest are function calling (Choice over functions plus per-argument Nouls in one request),
  skill suggestion (182-option Choice plus gate Nouls, 0.30 abstain gate), and basic memory (prior
  outcomes as a state field, pruned by a relevance question).

Sources: https://arxiv.org/abs/2610.12126 , https://github.com/zju3dv/SuperNav ,
https://docs.typesafe.ai/llms.txt and the pages concepts/system-one, concepts/state, primitives,
primitives/choice, primitives/noul, primitives/score, confidence, patterns/fan-out,
patterns/confidence-routing, cookbooks/parallel_questions, cookbooks/skill_suggestion,
cookbooks/basic_memory, cookbooks/function_calling, models, model-jaggedness/jev-1.13.

---

## 6. Implementation status (2026-10-13)

R1 to R5 were implemented on `system-overhaul`, uncommitted. Offline checks: 60 Python tests pass
(`venv/Scripts/python tests/run_tests.py`), `agentloop_dryrun.js` and `verify_test.js` pass, and
one live Jev decision with the new question set took 326 ms (action Choice, danger Noul, progress
Score, 11 effect Nouls). A first draft replaced the `completable` Noul with 1 - P(none_of_these);
four live decisions showed that is a different question (0.76 to 0.87 for a movement-only menu on
a "get 3 logs" subtask, where the whole-menu Noul answered 0.12 to 0.14), so the Noul was kept. A fake-world run wrote decision records end to end through the Node
routes. Nothing has run in Minecraft yet. The design record is PLAN.md sections 5.2, 6, 7, 8 and 12.

| R | What changed | Where |
| --- | --- | --- |
| R1 | Repair before design, including retired actions; same name, full source, labelled outcomes with record ids, label hints; drafts compile under `<name>_draft`; `repair: true` resets counts; versions archived; budget of 3 repairs, then restore the best version or retire for good; same-design builds repair instead of duplicating; all kinds open to Jev, with learned actions per kind in its state; library pruning to `_archive/`; model fallback aligned and logged | `builder.py`, `primitiveRegistry.js`, `index.js`, `bridge.py`, `lib/outcomes.js`, `llm.py` |
| R2 | One decision record per action in `<ckpt>/decisions.jsonl` with id, goal id, state digest, Jev answer, label, delta, `jev_ms`, `act_ms`; completed-subtask traces kept and the closest one retrieved into builder prompts | `agentLoop.js`, `experience.py`, `traversal.py`, `builder.py` |
| R3 | `completable` Noul asked over the whole menu including trials (the proven-only framing was the F4 bug); `none_of_these` added to the action Choice for sampling only; full budget after retries and forced builds; two empty forced builds hand over to re-decomposition; every empty build records why; builder calls timed; builds on a background thread with one slot; place/eat/use final steps get their item subtree | `agentLoop.js`, `traversal.py`, `builder.py`, `decomposer.py`, `run_tree.py` |
| R4 | Post-goal review per lesson family writes Tried / Observed / Still doubt / Test next with record ids, archives the old file, maintains evidence-backed remedies (hypothesis, confirmed, disputed); builder loads the family lesson; curriculum sees "Test next" | `review.py`, `run_tree.py`, `curriculum.py`, `builder.py` |
| R5 | Jev reads a digested state (words, not offsets); options shuffled per request; progress is a 4-level Score; per-option effect Nouls filter sampling; the 500 ms floor is unchanged until latency is measured | `jevState.js`, `agentLoop.js`, `jevTraversal.js` |

Deliberately not done: the builder `CONTRACT` keeps its full API guidance until lesson files exist
and are shown to cover it; menu lines still carry live facts in the criteria; R6 to R8 are open.

### Follow-up after the 2026-10-13 evening runs

Two runs of "obtain a stone pickaxe" (20:21 and 20:27) showed two new problems, both now fixed and
tested offline (62 Python tests, both Node scripts):

* **Repairs landed after the node failed.** Builds took 26 to 64 s; both repaired actions registered
  7 to 33 s after their node was marked failed and were never tried. Traversal now grants up to two
  grace rounds when a build for the node is running or an action built for it is untested.
* **Leftover actions burned the budget.** When `none_of_these` won (187 of 243 decisions in the 20:27
  run) the loop executed a leftover option, mostly looks and turns that changed nothing. The loop now
  holds still without spending budget and requests a build at once, for up to 120 s per posting.

Still open from that review: drops not picked up after mining (needs a pickup-failure label and the
held item and block heights in the logged state), library skills that cannot be repaired
(`craft_crafting_table` counts only spruce planks), and purpose declarations that mis-score working
actions (`*_plank`; a generic crafter declared for crafting tables only).
