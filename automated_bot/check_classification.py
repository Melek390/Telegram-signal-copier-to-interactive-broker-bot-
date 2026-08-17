"""
Classification-only check across every saved message that has an image.

Ground truth comes from literal keyword presence in the Arabic text, which is exactly
what the rules say the answer should be — so any disagreement is a real defect, not a
judgement call.

    python -m automated_bot.check_classification
    python -m automated_bot.check_classification --model claude-sonnet-5
"""

import argparse
import glob
import json
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env", override=True)

from .signal_reader import read_signal  # noqa: E402

SAMPLES = BASE / "recent_signals"


def expected_action(text: str) -> str:
    """What the rules say, from literal keyword presence."""
    if "المتوسط" in text:
        return "ignore"                                   # averaging down
    if "بسم الله" in text and ("كول" in text or "بوت" in text):
        return "buy"
    if "خفف" in text:
        return "trim"
    return "ignore"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--dir", default=str(SAMPLES))
    args = ap.parse_args()

    cases = []
    for f in sorted(glob.glob(str(Path(args.dir) / "2*.json"))):
        m = json.loads(Path(f).read_text(encoding="utf-8"))
        if m.get("has_image") and m.get("image_file"):
            cases.append(m)

    today = date.today().isoformat()
    wrong = []
    print(f"Checking {len(cases)} image messages with {args.model}\n")

    for m in cases:
        want = expected_action(m["text"])
        r = read_signal(Path(args.dir) / m["image_file"], m["text"],
                        today=today, model_id=args.model)
        # A chart image is allowed to downgrade a buy/trim to ignore — we can't know
        # from the text alone whether the picture is a card, and ignoring is the safe way to be wrong.
        ok = (r.action == want) or (want != "ignore" and r.action == "ignore"
                                    and r.reason in ("chart_image", "unreadable"))
        if not ok:
            wrong.append((m["message_id"], want, r.action, r.reason,
                          m["text"].split("\n")[0][:52]))
        print(f"  {'ok  ' if ok else 'WRONG'} {m['message_id']}  want={want:<7}got={r.action:<7}{r.reason or ''}")

    print(f"\n{len(cases) - len(wrong)}/{len(cases)} agree with the rules")
    if wrong:
        print("\nDisagreements:")
        for mid, want, got, reason, head in wrong:
            print(f"  {mid}: want {want}, got {got} ({reason or '-'})  |  {head}")
        # A false 'trim' is the dangerous direction: it feeds the emergency-exit rule.
        false_trims = [w for w in wrong if w[2] == "trim" and w[1] != "trim"]
        if false_trims:
            print(f"\n  !! {len(false_trims)} false trim(s) — these could trigger an exit")


if __name__ == "__main__":
    main()
