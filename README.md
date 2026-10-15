# Voyager: An Open-Ended Embodied Agent with Large Language Models
<div align="center">

[[Website]](https://voyager.minedojo.org/)
[[Arxiv]](https://arxiv.org/abs/2305.16291)
[[PDF]](https://voyager.minedojo.org/assets/documents/voyager.pdf)
[[Tweet]](https://twitter.com/DrJimFan/status/1662115266933972993?s=20)

[![Python Version](https://img.shields.io/badge/Python-3.9-blue.svg)](https://github.com/MineDojo/Voyager)
[![GitHub license](https://img.shields.io/github/license/MineDojo/Voyager)](https://github.com/MineDojo/Voyager/blob/main/LICENSE)
______________________________________________________________________


https://github.com/MineDojo/Voyager/assets/25460983/ce29f45b-43a5-4399-8fd8-5dd105fd64f2

![](images/pull.png)


</div>

We introduce Voyager, the first LLM-powered embodied lifelong learning agent
in Minecraft that continuously explores the world, acquires diverse skills, and
makes novel discoveries without human intervention. Voyager consists of three
key components: 1) an automatic curriculum that maximizes exploration, 2) an
ever-growing skill library of executable code for storing and retrieving complex
behaviors, and 3) a new iterative prompting mechanism that incorporates environment
feedback, execution errors, and self-verification for program improvement.
Voyager interacts with GPT-4 via blackbox queries, which bypasses the need for
model parameter fine-tuning. The skills developed by Voyager are temporally
extended, interpretable, and compositional, which compounds the agent’s abilities
rapidly and alleviates catastrophic forgetting. Empirically, Voyager shows
strong in-context lifelong learning capability and exhibits exceptional proficiency
in playing Minecraft. It obtains 3.3× more unique items, travels 2.3× longer
distances, and unlocks key tech tree milestones up to 15.3× faster than prior SOTA.
Voyager is able to utilize the learned skill library in a new Minecraft world to
solve novel tasks from scratch, while other techniques struggle to generalize.

In this repo, we provide Voyager code. This codebase is under [MIT License](LICENSE).

# Installation
Voyager requires Python ≥ 3.9 and Node.js ≥ 16.13.0. We have tested on Ubuntu 20.04, Windows 11, and macOS. You need to follow the instructions below to install Voyager.

## Python Install
```
git clone https://github.com/MineDojo/Voyager
cd Voyager
pip install -e .
```

## Node.js Install
In addition to the Python dependencies, you need to install the following Node.js packages:
```
cd voyager/env/mineflayer
npm install -g npx
npm install
cd mineflayer-collectblock
npx tsc
cd ..
npm install
```

## Minecraft Instance Install

Voyager depends on Minecraft game. You need to install Minecraft game and set up a Minecraft instance.

Follow the instructions in [Minecraft Login Tutorial](installation/minecraft_instance_install.md) to set up your Minecraft Instance.

## Fabric Mods Install

You need to install fabric mods to support all the features in Voyager. Remember to use the correct Fabric version of all the mods. 

Follow the instructions in [Fabric Mods Install](installation/fabric_mods_install.md) to install the mods.

# Getting Started

This fork replaces the GPT-writes-programs loop with three systems (design
record: [PLAN.md](PLAN.md)):

| System | Where | What it does |
| --- | --- | --- |
| Decomposer | `voyager/tree/decomposer.py` | Turns a GPT goal ("obtain a stone pickaxe") into a tree of subtasks. Every node has a code-checkable `VerifySpec` (`has_item`, `near_block`, `has_tool_tier`, ...). Recipes, tool tiers, furnaces and fuel come from `GET /knowledge` (minecraft-data plus `lib/mcKnowledge.js`). One node per item, sized for whole-tree demand; a node's `done` flag is persisted the moment it verifies. |
| Jev action loop | `voyager/env/mineflayer/lib/agentLoop.js` | Runs one subtask at a time. Each decision: observe (`lib/observe.js`), build a menu, digest the snapshot into words (`lib/jevState.js`), ask Jev one request (action Choice over the shuffled menu plus `none_of_these`, a `completable` Noul over the whole menu, a danger Noul, a progress Score, and per-option "would this change anything" Nouls), execute one bounded action, label the outcome (`lib/outcomes.js`), log the record to `decisions.jsonl`, check the spec. The starting menu is movement and looking only (`lib/baseActions.js`). |
| Action builder | `voyager/tree/builder.py` | When the `completable` Noul stays below 0.5 over a 10-action window, or on stuck routing, the builder first **repairs** a failing action built for the subtask in place (same name, full source, every labelled outcome, earlier versions; up to 3 repairs, then the best version that ever worked is restored). Otherwise Jev picks the kind and target of the missing action and GPT writes one mineflayer primitive (`menu` / `execute` / optional `observe`). Drafts are checked, registered as trials, and persisted with versions under the library. Prompts carry the closest completed trace and the family lesson. Builds run on a background thread. |
| Post-goal review | `voyager/tree/review.py` | After each goal, a fresh-context GPT call per lesson family reads that goal's decision records and rewrites `lessons/<family>.md` (Tried / Observed / Still doubt / Test next, each bullet citing record ids) and the remedy store. The builder reads the lessons; the curriculum reads the "Test next" items. |
| Traversal | `voyager/tree/traversal.py` | Post-order over the tree. Routes stuck and budget triggers: build, retry with half the budget, re-decompose the node, or fail the chain and ask GPT for a new goal. |

Jev never sees code: the one-line description a primitive's `menu()` writes
is its whole interface.

## Run

Keys go in `.env` (`OPENAI_API_KEY`, `TYPESAFE_API_KEY`; the loop falls back to
plain code without Jev). Open a Minecraft world to LAN and note the port.

```bash
venv/Scripts/python run_tree.py --mc-port 54321                              # GPT proposes goals; a fresh run starts with "Obtain 1 wood log"
venv/Scripts/python run_tree.py --mc-port 54321 --goal "obtain a wooden pickaxe" --max-goals 1
venv/Scripts/python run_tree.py --resume ckpt_20261002_120000                 # reload the tree in progress and saved primitives
venv/Scripts/python run_tree.py --help
```

Checkpoints: `<ckpt>/tree/<goal_id>.json` (the tree with done flags and
traces), `<ckpt>/decisions.jsonl` (one record per action: what the bot saw,
Jev's answer, the action, its labelled outcome, timings),
`<ckpt>/typesafe/*.jsonl` (Jev requests, builds, repairs, reviews),
`<ckpt>/curriculum/` (goals completed and failed). The shared library
(`--library`, default `skill_library/tree`) holds `<name>.js` + `.json`
(learned actions), `versions/` (earlier versions of repaired actions),
`traces/` (records of completed subtasks), `lessons/` (review output), and
`_archive/` (pruned duplicates).

## Offline checks (no Minecraft, no keys)

```bash
venv/Scripts/python tests/run_tests.py                       # tree, traversal and builder tests against a fake bridge
cd voyager/env/mineflayer
node scripts/verify_test.js                                  # every VerifySpec predicate against a canned plains world
node scripts/agentloop_dryrun.js                             # snapshot + menu; one real Jev decision when TYPESAFE_API_KEY is exported
FAKE_WORLD=1 node index.js 3999                              # serve /knowledge and /agent/* over the canned bot (it cannot move or mine)
```

The Node HTTP surface (`/knowledge`, `/agent/subtask`, `/agent/status`,
`/agent/verify`, `/agent/primitive`, `/agent/stop`) and the snapshot and
tree schemas are specified in PLAN.md sections 1, 3 and 7.

# FAQ
If you have any questions, please check our [FAQ](FAQ.md) first before opening an issue.

# Paper and Citation

If you find our work useful, please consider citing us! 

```bibtex
@article{wang2023voyager,
  title   = {Voyager: An Open-Ended Embodied Agent with Large Language Models},
  author  = {Guanzhi Wang and Yuqi Xie and Yunfan Jiang and Ajay Mandlekar and Chaowei Xiao and Yuke Zhu and Linxi Fan and Anima Anandkumar},
  year    = {2023},
  journal = {arXiv preprint arXiv: Arxiv-2305.16291}
}
```

Disclaimer: This project is strictly for research purposes, and not an official product from NVIDIA.
