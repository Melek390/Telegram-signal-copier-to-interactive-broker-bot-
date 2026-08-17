"""
Run the Claude signal reader over saved channel messages and print what it decided.

    python -m automated_bot.check_samples                      # every message with an image
    python -m automated_bot.check_samples --limit 5
    python -m automated_bot.check_samples --msg 32045 32019    # specific messages
"""

import argparse
import json
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env", override=True)

from .signal_reader import read_signal  # noqa: E402  (must follow load_dotenv)

SAMPLES = BASE / "recent_signals"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(SAMPLES))
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--msg", nargs="*", help="only these message ids")
    args = ap.parse_args()

    folder = Path(args.dir)
    metas = []
    for f in sorted(folder.glob("2*.json")):
        m = json.loads(f.read_text(encoding="utf-8"))
        if not m.get("has_image"):
            continue                                   # pre-filter: images only
        if args.msg and str(m["message_id"]) not in args.msg:
            continue
        metas.append(m)
    if args.limit:
        metas = metas[-args.limit:]

    if not metas:
        print(f"No messages with images found in {folder}")
        return

    today = date.today().isoformat()
    print(f"Reading {len(metas)} message(s) with {folder.name}/\n")
    print(f"{'msg':<8}{'action':<9}{'reason':<17}{'contract':<34}{'target':<8}conf")
    print("-" * 88)

    counts: dict[str, int] = {}
    for m in metas:
        r = read_signal(folder / m["image_file"], m.get("text", ""), today=today)
        counts[r.action] = counts.get(r.action, 0) + 1

        if r.ticker:
            price = r.price_ask or r.price_last or r.price_bid
            contract = f"{r.ticker} {r.strike} {r.right} {r.expiry} @ {price}"
        else:
            contract = "-"
        print(f"{m['message_id']:<8}{r.action:<9}{str(r.reason or ''):<17}"
              f"{contract:<34}{str(r.first_target or '-'):<8}{r.confidence}")
        if r.note:
            print(f"        note: {r.note}")

    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
