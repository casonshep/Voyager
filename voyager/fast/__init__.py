"""Fast mode: a Jev-driven action loop in the bot process with a goal brain in Python.

See ``FastBrain`` for the goal/subgoal loop and ``SkillMemory`` for the
store of completed subgoals. The Node-side controller lives in
``voyager/env/mineflayer/lib/fastLoop.js``.
"""

from .skills import SkillMemory
from .brain import FastBrain

__all__ = ["FastBrain", "SkillMemory"]
