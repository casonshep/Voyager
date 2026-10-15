# Insights ledger

Cross-source lessons that apply to this project, with where they come from, what they map to,
and whether they have been adopted. Add new rows at the bottom; never delete a row, change its
status instead. Status values: `proposed`, `implemented <date>, uncommitted`, `adopted <commit>`, `rejected <why>`,
`superseded by <row>`. Failure codes (F1 to F7) and recommendations (R1 to R8) refer to
[EVALUATION.md](../../EVALUATION.md) (2026-10-13).

| # | Insight | Sources | Maps to | Addresses | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | Keep raw per-decision histories; distilled rules must stay revisable from them | Learn2Play (MEMORY wins; B.1.3 case), AgentGarten (evidence IDs) | Node agent loop logging, library | F5, F1 | implemented 2026-10-13, uncommitted (R2) |
| 2 | Repair a failing skill with its full code, outcomes and a typed failure label; keep the best version under a repair budget; never retire-and-rewrite | RESKILL, SkillRevise, MineEvolve, our own logs (7x `place_crafting_table`) | builder, primitiveRegistry retire rule | F1, F6 | implemented 2026-10-13, uncommitted (R1) |
| 3 | The author model matters; gpt-4o-mini ignored its own contract seven times (CORRECTED: only the pre-14:27 Oct 3 runs could have used a weak model; later runs used gpt-6.1-sol and still failed, see row 30) | our logs; Learn2Play backbone table | `voyager/tree/llm.py:16` | F1 | superseded by row 30 |
| 4 | Do not hide the correct action kind from Jev to prevent rewrites; fix the rewrite path instead | our logs (ckpt_150551 cobblestone) | `builder._open_kinds` | F2 | implemented 2026-10-13, uncommitted (R1) |
| 5 | Builder trigger: a `none_of_these` option on the action Choice beats a multi-hop Noul over a proven-only menu (REVISED: live Jev showed P(none) is step-level, not plan-level; the fix that held is asking `completable` over the whole menu, with `none_of_these` kept for sampling) | TypeSafe docs; live check 2026-10-13 | `agentLoop.decide` | F4 | implemented 2026-10-13, uncommitted (R3) |
| 6 | Every `None` from the builder must record why; time every builder LLM call | our logs (F3 loop) | builder, traversal | F3 | implemented 2026-10-13, uncommitted (R3) |
| 7 | Do not carry a halved budget into forced-build re-posts; do not reset exhaustions after a forced build | our logs | `traversal.py:324-327, 423` | F3 | implemented 2026-10-13, uncommitted (R3) |
| 8 | Post-goal review writes per-family lessons with tried / observed / still doubt / test next, as claims with evidence IDs, in fresh context | AgentGarten | new review step, library | F5, F6 | implemented 2026-10-13, uncommitted (R4) |
| 9 | Structured remedy store `{failure_type, repair_action, scope, confidence, evidence}` beats free-text reflection | MineEvolve (35.0 vs 29.6) | library sidecars | F1 | implemented 2026-10-13, uncommitted (R4) |
| 10 | Skills as Markdown loaded on demand are worth more than interface design; keep them out of the fast model's view | SuperNav ablation (35.0 vs 68.3) | builder CONTRACT, decomposer prompts | - | implemented 2026-10-13, uncommitted (R4) |
| 11 | Injected lessons are "reference guidance, not ground truth"; raw traces remain the source of truth | EmbodiedCLBench prompts, AgentGarten task files | builder prompt | - | implemented 2026-10-13, uncommitted (R4) |
| 12 | One retrieved exemplar gives most of the gain; more samples do not help | EmbodiedCLBench Fig 5 | builder prompt retrieval | - | implemented 2026-10-13, uncommitted (R2) |
| 13 | Learning shows up as fewer invalid actions; measure invalid-action rate and invalid-build rate across runs | EmbodiedCLBench Fig 6 | metrics | - | implemented 2026-10-13, uncommitted (R8) |
| 14 | Agents over-exploit; measure attempt-to-attempt action-sequence similarity and expose it to the decision | Learn2Play (0.66 vs 0.54) | Jev loop sampling, metrics | F1 | partly implemented 2026-10-13 (records allow measuring it; not yet a state feature) (R5, R8) |
| 15 | Precondition-specific outcome strings (not in view / not reachable / not holding / no progress / API misuse) | EmbodiedCLBench feedback design | generated `execute()` returns, action records | F1 | implemented 2026-10-13, uncommitted (R1) |
| 16 | Every action record carries the post-action observation delta | SuperNav tool design | `agentLoop` records | - | implemented 2026-10-13, uncommitted (R2) |
| 17 | find-X subtasks follow search / recheck / recover / complete with a recovery ladder in code and an evidence gate for completion | SuperNav skill rules | locate nodes, fallback action | - | proposed |
| 18 | Observation-side landmark table and visitation grid; horizon directions labelled visited / new | MrSteve, WISE, Mindcraft, MineExplorer | `observe.js` | weak exploration | proposed (R6) |
| 19 | Slow planner / builder runs concurrently with the fast loop through a one-slot buffer | Parallel Planning-Acting, Optimus-3, Latent Bridge | Python poll loop | trigger stalls during builds | implemented 2026-10-13, uncommitted (R3) |
| 20 | Replan only the failing subtree after a dry-run inventory simulation | MineEvolve, JARVIS-1 | traversal redecompose | - | proposed |
| 21 | Dry-run a drafted primitive in the fake world before it becomes an in-game trial | JARVIS-1 self-check, Learn2Play harness finding | builder, `scripts/fakeWorld.js` | F1 cost | proposed (R7) |
| 22 | New primitives may call proven learned primitives (composition) | Odyssey, Parallel Planning-Acting | primitive `loop` helpers | 54 craft actions written | proposed (R7) |
| 23 | Hand-verified parameterised primitive set (Odyssey 40, Mindcraft skills.js) | survey | base actions | - | rejected: conflicts with "actions are learned, not hand-written" |
| 24 | Jev question hygiene: precise Nouls, Score with situation levels, shuffled options, pre-digested facts, speculative fan-out in one request | TypeSafe docs | `agentLoop.decide`, `observe.js` | F4 | implemented 2026-10-13, uncommitted (R5) |
| 25 | Measure Jev latency per decision before tuning the 500 ms floor; docs say ~100 ms typical | TypeSafe docs | `agentLoop.js:16` | real-time goal | partly implemented 2026-10-13 (latency logged per decision; floor not yet tuned) (R2) |
| 26 | Fixed ladder benchmark with no-library vs carried-library, 5 seeds, matched budgets; fix the wood-to-stone bottleneck before measuring transfer | MC-EvoBench, EmbodiedCLBench | metrics, `scripts/progress_report.py` | - | proposed (R8) |
| 27 | Reject an empty item plan with a final step in decomposer validation | our logs (ckpt_145002 one-node tree) | decomposer plan validation | F7 | implemented 2026-10-13, uncommitted (R3) |
| 28 | Periodic reflector Choice: continue / done / stuck, with stuck feeding the remedy store | Optimus-1 | Jev loop | - | proposed |
| 29 | Dedupe the library on (kind, target, purpose spec); drop never-tried entries after N runs | our logs (approach_nearby_logs x7) | builder reload | F6 | implemented 2026-10-13, uncommitted (R1) |
| 30 | Repair must reach actions Node already retired; the old repair window (between failures 2 and 3) let repair fire 4 times in 163 primitives, each time minting a new name | our logs + code reading (`_failing_trial` skipped retired) | builder repair path | F1, F6 | implemented 2026-10-13, uncommitted (R1) |
| 31 | Do not give up on a node while a build for it is running or its new action is untested: builds take 26 to 64 s and both 2026-10-13 repairs arrived after their node failed | our logs (ckpt_20261013_202150, _202725) | traversal exhaustion handling | repairs never tried | implemented 2026-10-13, uncommitted |
| 32 | When the fast model says nothing on the menu helps, hold still and ask for a build instead of executing a low-probability leftover (a human player waits too) | our logs (none_of_these top in 187 of 243 decisions; 42% of actions changed nothing) | Jev loop | budget burned while waiting for builds | implemented 2026-10-13, uncommitted |
| 33 | Mining succeeded but the drop was never collected in every failing mine attempt (ice, stone under dirt); needs its own outcome label and the held item and block heights in the logged state | our logs | outcome labels, decision digest, review prompt | acquisition failures | proposed |
| 34 | A proven library skill can be invisible in a new world (`craft_crafting_table` counts only spruce planks): library skills must be repairable, not only per-run builds | our logs; Learn2Play transfer finding ("remember what worked but not why") | builder repair path, library | transfer | proposed |
| 35 | Validate declared purpose names against the game's item list and credit gains on the current step's target, so working actions are not scored as failures | our logs (`*_plank`, generic crafter declared for crafting_table) | registry, agent loop scoring | wrong library stats | proposed |
