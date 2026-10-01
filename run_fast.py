"""Run Voyager in fast mode: Jev picks actions inside the bot process, the
curriculum sets goals from Python, and the Minecraft server is never paused.

Usage (from the repo root, with the venv python):
    python run_fast.py
"""

import os
import time

from dotenv import load_dotenv

from voyager import Voyager
from voyager.fast import FastBrain

load_dotenv()

openai_api_key = os.environ["OPENAI_API_KEY"]

RESUME = False
RESUME_CKPT_DIR = "ckpt"
ckpt_dir = RESUME_CKPT_DIR if RESUME else f"ckpt_{time.strftime('%Y%m%d_%H%M%S')}"
print(f"Using checkpoint directory: {ckpt_dir}")

voyager = Voyager(
    mc_port=int(os.environ.get("MC_PORT", "53202")),
    openai_api_key=openai_api_key,
    ckpt_dir=ckpt_dir,
    resume=RESUME,
    env_wait_ticks=5,
)

brain = FastBrain(
    voyager,
    poll_seconds=1.0,
    goal_timeout_seconds=300,
    stalls_before_gpt=2,
    stalls_before_fail=4,
)
brain.learn()
