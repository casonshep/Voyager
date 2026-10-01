"""Offline tests for fast mode (no Minecraft, no API keys)."""
import json
import os
import tempfile

from voyager.fast.sequences import SequenceMemory, expand, fingerprint_key
from voyager.fast.brain import FastBrain, _matcher


def test_sequence_store_lookup_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fast", "sequences.json")
        mem = SequenceMemory(path)
        fp = {"biome": "plains", "toolTier": "none", "daylight": "day"}
        mem.store("*_log", fp, [{"action": "walk:north", "times": 1}, {"action": "mine:oak_log", "times": 3}], 42.0)
        assert mem.lookup("*_log", fp) == ["walk:north", "mine:oak_log", "mine:oak_log", "mine:oak_log"]
        assert mem.lookup("*_log", {**fp, "daylight": "night"}) is None
        assert mem.library_for(fp) == {"*_log": ["walk:north", "mine:oak_log", "mine:oak_log", "mine:oak_log"]}
        # shorter sequence replaces the longer one
        mem.store("*_log", fp, [{"action": "mine:oak_log", "times": 3}], 20.0)
        assert mem.lookup("*_log", fp) == ["mine:oak_log"] * 3
        # longer one does not replace, but counts as a success
        mem.store("*_log", fp, [{"action": "mine:oak_log", "times": 5}], 20.0)
        assert mem.lookup("*_log", fp) == ["mine:oak_log"] * 3
        assert mem.entries[SequenceMemory.key("*_log", fp)]["successes"] == 2
        reloaded = SequenceMemory(path)
        assert reloaded.lookup("*_log", fp) == ["mine:oak_log"] * 3
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)


def test_expand_and_fingerprint_key():
    assert expand([{"action": "a", "times": 2}, {"action": "b"}]) == ["a", "a", "b"]
    assert fingerprint_key(None) == "?|?|?"


def test_matcher_and_remaining_target():
    assert _matcher("*_log")("spruce_log") and not _matcher("*_log")("log_cabin")
    assert _matcher("stick")("stick") and not _matcher("stick")("sticks")
    target = {"item": "*_log", "need": 3, "have": 4, "gained": 1}  # started with 3 logs
    # GPT program mined two more logs: 6 now -> gained 3 -> remaining 0
    assert FastBrain._remaining_target(target, {"oak_log": 5, "birch_log": 1}) == {"item": "*_log", "count": 0}
    # no inventory snapshot: keep the loop's own count
    assert FastBrain._remaining_target(target) == {"item": "*_log", "count": 2}
    assert FastBrain._remaining_target(None) is None


def test_merge_events_keeps_trailing_observe():
    obs = ("observe", {"inventory": {"oak_log": 1}})
    events = [("onChat", {"onChat": "hi"}), obs]
    merged = FastBrain._merge_events(events, [["onSave", {"onSave": "oak_log_mined"}]])
    assert merged[-1][0] == "observe"
    assert [e[0] for e in merged] == ["onChat", "onSave", "observe"]
    merged2 = FastBrain._merge_events(merged, [["onChat", {}], ["observe", {"inventory": {}}]])
    assert merged2[-1][1] == {"inventory": {}}
    assert FastBrain._inventory(merged) == {"oak_log": 1}
