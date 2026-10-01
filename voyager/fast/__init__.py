"""Fast mode: a Jev-driven action loop in the bot process with a goal brain in Python.

See ``FastBrain`` for the slow-timescale loop and ``SequenceMemory`` for the
record-and-replay store. The Node-side controller lives in
``voyager/env/mineflayer/lib/fastLoop.js``.
"""

from .sequences import SequenceMemory
from .brain import FastBrain

__all__ = ["FastBrain", "SequenceMemory"]
