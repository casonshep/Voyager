"""Summarise critic shadow agreement between Jev and the GPT critic.

Usage:
    venv/Scripts/python.exe scripts/typesafe_agreement.py [ckpt_dir]

Reads <ckpt_dir>/typesafe/critic_shadow.jsonl (default ckpt_dir = ckpt) and
prints, for several thresholds, how often Jev's success Noul agrees with the
GPT verdict, treating GPT as the reference. False positive = Jev says success,
GPT says failure (the dangerous direction: a bad skill would enter the library).
"""

from __future__ import annotations

import json
import os
import sys

THRESHOLDS = (0.5, 0.7, 0.8, 0.9)


def load(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "answers" in row and "success" in row["answers"]:
                rows.append(row)
    return rows


def summarise(rows: list[dict], key: str) -> None:
    rows = [r for r in rows if r["answers"].get(key) and r["extra"].get("gpt_success") is not None]
    if not rows:
        print(f"  {key}: no rows")
        return
    print(f"  {key} (n={len(rows)})")
    print(f"  {'thr':>5} {'agree':>7} {'FP':>4} {'FN':>4}")
    for thr in THRESHOLDS:
        agree = fp = fn = 0
        for r in rows:
            jev = r["answers"][key]["noul"] >= thr
            gpt = bool(r["extra"]["gpt_success"])
            if jev == gpt:
                agree += 1
            elif jev and not gpt:
                fp += 1
            else:
                fn += 1
        print(f"  {thr:>5.2f} {agree / len(rows):>7.1%} {fp:>4} {fn:>4}")


def main() -> None:
    ckpt_dir = sys.argv[1] if len(sys.argv) > 1 else "ckpt"
    path = os.path.join(ckpt_dir, "typesafe", "critic_shadow.jsonl")
    if not os.path.exists(path):
        sys.exit(f"no shadow log at {path}")
    rows = load(path)
    print(f"{len(rows)} shadow judgments in {path}")
    summarise(rows, "success")
    summarise(rows, "success_by_delta")

    channels: dict[str, int] = {}
    for r in rows:
        ch = r["answers"].get("verification_channel", {}).get("choice")
        if ch:
            channels[ch] = channels.get(ch, 0) + 1
    if channels:
        print("  verification channels:", dict(sorted(channels.items(), key=lambda kv: -kv[1])))

    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    if latencies:
        latencies.sort()
        print(f"  latency ms: median={latencies[len(latencies) // 2]} max={latencies[-1]}")


if __name__ == "__main__":
    main()
