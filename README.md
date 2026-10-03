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
Voyager uses OpenAI's GPT-4 as the language model. You need to have an OpenAI API key to use Voyager. You can get one from [here](https://platform.openai.com/account/api-keys).

After the installation process, you can run Voyager by:
```python
from voyager import Voyager

# You can also use mc_port instead of azure_login, but azure_login is highly recommended
azure_login = {
    "client_id": "YOUR_CLIENT_ID",
    "redirect_url": "https://127.0.0.1/auth-response",
    "secret_value": "[OPTIONAL] YOUR_SECRET_VALUE",
    "version": "fabric-loader-0.14.18-1.19", # the version Voyager is tested on
}
openai_api_key = "YOUR_API_KEY"

voyager = Voyager(
    azure_login=azure_login,
    openai_api_key=openai_api_key,
)

# start lifelong learning
voyager.learn()
```

* If you are running with `Azure Login` for the first time, it will ask you to follow the command line instruction to generate a config file.
* For `Azure Login`, you also need to select the world and open the world to LAN by yourself. After you run `voyager.learn()` the game will pop up soon, you need to:
  1. Select `Singleplayer` and press `Create New World`.
  2. Set Game Mode to `Creative` and Difficulty to `Peaceful`.
  3. After the world is created, press `Esc` key and press `Open to LAN`.
  4. Select `Allow cheats: ON` and press `Start LAN World`. You will see the bot join the world soon. 

# Resume from a checkpoint during learning

If you stop the learning process and want to resume from a checkpoint later, you can instantiate Voyager by:
```python
from voyager import Voyager

voyager = Voyager(
    azure_login=azure_login,
    openai_api_key=openai_api_key,
    ckpt_dir="YOUR_CKPT_DIR",
    resume=True,
)
```

# Run Voyager for a specific task with a learned skill library

If you want to run Voyager for a specific task with a learned skill library, you should first pass the skill library directory to Voyager:
```python
from voyager import Voyager

# First instantiate Voyager with skill_library_dir.
voyager = Voyager(
    azure_login=azure_login,
    openai_api_key=openai_api_key,
    skill_library_dir="./skill_library/trial1", # Load a learned skill library.
    ckpt_dir="YOUR_CKPT_DIR", # Feel free to use a new dir. Do not use the same dir as skill library because new events will still be recorded to ckpt_dir. 
    resume=False, # Do not resume from a skill library because this is not learning.
)
```
Then, you can run task decomposition. Notice: Occasionally, the task decomposition may not be logical. If you notice the printed sub-goals are flawed, you can rerun the decomposition.
```python
# Run task decomposition
task = "YOUR TASK" # e.g. "Craft a diamond pickaxe"
sub_goals = voyager.decompose_task(task=task)
```
Finally, you can run the sub-goals with the learned skill library:
```python
voyager.inference(sub_goals=sub_goals)
```

For all valid skill libraries, see [Learned Skill Libraries](skill_library/README.md).

# TypeSafe (Jev) Gates

Voyager can consult [TypeSafe](https://docs.typesafe.ai)'s Jev model for typed judgments next to the GPT agents. Set `TYPESAFE_API_KEY` in your environment (or `.env`); without it every gate is a silent no-op and Voyager behaves exactly as before.

| Gate | Mode | What it does |
| --- | --- | --- |
| Critic shadow | log only | After each GPT critic verdict, Jev judges success from the same final state (plus the inventory delta since the task started). Both verdicts are logged for agreement analysis; GPT keeps control. |
| QA gating | active | Jev scores the curriculum's generated questions for usefulness; only the top `typesafe_qa_keep` are answered by GPT. Near-duplicate cached questions are reused when Jev agrees they ask the same thing. |
| Task fan-out | veto active, score logged | Each GPT-proposed task is checked for rule violations (placing/building/planting/trading, pointless repeats, unchanged failed tasks). A veto re-asks GPT with the reason, at most twice. A feasibility score and verb class are logged for later use. |

Records go to `<ckpt_dir>/typesafe/*.jsonl`. Tunables on `Voyager(...)`: `typesafe_enabled`, `typesafe_model`, `typesafe_critic_threshold`, `typesafe_veto_threshold`, `typesafe_qa_keep`.

```bash
python scripts/check_typesafe_state.py      # offline sanity check of the state builder
python scripts/typesafe_agreement.py ckpt   # Jev vs GPT critic agreement after a run
```

Beyond the gates, Jev also steers exploration and is available to generated skills:

| Where | What it does |
| --- | --- |
| `exploreUntil` traversal | Every ~10 s of exploration, Jev picks the next compass direction (or up/down/stay) from a terrain survey; pathfinder walks there. Falls back to the original random walk when Jev is unavailable or unsure. |
| `askJev(bot, question, options, context)` primitive | Generated skills can ask one Choice question about the live game state for judgment calls (which target, whether to retreat, where to place). Returns `{choice, confidence, probabilities}` or `null`; at most 20 calls per program. Exact facts such as inventory checks stay in code. |

Between tasks the bot is reset in place (`POST /reset` on the mineflayer server: stop pathing, clear controls, cancel pvp/collect tasks, reset per-task counters and timers) instead of restarting the Node process. Hard resets and error recovery still restart the process.

```bash
# Node does not read .env; export the key first (Voyager itself inherits it via load_dotenv).
cd voyager/env/mineflayer && TYPESAFE_API_KEY=... node scripts/jev_dryrun.js   # traversal + askJev against a canned world, no Minecraft
```

# Fast Mode (Jev action loop + goal brain)

`run_fast.py` runs Voyager with two timescales instead of the step/pause cycle:

| Layer | Where | Cadence | What it does |
| --- | --- | --- | --- |
| Fast loop | `voyager/env/mineflayer/lib/fastLoop.js` | every action (~0.2 s Jev call + the primitive itself) | Snapshots the world, builds a menu of bounded primitives (`walk:<dir>`, `climb:up`, `pillar:up` (jump-and-place tower that re-sends the placement on every tick of the valid window near the top of the jump; the pathfinder's own towering and bridging are switched off because they place once at lift-off and bounce for many jumps), `surface:up` (dig a staircase back to daylight; offered underground with a pickaxe), `dig:down`, `mine:<block>`, `collect:items` (dropped stacks), `craft:<item>`, `smelt:<item>` (loads the furnace with the whole batch plus fuel and leaves it cooking; the brain parks that subgoal and works on another until `furnace:collect` is offered as ready), `place:<block>` (finds an air block with a solid neighbour, digging a pocket underground if needed), `equip:<tool|armor>` (best sword, pickaxe or axe for the job; any carried armor piece that beats the worn one), `chest:deposit` (everything the keep table does not want, into a nearby chest, from 24 slots used), `discard:junk` (toss it when no chest is near and the bag is nearly full), `withdraw:<item>` (take what the goal needs from a chest whose contents are known), `return:home` (back to the home base), `return:<landmark>` (walk back to the last crafting table, furnace or chest seen), `attack:<mob>`, `flee`, `eat`; there is no wait: standing still never reaches a goal) with live facts in each description, asks Jev one Choice plus Nouls for danger, stuck, and whether the subgoal is still worth doing, executes the pick, and repeats. Code owns the exact checks. |
| Milestone brain | `voyager/fast/brain.py`, `voyager/fast/milestones.py`, `lib/goalPlanner.js` | per subgoal | Goals come from a fixed **milestone ladder** toward beating the game (wood, wooden tools, a home base of crafting table + furnace + chest whose position is remembered across runs, a recurring basic kit of logs/food/torches/cobblestone/spare pickaxe/free slots, stone tools, furnace and coal, iron tools, iron armor, food stock, diamonds, diamond gear, nether portal, the nether, blaze rods, ender pearls, eyes of ender, stronghold, the End, the dragon). Each milestone is a set of verifiable targets (item counts, families such as `family:food`, a block nearby, the current dimension, or a judged statement); Node checks them all exactly each cycle and the first unmet milestone becomes the high goal, kits first. The **goal planner** expands the milestone's targets through recipes, smelting, block drops, mob drops, tool tiers, stations and fuel into one requirement graph, together with the **next milestone's targets as lookahead**, so shared needs aggregate (iron for the pickaxe and the armor is one trip) and current-goal leaves are offered before lookahead-only ones. Jev picks the subgoal. The ladder is strict: a reached milestone stays reached (it is a skill-tree node); a rung that fails three times forces a full kit run first, and after five failures the ladder may step exactly one rung past it for a while, never further. A block named as a target (a chest nearby) is a station to craft and place next to the crafting table, or to walk back to if one already stands somewhere known; only natural blocks are explored for. Gathering subgoals are chunked to 8 items, need free inventory space first (the planner inserts a deposit/discard step), and take from a known chest instead of mining when one holds the item. Armor is worn automatically by code the moment a better piece is carried. The GPT curriculum is used only with `use_curriculum=True` or once the ladder is complete. |
| Skill tree | `voyager/fast/skills.py` | per reached goal | A skill is a goal the bot has verified reaching: a node keyed by its target with a `verify` spec (item count, block nearby, or judged by a Jev Noul). A reached subgoal becomes a `sub` node recording the high goal it served, seconds, tools held, inventory delta, and per-fingerprint replay routes. A reached high goal becomes a `high` node connected to the sub nodes used on the way, so `ckpt/fast/skills.json` records how each goal was reached from earlier ones. A fresh bot has no nodes. |

| Player chat | `lib/fastLoop.js` (`playerChat`), `brain.py` (`_handle_player_chat`) | per chat line | Lines typed by players arrive with each status poll (lines from this bot and from sibling bots named `bot`, `bot2`, ... are dropped). For each line Jev answers one Choice (`new_task`, `stop`, `none`; a message naming another bot is `none`) and one Noul (interrupt the current high goal now?). Tasks go on a **directive queue that is disjoint from the ladder**: a directive runs as its own high goal (source `chat`) with no milestone bookkeeping, no failed-task cooldown and no skill-tree node when interrupted; the ladder resumes afterwards. An interrupting request ends the current goal at once; `stop` clears the queue and ends a chat goal. The bot acknowledges in chat (`On it: ...`, `Queued: ...`, `Done: ...`). The interrupt Noul is told the ladder is resumable and the player is waiting, threshold 0.45 (`chat_interrupt_threshold`): a clear task interrupts, a vague group remark does not. A directive is **decomposed once** by the curriculum's QA model into at most 6 ordered steps (`_chat_decompose`), each with a target code verifies: `{item, count}` (planned through the recipe graph like a ladder target), `{nearBlock}`, `{nearPlayer, distance}` (new target kind; `goto:player` primitive walks to the player when in render distance), `{give: {item, count, to}}` (new; `give:<item>` drops the items at the player's feet within 4 blocks, counted in code), or `null` (judged by the goalReached Noul). Steps run in order; a stalled step is re-offered rather than skipped; the last step reached ends the goal. Player-relative steps are not skill-tree nodes. Unparseable replies fall back to the raw request as one judged step. |

| Parameterised menu | `lib/fastLoop.js` (`foldMenu`, `composeAction`) | every action | The flat menu (`mine:oak_log`, `walk:north`, ...) stays the source of truth for execution, replay and skills. For Jev it is folded into one verb Choice (a verb with several targets is one option whose description lists every target with its facts) plus **speculative parameter Choices answered in the same request**: `param_<verb>` picks the exact target for walk/mine/craft/attack/smelt/place/equip/return/withdraw when a verb has 2+ options, and `param_count` picks the mine batch size (one, four, eight). Code composes `verb:param`, checks it is on the menu, and caps the count at the target's remaining need. The count never enters the action id. |
| Batch mining and bridging | `lib/fastLoop.js` (`mineBatch`, `reachDig`, `connectedSameFamily`, `bridge`) | per action | `mine:<block>` takes up to the chosen count in one action (45 s batch cap, 10 s per block): blocks within arm's reach (4.5 blocks) are dug directly without the pathfinder, then the bot steps onto the spot to pick up the drop; otherwise the collect plugin walks. Connected logs or ore of the same family are followed. Pathfinder scaffolding is on for horizontal gaps and water with carried cobblestone/dirt/planks (one-by-one towers stay off, the server rejected jump placement). `bridge:<direction>` is offered when the survey shows water or no surface that way and the bot carries 4+ filler blocks: sneak, place under the feet, step, up to 12 blocks. Bridging is untested on the live server. |
| Action generator | `voyager/fast/generator.py`, `lib/primitiveRegistry.js`, `POST /fast/primitive` | on a stall | When a subgoal has stalled twice and Jev's `action_gap` Noul (threshold 0.65; the state carries the biome and outcome tallies; live: a mining stall with every verb available 0.22, four mine timeouts in a forest 0.29, walks timing out in an ocean 0.87) says no available action fits, GPT (the action agent's model) is asked in a worker thread for ONE primitive following the module contract: synchronous `menu(snap, loop)` returning a live one-line description or null, and `async execute(bot, loop, ctx)` returning `ok...` or `failed: ...`. The same static checks run in Python and Node (no process/fs/eval/Function/bot.chat/setInterval, every loop body must await, restricted require), Node compiles it with `new Function` in a limited scope, calls `menu()` once, and offers it as `x:<name>` with a trial note. An outcome starting with `ok` counts as a success (an enabler such as reaching the shore gains no items; a useless one is caught by no-progress); three failures with no success retire it. One generation in flight, a 10 min cooldown per subgoal, at most 8 live. Accepted sources are saved under `ckpt/fast/primitives/` and re-registered on resume. Turn off with `FastBrain(generate_actions=False)`. A permanent `uncaughtException` handler keeps the bot process alive if a generated callback throws later. |

Subgoal targets are **absolute holdings** (what the bot must hold in total: current count plus the step), so the planner, the already-done filter and the loop's reached check agree; with relative counts "Craft 3 oak_planks" was pruned as done when 5 were held and the Home base goal fell through to its bare name. A high goal never posts its own name as a subgoal: when every step is in the failed set the brain offers one again, then the first unmet target itself ("Place the chest"), and otherwise fails the goal at once; Node refuses such a goal too. `discard:junk` deletes junk with `/clear @s` when the bot is an operator (nothing hits the ground), otherwise tosses it and ignores those item entities in the drop scan for 6 minutes so the bot stops picking its own junk back up.

**Run review fixes (2026-10-02).** A dark forest canopy is not a roof: the sky check ignores leaves, logs and plants, so the bot is only "underground" with real blocks overhead, and the staircase action fails fast when there is nothing to step onto instead of turning 48 times. Tools: the craft menu does not offer a tool or station already held unless a goal asks for it, and the keep table keeps the best of each tool kind plus one spare pickaxe (the rest is junk for deposit or discard). Stuck: an action that returns in under a second without gain, or that Jev judges stuck on twice running, sits out the next 4 decisions (`cooldowns`; eat and flee are never pruned), instant failures cost two of the no-progress budget, two consecutive stuck verdicts raise an early no-progress so the brain re-selects, and the jump nudge only runs on the geometric stuck check. Generated actions: every outcome is logged (`action_outcome`), progress toward the target counts as success even after a timeout, a failed action is sent back to GPT once with its outcome for a repair before it can be retired (`action_repaired`), the gap Noul threshold is 0.65 and sees outcome tallies per action, the generation prompt carries those tallies and asks for the obstacle-removing action rather than a copy of an existing kind, and the contract states that entity direction is a compass word, that mineflayer calls return promises, and offers `loop.nearestEntity` and `loop.freeSpotNear`.

Recalculation is triggered by code, not by raw inventory changes: **subgoal reached** (exact count on its target, or a Jev Noul for subgoals without one), **high goal reached** (its own target, parsed once and kept across subgoals), **no progress** (no target gain for 120 s or 12 actions, whichever first; the subgoal is marked failed and another is chosen, three failures fail the high goal), **hazard** (health drop, lava, drowning, hostile within 6 blocks), and **stuck** (no movement over four move actions). The Minecraft server is never paused in fast mode (`VoyagerEnv(pause_server=False)`). The only GPT call in fast mode is the curriculum's high-goal proposal: no GPT critic and no GPT-written programs.

```bash
venv/Scripts/python run_fast.py                              # MC_PORT env var overrides the port
venv/Scripts/python run_fast.py --bot 2                      # a second bot: name bot2, mineflayer port 3001, checkpoint dir *_bot2
cd voyager/env/mineflayer && npm install                     # postinstall patches minecraft-protocol's signed-chat bug (scripts/patch_minecraft_protocol.js); without it any player chat message kills the bot process
venv/Scripts/python scripts/run_bots.py 3                    # bot, bot2, bot3 on one server, one Python + one node process each, staggered starts
cd voyager/env/mineflayer && TYPESAFE_API_KEY=... node scripts/fastloop_dryrun.js   # target parsing, menu, one real Jev decision; no Minecraft
venv/Scripts/python scripts/skill_tree.py --open                # render ckpt/fast/skills.json as an interactive HTML skill tree
```

Every LLM call and environment round trip is timed to `ckpt/timing.jsonl` (`voyager/utils/timing.py`) in both modes, so the GPT / Jev / Minecraft split of a run can be read off the log.

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
