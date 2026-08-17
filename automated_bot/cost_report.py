"""
Run the reader over every saved message that has an image and report the real cost,
using the token counts the API returns rather than an estimate.

    python -m automated_bot.cost_report
    python -m automated_bot.cost_report --model claude-sonnet-5
"""

import argparse
import glob
import json
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env", override=True)

from .signal_reader import read_signal  # noqa: E402

SAMPLES = BASE / "recent_signals"

# USD per million tokens. Cache write is 1.25x base input, cache read is 0.1x.
RATES = {
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
    "claude-sonnet-5":  {"in": 2.00, "out": 10.00},   # intro pricing to 2026-08-31
    "claude-opus-5":    {"in": 5.00, "out": 25.00},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--dir", default=str(SAMPLES))
    args = ap.parse_args()

    rate = RATES.get(args.model)
    if not rate:
        raise SystemExit(f"No rate card for {args.model}; add one to RATES.")

    folder = Path(args.dir)
    total = with_image = 0
    cases = []
    for f in sorted(glob.glob(str(folder / "2*.json"))):
        m = json.loads(Path(f).read_text(encoding="utf-8"))
        total += 1
        if not m.get("has_image") or not m.get("image_file"):
            continue                                   # pre-filter: never sent to Claude
        with_image += 1
        cases.append((m["message_id"], folder / m["image_file"], m.get("text", "")))

    print(f"{total} messages on disk — {with_image} have an image and get analysed, "
          f"{total - with_image} are text-only and are skipped for free.\n")

    tok = {"in": 0, "cache_write": 0, "cache_read": 0, "out": 0}
    actions: dict[str, int] = {}
    today = date.today().isoformat()
    started = time.monotonic()

    def collect(u) -> None:
        tok["in"] += u.input_tokens or 0
        tok["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        tok["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        tok["out"] += u.output_tokens or 0

    for i, (mid, img, text) in enumerate(cases, 1):
        r = read_signal(img, text, today=today, model_id=args.model, on_usage=collect)
        actions[r.action] = actions.get(r.action, 0) + 1
        print(f"  [{i:>3}/{with_image}] {mid}  {r.action:<7}{r.reason or ''}")

    wall = time.monotonic() - started

    cost_in    = tok["in"] * rate["in"] / 1e6
    cost_write = tok["cache_write"] * rate["in"] * 1.25 / 1e6
    cost_read  = tok["cache_read"] * rate["in"] * 0.10 / 1e6
    cost_out   = tok["out"] * rate["out"] / 1e6
    total_cost = cost_in + cost_write + cost_read + cost_out

    print(f"\n{'=' * 62}\n{args.model} — {with_image} images analysed\n{'=' * 62}")
    print(f"{'':<22}{'tokens':>12}{'cost':>12}")
    print(f"{'input (uncached)':<22}{tok['in']:>12,}{cost_in:>12.4f}")
    print(f"{'cache writes':<22}{tok['cache_write']:>12,}{cost_write:>12.4f}")
    print(f"{'cache reads':<22}{tok['cache_read']:>12,}{cost_read:>12.4f}")
    print(f"{'output':<22}{tok['out']:>12,}{cost_out:>12.4f}")
    print(f"{'-' * 46}")
    print(f"{'TOTAL':<22}{sum(tok.values()):>12,}${total_cost:>11.4f}")
    print(f"\nper image      : ${total_cost / with_image:.5f}")
    print(f"wall clock     : {wall:.0f}s  ({wall / with_image:.1f}s per image)")
    print(f"classified     : " + "  ".join(f"{k}={v}" for k, v in sorted(actions.items())))


if __name__ == "__main__":
    main()
