---
title: "AgentGarten: Code Worlds for Evolving Agents"
source: arXiv 2610.12374 (v2, 10 Oct 2026)
authors_org: MirroS Technical Report
published: 2026-10-08
read: 2026-10-13
type: paper
code: partial (github.com/MirroS-Lab/AgentGarten holds the neural renderer only; the agent practice loop, task files and playbook prompts are unreleased per the README TODO)
relevance: high (section 5 only)
---

# AgentGarten: Code Worlds for Evolving Agents

## What it is

A framework that couples simulators and game engines with a shared neural video renderer so
agents see photoreal first-person frames of a code-defined world. Most of the paper is the renderer
(Adversarial Forcing distillation, exact replay, real-time inference). Section 5 is an agent
practice loop with written playbooks, which is the part that applies to us.

## What it says

- **Round loop** (Sec 5.3, Fig 11): "A round begins with a task file that states the goal, the
  available actions, and the limits, but no solution. One or more agents then play in parallel,
  each within a fixed budget of steps or simulated time. Afterwards each agent writes a playbook
  that records what it tried, what it observed, what it still doubts, and what to test next.
  Playbooks are archived, and the next round's agents, started in fresh conversations, receive the
  task file and the playbooks of earlier rounds." Nothing is merged or pruned.
- **Hide-and-seek** (Sec 5.2): agents act by submitting short Python programs
  (`def policy(): step(turn=-1, steps=2); pull(1); goto(180, 2)`) and then observe. "Each role
  keeps its own playbook, organized as a library of short skill files, Markdown notes that may
  contain code, with one lesson per file" (`check_shelter.md`, `object_control.md`,
  `ramp_landing.md`). After each round the role "reviews its own action programs and the visual
  evidence it was permitted to see, identifies failures, and adds skill files." Failed attempts
  count too.
- **Task file format** (Appendix D of the mirros.ai report PDF): title; goal and observation limits
  in prose; a table `| Action | Fixed duration | Meaning |`; optional cue section; "Evaluation and
  experience" with the score rule but no solution. Every task file carries an epistemic block:
  "distinguish uncertain visual evidence from conclusions. Record evidence IDs, failures and
  hypotheses in your playbook. An inherited playbook is another attempt's claims, not ground
  truth." and "do not mistake a guess about an unseen area for an observation."
- **Why it differs from Voyager / Reflexion** (Sec 6): "playbooks pass between independent agents
  in fresh conversations, so each lesson has to be stated well enough for another reader to check
  it." Sec 7: a world can be reset and replayed exactly, so "a lesson can be tested before it is
  passed on."
- **Evidence** (Tables 3, 4): shelters built by round 4, ramps used by round 10 (RL baseline
  needed about 25M and 100M episodes; the paper says this is not a sample-efficiency ratio). Four
  other worlds, four rounds, one episode each: companion dog 13, 14, 19, 19; one-lane bridge 71,
  68, 45, 41 s; herding 60, 90.1, 87.6, 88.3; quarry loader 30, 0, 30, 90.9. No no-playbook
  ablation. The agent is a frontier model (ref [32], GPT-6 Astra).

## What the code contains

Renderer only (`wm/` package, Apache-2.0). No agent, playbook, task or prompt files. Full task
files are in Appendix D of https://mirros.ai/report/agent-garten.pdf.

## What applies to this project

- **Post-goal review that writes lessons.** Maps to: a new review step after `traversal.run()`
  in `run_tree.py`. Addresses: F5 (no raw history), F6 (library is counts only). Proposal: one
  fresh-context GPT call reads the goal's raw traces and writes per-family Markdown with the four
  required sections tried / observed / still doubt / test next (EVALUATION.md R4).
- **Lessons are claims with evidence IDs, promoted only after replay confirms them.** Maps to:
  library sidecars and lesson files. Addresses: F1 (rewrites that ignore evidence). Proposal: every
  lesson and primitive cites decision-record ids (needs R2); mark anything with under two
  supporting outcomes as a hypothesis.
- **"Test next" feeds the planner; "still doubt" feeds the builder.** Maps to: decomposer candidate
  subtasks; builder prompt assumptions the new `execute()` must check.
- **Fresh-context inheritance.** Maps to: builder and reviewer prompts. Proposal: start them from
  the task description plus archived lessons, never a transcript, so lessons must be stated well
  enough for another reader.
- **Task-file discipline** (goal, action table with cost and meaning, budget, evaluation rule, no
  solution) is nearly what Jev's menu plus the VerifySpec already are. Keep it that way; add a
  per-action cost or duration to menu lines.
- **Open-loop guard.** Keep `execute()` bodies short and re-observe between batches. Our 20 s
  action timeout approximates this already.

## What does not apply

The renderer, geometry conditioning, distillation and inference engineering (Sections 2 to 4).
We observe structured state, not frames.

## Open questions

- Does a playbook written by a cheap model help a cheap builder, or does the review need the
  strong model? (Their agent is frontier-class.)
- How to prune or merge playbooks over many goals; the paper leaves this open (Sec 7).

## Sources

https://arxiv.org/abs/2610.12374 , https://arxiv.org/html/2610.12374 ,
https://mirros.ai/report/agent-garten.pdf , https://github.com/MirroS-Lab/AgentGarten ,
https://mirros-lab.github.io/agent-garten , https://mirros.ai/blog/worlds-for-evolving-agents
