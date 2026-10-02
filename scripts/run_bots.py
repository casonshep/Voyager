"""Start several fast-mode bots on one Minecraft server, one process each.

Usage (from the repo root, with the venv python):
    python scripts/run_bots.py 3            # bot, bot2, bot3
    python scripts/run_bots.py 2 --resume ckpt_20261001_193757

Bot N is "bot<N>" on mineflayer port 3000+N-1 with its own checkpoint dir.
Starts are staggered so the node servers do not all hard-reset in the same
tick. Ctrl+C stops every bot.
"""

import argparse
import os
import subprocess
import sys
import time

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("count", type=int, nargs="?", default=2, help="how many bots, 1-5")
parser.add_argument("--resume", default=None, help="checkpoint dir prefix to resume")
parser.add_argument("--stagger", type=float, default=15.0, help="seconds between bot starts")
args = parser.parse_args()
if not 1 <= args.count <= 5:
    parser.error("count must be between 1 and 5")

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
procs = []
try:
    for n in range(1, args.count + 1):
        cmd = [sys.executable, os.path.join(root, "run_fast.py"), "--bot", str(n)]
        if args.resume:
            cmd += ["--resume", args.resume]
        print(f"starting bot {n}: {' '.join(cmd)}")
        procs.append(subprocess.Popen(cmd, cwd=root))
        if n < args.count:
            time.sleep(args.stagger)
    while True:
        alive = [p for p in procs if p.poll() is None]
        if not alive:
            break
        time.sleep(2)
except KeyboardInterrupt:
    print("stopping all bots")
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
