---
title: "LLM-driven Minecraft agents, 2024 to October 2026 (survey)"
source: multiple (arXiv ids below)
authors_org: various
published: 2023-2026
read: 2026-10-13
type: survey
code: mixed, listed per system
relevance: high
---

# Minecraft LLM agents, 2024 to October 2026

Compiled while evaluating the system-overhaul branch. Provenance caveat: Optimus-3 v2, LARM,
ROCKET-2 efficiency and the Andy-4 diamond-armour claims come from abstracts only; WISE, Luban,
MrSteve (official) and OmniJARVIS have no verified public code.

## Systems

| System (year) | Control | Planner | Memory / skills | Code |
| --- | --- | --- | --- | --- |
| Voyager (2023) | GPT-4 JS mineflayer programs | auto-curriculum, critic | code library, embedding retrieval | MineDojo/Voyager |
| DEPS (2023, 2302.01560) | goal-conditioned controllers | describe-explain-plan-select | none persistent | CraftJarvis/MC-Planner |
| GITM (2023) | structured text actions | LLM goal tree | text memory of successful action lists | OpenGVLab/GITM |
| JARVIS-1 (2023, 2311.05997) | STEVE-1 + craft/smelt | self-check (dry-run plan), self-explain | (task, situation) to plan memory | CraftJarvis/JARVIS-1 |
| MP5 (CVPR24, 2312.07472) | MineDojo actions | parser / planner / patroller loop | active perception | IranQin/MP5 |
| Odyssey (IJCAI25, 2407.15325) | mineflayer JS: 40 parameterised primitives + 183 compositional skills | planner-actor-critic on LLaMA-3-8B | embedding top-5 retrieval of skill descriptions | zju-vipa/Odyssey |
| Optimus-1 (NeurIPS24, 2408.03615) | STEVE-1 | knowledge-graph planner | multimodal experience pool; reflector COMPLETE / CONTINUE / REPLAN | JiuTian-VL/Optimus-1 |
| Optimus-2 (CVPR25, 2502.19902) | GOAP keyboard/mouse policy | MLLM planner | - | JiuTian-VL/Optimus-2 |
| Optimus-3 (2025, 2506.10357) | MoE VLA, System 1 / System 2 | task-routed experts | - | cybertronagent.github.io |
| MrSteve (ICLR25, 2411.06736) | STEVE-1 + VPT-Nav | LLM subgoals | Place Event Memory; count-based exploration (84% coverage) | unverified |
| ROCKET-1/2 (2503.02505) | mask-conditioned visuomotor | VLM points at targets | - | CraftJarvis/ROCKET-2 |
| JARVIS-VLA (ACL25, 2503.16365) | keyboard/mouse VLA | none | - | CraftJarvis/JarvisVLA |
| MineStudio / MCU (ICML25, 2412.18293, 2310.08367) | any | benchmark toolkit, 3,452 atomic tasks | - | CraftJarvis/MineStudio |
| Plancraft (COLM25, 2412.21033) | inventory GUI | crafting only | - | gautierdag/plancraft |
| Mindcraft (2024 to 26, 2504.17950) | mineflayer discrete `!command(args)` menu; `!newAction` code off by default | self-prompted goals | `skills.js` JSDoc library, `!rememberHere`, embedding example selection, Andy-4 local models | mindcraft-bots/mindcraft |
| Parallel Planning-Acting (2025/26, 2503.03505) | mineflayer skill API, recursive | planner + actor threads, single-slot buffer, interrupt flag | weighted prerequisite DAG, team JSON memory | in zju-vipa/Odyssey |
| MineEvolve (Mar 2026, 2603.13131) | STEVE-1 | LLM subgoals; suffix-only replanning | schema-validated skill + remedy store | xzw-ustc/MC-MineEvolve |
| Echo (Apr 2026, 2604.05533) | MLLM plan + executor | analogy retrieval | structured task descriptors, success-only | CatworldLee/Echo |
| WISE (Jun 2026, 2606.12852) | STEVE-1 + VPT-Nav | opportunistic scheduler | causal event graph; quadtree / frontier / Voronoi exploration | none |
| MineExplorer (2026, 2605.30931) | frames, 0.1 s steps | benchmark | - | meituan-longcat/MineExplorer |
| MC-EvoBench (Oct 2026, 2610.13829) | MineRL | benchmark, task-state graph | - | xzw-ustc/mc-bench-open-source |

Outside Minecraft but relevant: RESKILL (2609.15684) and SkillRevise (2606.01139) patch skills
by attributing failures instead of rewriting; Latent Bridge (2606.24470) has a slow VLM steer a
millisecond-latency fast model; the DOOM-1.3M study (2604.07385) measures LLMs at 646 ms to 13 s
per decision vs 31 ms for a small model.

## What applies to this project

1. **Patch, don't retire-and-rewrite** (RESKILL, SkillRevise, MineEvolve). Maps to: builder,
   `primitiveRegistry.js:25` retire rule. Addresses: F1, F6. Proposal: typed failure label plus
   old code plus trace to the builder for a local edit; per-skill repair budget; keep the best
   version (R1).
2. **Structured skill and remedy store with verification conditions** (MineEvolve: skills carry
   `trigger_context, preconditions, steps, verification, confidence, supporting_feedback`;
   remedies carry `failure_type, repair_action, scope`; hard-task SR 25.1 to 35.0 over 400
   episodes vs 29.6 for free-text reflection). Maps to: library sidecars. Proposal: extend
   sidecars with trigger context and remedies; retrieve under a token budget (R4).
3. **Replan only the unfinished suffix** (MineEvolve, JARVIS-1 self-check). Maps to:
   `traversal.py` redecompose. Proposal: freeze the done prefix, regenerate the failing subtree
   after a dry-run inventory simulation.
4. **Count-based exploration plus landmark memory** (MrSteve, WISE, Mindcraft `!rememberHere`).
   Maps to: `observe.js`. Addresses: weak exploration, nothing remembers placed tables. Proposal:
   landmark table and visitation grid in the snapshot, observation-side only (R6). MineExplorer:
   navigation is the dominant failure class, so measure it separately.
5. **Slow planner and fast actor concurrently through a one-slot buffer with an interrupt flag**
   (Parallel Planning-Acting, Optimus-3, Latent Bridge). Maps to: Python poll loop. Addresses:
   synchronous builds stall trigger handling. Proposal: builder in a worker thread (R3).
6. **Retrieve skills by description embedding keyed on situation** (Voyager, Odyssey, JARVIS-1,
   Echo). Maps to: menu construction. Proposal: show Jev only the top-k relevant learned actions
   as the library grows.
7. **Periodic reflector with three verdicts** (Optimus-1 COMPLETE / CONTINUE / REPLAN). Maps to:
   Jev loop. Proposal: one Choice every N actions; "stuck" feeds the remedy store.
8. **Parameterised primitives plus recursive composition over a prerequisite DAG** (Odyssey,
   Parallel Planning-Acting, Mindcraft). `conflicts: actions are learned, not hand-written`. The
   compatible version: let new primitives call *proven learned* primitives (R7).

## Metrics the field uses

- Tech-tree ladder (Optimus-1, MineEvolve): 67 to 70 tasks over wood, stone, iron, gold,
  redstone, diamond, armour; success rate, average steps, average time, 30 or more seeds.
- MC-EvoBench transfer protocol: reset world, carry only the declared learned artefact, compare
  no-prior / related / unrelated / shuffled experience at matched step budgets, 5 seeds; report
  stages passed, weighted DAG-edge progress, milestones. Their finding: reference agents showed no
  stable transfer because early wood and crafting bottlenecks masked everything downstream.
- Continual curves: Echo Success@0 to 10 and 0 to 30; Voyager milestones vs iterations.
- Exploration: map coverage and revisit counts (MrSteve); distinct items, recipes, advancements,
  distance (Odyssey); return-to-location success (WISE).
- Diagnostics: MineExplorer failure taxonomy (navigation, gathering, action, goal
  misidentification); internally: patches vs rewrites per skill, builder refusals, skill reuse
  count, Jev latency, planner-blocking time.

## Sources

arXiv ids in the table; https://github.com/mindcraft-bots/mindcraft ; https://github.com/CraftJarvis ;
https://huggingface.co/Sweaterdog/Andy-4-base
