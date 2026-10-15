---
title: "EmbodiedCLBench: Evaluating Continual Learning for Self-Evolving Embodied Agents"
source: arXiv 2610.13021 (v1, 9 Oct 2026)
authors_org: Peking University / BIGAI / HKU (PKU-VaLuE-Lab)
published: 2026-10-09
read: 2026-10-13
type: paper + benchmark
code: full (github.com/PKU-VaLuE-Lab/EmbodiedCLBench; dataset on Hugging Face PKU-VaLuE-Lab/EmbodiedCLBench)
relevance: high (protocol and metrics)
---

# EmbodiedCLBench

## What it is

A benchmark that measures continual learning in an embodied setting as **compositional
generalisation**: an agent first completes basic household tasks, then must solve unseen advanced
tasks that recombine those skills. Evaluation is offline over state-transition graphs built from
the TongSim simulator, with a first-person image and a candidate-action list at every state.

## What it says

- **Structure.** Atomic action = walk-to / look-at / operate (3 steps). Basic task = 2 atomic
  actions (6-step shortest path), 168 tasks over six categories. Advanced task = 4 atomic actions
  from 2 to 4 categories (12 steps), 100 tasks. Budget = shortest path + 3 steps at test, + 6
  during learning. Candidate actions at each state are optimal, suboptimal (executable but off
  the shortest path) and invalid (not executable now), written so none stands out.
- **Main result** (Table 1; harnesses OpenClaw, Claude Code, Codex, Hermes; models Qwen3.8-Flash,
  GPT-5.6-Terra): basic zero-shot about 50%; advanced zero-shot 3 to 12%; after learning two
  basic tasks, In-Context Learning 16 to 52%, Skill Learning 22 to 38%. Neither method wins
  consistently.
- **Complexity** (Fig 4): the gain over zero-shot stays near 30 points as atomic actions rise
  from 3 to 6, while zero-shot falls from 28% to 4%.
- **Samples** (Fig 5): one learning sample gives nearly all of the gain (8 to 10% up to 28 to
  40%); k = 2 to 6 adds nothing consistent. "Current agents cannot extract sufficiently distinct
  information from additional learning samples."
- **Error analysis** (Fig 6): experience cuts invalid-action selections by 13 to 20 points and
  raises optimal ones; that is where the success comes from. Skill summaries shift behaviour less
  than trajectories.
- **Budget** (Fig 7): at 0 extra steps zero-shot is 0%, ICL 15%, Skill 7%; the gap persists at
  6 extra steps.

## What the code contains

- ICL is a **resumed session**, not pasted trajectories: run the basic tasks, export a checkpoint,
  resume with "Use the preceding interactions as learning examples ... They are reference
  experience, not the current task, and their ending does not mean that the new task is
  complete." Compaction drops only duplicate task and choice blocks
  (`frameworks/hermes_impl/task_history_compact.py`).
- Skill Learning is one tool-less turn: "write a skill description in Markdown format ... at or
  below 1000 English words", injected into a fresh session as "reference guidance, not a task
  episode or a completion result. The current task goal, current image, previous feedback, and
  listed choices remain the source of truth." Prompts under
  `public/eval/src/tongbench_eval/cli/framework/dialogue_runtime/parts/`.
- Feedback strings are precondition-specific: "could not be executed because the required object
  was not currently in view / reachable / being held"; "was allowed but made no progress."

## What applies to this project

- **Learning metrics.** Maps to: metrics / `scripts/progress_report.py`. Addresses: nothing is
  measured across runs. Proposal: invalid-action rate (failed or NO EFFECT outcomes over all
  actions), invalid-build rate, success within budget, with and without the carried library
  (EVALUATION.md R8).
- **One exemplar is enough.** Maps to: builder prompt and Jev state. Proposal: retrieve the single
  most similar prior subtask trace, not many (R2).
- **Compositional hold-out as the transfer test.** Maps to: decomposer / evaluation protocol.
  Proposal: hold out goals whose trees recombine 2 to 4 proven subtasks and measure success at
  shortest + 3 nodes' worth of budget.
- **Precondition-specific outcome strings.** Maps to: Jev loop outcome records and generated
  `execute()` return strings. Addresses: F1 (repair cannot see why). Proposal: standard failure
  labels (not in view / not reachable / not holding / no progress / API misuse).
- **"Reference guidance, not ground truth" framing** for any injected lesson text. Maps to:
  builder prompt (R4).
- **`proven_only` library reload is the right instinct** (fewer invalid actions is the gain).

## What does not apply

Offline graph evaluation with pre-authored candidate actions; our menu is generated live and the
world is open. Household scenes and image observations.

## Open questions

- Their harnesses are coding agents with a session; ours is a single prompt-and-parse builder.
  Is the gain from the session (model decides when to look back) or from the content?

## Sources

https://arxiv.org/abs/2610.13021 , https://arxiv.org/html/2610.13021v1 ,
https://pku-value-lab.github.io/EmbodiedCLBench-homepage ,
https://github.com/PKU-VaLuE-Lab/EmbodiedCLBench ,
https://huggingface.co/datasets/PKU-VaLuE-Lab/EmbodiedCLBench
