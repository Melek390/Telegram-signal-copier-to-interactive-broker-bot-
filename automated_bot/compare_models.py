"""
Run several Claude models over the same signal screenshots and score them against the
answers we established by hand, so the model choice is a measurement rather than a guess.

    python -m automated_bot.compare_models
    python -m automated_bot.compare_models --models claude-haiku-4-5 claude-sonnet-5
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

# Expected results, verified by hand against the real messages earlier.
# `None` for a field means "don't care".
EXPECTED = {
    "32019": dict(action="buy",    ticker="MRVL", strike=250.0, expiry="2026-07-31",
                  price=2.30, target=3.00, note="price rendered as 2:30"),
    "32008": dict(action="buy",    ticker="NVDA", strike=215.0, expiry="2026-07-31",
                  price=1.96, target=2.80, note="JUL 31'26 no space + 1,96 comma"),
    "32041": dict(action="buy",    ticker="AVGO", strike=410.0, expiry="2026-08-03",
                  price=1.00, target=1.60, note="two prices 0.87/1.00 -> ask 1.00"),
    "32045": dict(action="buy",    ticker="TSLA", strike=330.0, expiry="2026-08-10",
                  price=2.43, target=3.20, note="text says 430, screenshot says 330"),
    "32020": dict(action="buy",    ticker="SPY",  strike=755.0, expiry="2026-07-31",
                  price=1.04, target=1.60, note="clean single-price card"),
    "32033": dict(action="buy",    ticker="NVDA", strike=210.0, expiry="2026-08-07",
                  price=1.45, target=2.00, note="clean, only 2 targets"),
    "32024": dict(action="ignore", reason="averaging_down", note="contains المتوسط"),
    "32043": dict(action="ignore", reason="chart_image",    note="chart, not a card"),
    "32048": dict(action="ignore", reason=None,             note="monthly report image"),
    "32027": dict(action="ignore", reason=None,             note="weekly report image"),
    "32050": dict(action="trim",   ticker="TSLA", strike=330.0, expiry="2026-08-10",
                  price=3.20, note="خفف take-profit update"),
}


def got_price(r):
    return r.price_ask or r.price_last or r.price_bid


def score(r, exp) -> tuple[bool, str]:
    """Return (passed, what_went_wrong)."""
    if r.action != exp["action"]:
        return False, f"action {r.action} (want {exp['action']})"
    if exp["action"] == "ignore":
        if exp.get("reason") and r.reason != exp["reason"]:
            return False, f"reason {r.reason} (want {exp['reason']})"
        return True, ""
    bad = []
    for field, want in (("ticker", exp.get("ticker")), ("strike", exp.get("strike")),
                        ("expiry", exp.get("expiry"))):
        have = getattr(r, field)
        if want is not None and have != want:
            bad.append(f"{field}={have} (want {want})")
    if exp.get("price") is not None:
        have = got_price(r)
        if have is None or abs(have - exp["price"]) > 0.005:
            bad.append(f"price={have} (want {exp['price']})")
    if exp.get("target") is not None:
        if r.first_target is None or abs(r.first_target - exp["target"]) > 0.005:
            bad.append(f"target={r.first_target} (want {exp['target']})")
    return (not bad), "; ".join(bad)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*",
                    default=["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"])
    args = ap.parse_args()

    # message id -> (image path, text)
    cases = {}
    for f in sorted(glob.glob(str(SAMPLES / "2*.json"))):
        m = json.loads(Path(f).read_text(encoding="utf-8"))
        mid = str(m["message_id"])
        if mid in EXPECTED and m.get("image_file"):
            cases[mid] = (SAMPLES / m["image_file"], m.get("text", ""))

    missing = set(EXPECTED) - set(cases)
    if missing:
        print(f"(skipping, no image on disk: {', '.join(sorted(missing))})\n")

    today = date.today().isoformat()
    summary = {}

    for model_id in args.models:
        print(f"\n{'=' * 78}\n{model_id}\n{'=' * 78}")
        passed = 0
        elapsed = 0.0
        for mid, (img, text) in sorted(cases.items()):
            exp = EXPECTED[mid]
            t0 = time.monotonic()
            r = read_signal(img, text, today=today, model_id=model_id)
            dt = time.monotonic() - t0
            elapsed += dt
            ok, why = score(r, exp)
            passed += ok
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {mid}  {dt:5.1f}s  {exp['note']}")
            if not ok:
                print(f"         -> {why}")
        n = len(cases)
        summary[model_id] = (passed, n, elapsed / n if n else 0)
        print(f"  {passed}/{n} correct, {elapsed / n if n else 0:.1f}s avg")

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(f"{'model':<24}{'score':<10}{'avg latency'}")
    for model_id, (passed, n, avg) in summary.items():
        print(f"{model_id:<24}{f'{passed}/{n}':<10}{avg:.1f}s")


if __name__ == "__main__":
    main()
