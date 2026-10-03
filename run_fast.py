"""Run Voyager in fast mode: Jev picks actions inside the bot process, the
curriculum sets goals from Python, and the Minecraft server is never paused.

Usage (from the repo root, with the venv python):
    python run_fast.py            # one bot named "bot" on mineflayer port 3000
    python run_fast.py --bot 2    # a second bot: "bot2", port 3001, its own checkpoint dir

Each bot is one Python process plus one node process; several share one
Minecraft server. `scripts/run_bots.py 3` starts three of them.
"""

import argparse
import os
import time

from dotenv import load_dotenv

from voyager import Voyager
from voyager.fast import FastBrain

load_dotenv()

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--bot", type=int, default=1, help="bot number, 1-5; sets the name, mineflayer port and checkpoint dir")
parser.add_argument("--resume", default=None, help="checkpoint dir to resume (bot number suffix is added for bots > 1)")
args = parser.parse_args()
if not 1 <= args.bot <= 5:
    parser.error("--bot must be between 1 and 5")

openai_api_key = os.environ["OPENAI_API_KEY"]

RESUME = args.resume is not None
RESUME_CKPT_DIR = args.resume or "ckpt_skill_tree"


BOT_NAME = "bot" if args.bot == 1 else f"bot{args.bot}"
SERVER_PORT = 3000 + args.bot - 1
suffix = "" if args.bot == 1 else f"_{BOT_NAME}"
ckpt_dir = (RESUME_CKPT_DIR if RESUME else f"ckpt_{time.strftime('%Y%m%d_%H%M%S')}") + suffix
print(f"Bot {BOT_NAME}: mineflayer port {SERVER_PORT}, checkpoint directory {ckpt_dir}")

voyager = Voyager(
    mc_port=int(os.environ.get("MC_PORT", "54321")),
    server_port=SERVER_PORT,
    bot_username=BOT_NAME,
    openai_api_key=openai_api_key,
    ckpt_dir=ckpt_dir,
    resume=RESUME,
    env_wait_ticks=5,
)

# Where the whole run is heading. The curriculum prefers tasks that move
# toward it, and Jev uses it to break ties between subgoals. Edit freely.
LONG_TERM_GOAL = (
    "Beat the game: get diamond gear, build a nether portal, find a stronghold, "
    "and defeat the ender dragon."
)

brain = FastBrain(
    voyager,
    poll_seconds=1.0,
    goal_timeout_seconds=600,  # three 120 s subgoal stalls must fit inside it
    subgoal_failures_before_fail=3,
    long_term_goal=LONG_TERM_GOAL,
    failed_task_cooldown=10,  # completed subgoals before a failed task may be proposed again
)
brain.learn()
