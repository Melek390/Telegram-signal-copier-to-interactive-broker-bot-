"""
The whole pipeline, end to end, over saved channel messages.

    stage 1  hardcoded prefilter — image or nothing (free, local, no API key needed)
    stage 2  Claude reads the survivors and classifies + extracts
    stage 3  report classifications and the real cost from the API's own token counts

    python -m automated_bot.pipeline --dry-run     # stage 1 only, costs nothing
    python -m automated_bot.pipeline               # full run
    python -m automated_bot.pipeline --model claude-sonnet-5
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

from . import prefilter                # noqa: E402
from .signal_reader import read_signal  # noqa: E402

SAMPLES = BASE / "recent_signals"

# USD per million tokens. Cache write is 1.25x base input, cache read is 0.1x.
RATES = {
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
    "claude-sonnet-5":  {"in": 2.00, "out": 10.00},   # intro pricing to 2026-08-31
    "claude-opus-5":    {"in": 5.00, "out": 25.00},
}


def load_messages(folder: Path) -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(folder / "2*.json"))):
        out.append(json.loads(Path(f).read_text(encoding="utf-8")))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--dir", default=str(SAMPLES))
    ap.add_argument("--dry-run", action="store_true",
                    help="run stage 1 only — no API calls, no cost")
    args = ap.parse_args()

    folder = Path(args.dir)
    messages = load_messages(folder)

    # ---- stage 1: hardcoded prefilter -------------------------------------------
    passed, blocked = [], []
    for m in messages:
        img = folder / m["image_file"] if m.get("image_file") else None
        if prefilter.allows(m.get("has_image")) and prefilter.allows_image_file(img):
            passed.append((m, img))
        else:
            blocked.append(m)

    print(f"STAGE 1 — prefilter (local, free)")
    print(f"  messages in        : {len(messages)}")
    print(f"  blocked, no image  : {len(blocked)}   <- never reach Claude")
    print(f"  passed to Claude   : {len(passed)}")

    # Prove the gate actually refuses at the reader, not just here.
    if blocked:
        probe = read_signal("", blocked[0].get("text", ""))
        ok = probe.action == "ignore" and probe.reason == "no_image"
        print(f"  reader gate check  : {'OK' if ok else 'FAILED'} "
              f"(imageless input -> {probe.action}/{probe.reason}, no API call)")

    if args.dry_run:
        rate = RATES.get(args.model, RATES["claude-haiku-4-5"])
        print(f"\nDry run — stopping before stage 2.")
        print(f"A full run would send {len(passed)} images to {args.model}.")
        return

    # ---- stage 2: Claude ---------------------------------------------------------
    tok = {"in": 0, "cache_write": 0, "cache_read": 0, "out": 0}

    def collect(u) -> None:
        tok["in"] += u.input_tokens or 0
        tok["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        tok["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        tok["out"] += u.output_tokens or 0

    print(f"\nSTAGE 2 — {args.model} reading {len(passed)} images")
    today = date.today().isoformat()
    actions: dict[str, int] = {}
    reasons: dict[str, int] = {}
    buys: list[str] = []
    started = time.monotonic()

    for i, (m, img) in enumerate(passed, 1):
        r = read_signal(img, m.get("text", ""), today=today,
                        model_id=args.model, on_usage=collect)
        actions[r.action] = actions.get(r.action, 0) + 1
        if r.reason:
            reasons[r.reason] = reasons.get(r.reason, 0) + 1
        if r.action == "buy":
            buys.append(f"{m['message_id']}  {r.ticker} {r.strike} {r.right} "
                        f"{r.expiry}  @{r.price_ask or r.price_last}  tgt {r.first_target}")
        print(f"  [{i:>3}/{len(passed)}] {m['message_id']}  {r.action:<7}{r.reason or ''}")

    wall = time.monotonic() - started

    # ---- stage 3: report ---------------------------------------------------------
    rate = RATES[args.model]
    cost = (tok["in"] * rate["in"]
            + tok["cache_write"] * rate["in"] * 1.25
            + tok["cache_read"] * rate["in"] * 0.10
            + tok["out"] * rate["out"]) / 1e6

    print(f"\nSTAGE 3 — results")
    print(f"  classified   : " + "  ".join(f"{k}={v}" for k, v in sorted(actions.items())))
    print(f"  ignore why   : " + "  ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    print(f"  tokens       : {tok['in']:,} in / {tok['out']:,} out"
          f"  (cache w/r: {tok['cache_write']:,}/{tok['cache_read']:,})")
    print(f"  cost         : ${cost:.4f}  (${cost / len(passed):.5f} per image)")
    print(f"  wall clock   : {wall:.0f}s  ({wall / len(passed):.1f}s per image)")

    print(f"\n  BUY signals extracted ({len(buys)}):")
    for b in buys:
        print(f"    {b}")


if __name__ == "__main__":
    main()
