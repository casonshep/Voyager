# Research history

A running log of papers, codebases and docs read for this project, what each one says, and
which parts apply to the self-learning Minecraft agent. One file per source under `papers/`;
cross-cutting lessons and their adoption status live in `insights.md`.

## How to add a batch

1. Copy `TEMPLATE.md` to `papers/YYYY-MM-DD-short-name.md` and fill it in. Keep "what it says"
   separate from "what it means for us"; quote numbers, name the ablation, and say whether code is
   public and what the repo actually contains.
2. Add each new insight to `insights.md` with its source, the failure or component it maps to,
   and a status (`proposed`, `adopted <commit>`, `rejected <why>`, `superseded`).
3. Add a row to the index below.
4. If the batch produced a full evaluation, link it (the first one is [EVALUATION.md](../../EVALUATION.md)).

Conventions: project constraints come first (Jev never sees code; actions are learned, not
hand-written; see PLAN.md). An insight that conflicts with a constraint is still logged, marked
`conflicts: <constraint>`, so the trade-off is on record.

## Index

| Read | Source | Type | Code public? | Most useful for | File |
| --- | --- | --- | --- | --- | --- |
| 2026-10-13 | AgentGarten: Code Worlds for Evolving Agents (arXiv 2610.12374) | paper | renderer only; agent loop unreleased | post-goal playbooks, lessons as claims with evidence | [papers/2026-10-13-agentgarten.md](papers/2026-10-13-agentgarten.md) |
| 2026-10-13 | EmbodiedCLBench (arXiv 2610.13021) | paper + benchmark | yes, full harness and prompts | learning metrics, ICL vs skill-summary formats | [papers/2026-10-13-embodiedclbench.md](papers/2026-10-13-embodiedclbench.md) |
| 2026-10-13 | SuperNav (arXiv 2610.12126) | paper + harness | yes, skill Markdown public | Markdown skills loaded on demand, tool return design | [papers/2026-10-13-supernav.md](papers/2026-10-13-supernav.md) |
| 2026-10-13 | Learn2Play Bench (arXiv 2610.08215) | paper + benchmark | games only; no prompts | raw history beats distilled rules, over-exploitation, harness cost | [papers/2026-10-13-learn2play.md](papers/2026-10-13-learn2play.md) |
| 2026-10-13 | Minecraft LLM agents 2024 to 2026 (survey) | survey | mixed, listed per system | skill repair, remedy stores, exploration memory, benchmarks | [papers/2026-10-13-minecraft-agents-survey.md](papers/2026-10-13-minecraft-agents-survey.md) |
| 2026-10-13 | TypeSafe docs (docs.typesafe.ai) | docs | n/a | Jev limits, question wording, Score vs Noul, fan-out | [papers/2026-10-13-typesafe-docs.md](papers/2026-10-13-typesafe-docs.md) |

## Evaluations

| Date | Branch / commit | File |
| --- | --- | --- |
| 2026-10-13 | `system-overhaul` @ 6a4d842 | [EVALUATION.md](../../EVALUATION.md) |
