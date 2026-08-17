#!/usr/bin/env python3
"""
Full-pipeline replay of the 3,000-message archive against the demo account.

Runs ON THE VPS as /root/run_simulation.py. Uses the PRODUCTION code unmodified:
  - automated_bot.read_signal  (real Claude calls, real prompt)
  - ibkr.client._place_bracket_sync / _buy_more_sync / _emergency_exit_sync

Chronological, oldest -> newest, like a live feed. For every message it also
computes the literal text-rule classification and logs any divergence from
Claude's answer — the "compare what's running to the spec" channel.

Adaptations for replay (approved):
  - expiry overridden to 2026-08-05; if the ticker has no Aug 5 expiry, the
    nearest listed one after it; no chain at all -> skip, logged
  - strike snapped to the nearest listed strike for that expiry
  - clientId 77 so nothing collides with the live bot's connections
  - no Telegram: everything goes to sim_results.jsonl + stdout
  - kill switch set for the duration (live bot buys blocked), cleared at end
  - flatten at the end: cancel sim orders, market-close sim positions
"""

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/root/bot")

import ibkr.client as ibc          # noqa: E402  (loads /root/bot/.env)
os.environ["IBKR_CLIENT_ID"] = "77"   # after import — load_dotenv would override

from automated_bot import read_signal   # noqa: E402
from ib_insync import IB, Option, Stock  # noqa: E402

MANIFEST = Path("/root/legit_signals/_manifest.json")
IMG_DIR = Path("/root/legit_signals")
RESULTS = Path("/root/sim_results.jsonl")
HALT = Path("/root/bot/.trading_halted")

TARGET_EXP = "20260805"        # Aug 5; fallback = nearest listed after


def text_rule(text: str) -> str:
    """The literal spec, for divergence checking."""
    t = text or ""
    if "المتوسط" in t:
        return "buy_more"
    if "بسم الله" in t and ("كول" in t or "بوت" in t):
        return "buy"
    if "خفف" in t:
        return "ignore" if ("الهدف" in t or "الاهداف" in t) else "exit"
    return "ignore"


_chains: dict = {}


def flatten(tag: str = "") -> tuple[int, int, list]:
    """Cancel every sim order and market-close every option position (clientId 77)."""
    from ib_insync import MarketOrder
    ib = IB()
    ib.connect("127.0.0.1", ibc._port(), clientId=77, timeout=20)
    ib.reqOpenOrders(); ib.sleep(3)
    n_cancel = 0
    for tr in ib.openTrades():
        ib.cancelOrder(tr.order); n_cancel += 1
    ib.sleep(3)
    n_close = 0
    for pos in ib.positions():
        if pos.contract.secType == "OPT" and pos.position != 0:
            c = pos.contract; c.exchange = c.exchange or "SMART"
            side = "SELL" if pos.position > 0 else "BUY"
            o = MarketOrder(side, abs(int(pos.position))); o.tif = "DAY"
            ib.placeOrder(c, o); n_close += 1
            ib.sleep(1)
    ib.sleep(5)
    left = [(p.contract.localSymbol, p.position) for p in ib.positions()
            if p.contract.secType == "OPT" and p.position != 0]
    ib.disconnect()
    print(f"flatten{tag}: {n_cancel} orders cancelled, {n_close} positions closed, "
          f"leftovers: {left or 'none'}", flush=True)
    return n_cancel, n_close, left


def available_funds(ib: IB) -> float:
    try:
        for v in ib.accountValues():
            if v.tag == "AvailableFunds":
                return float(v.value)
    except Exception:
        pass
    return 1e9


# Replaying year-old signals against today's chains means the card-price sizing
# fallback can under-guess wildly (a 2025 card price on a now-deep-ITM 2026
# contract), so fills eat cash far faster than the $1k budget implies. When funds
# run low, recycle: close everything and keep going. Production never hits this —
# there the card price is current.
LOW_FUNDS = 250_000.0


def resolve(ib: IB, ticker: str, right: str):
    """(expiry_yyyymmdd, [strikes]) for the nearest expiry >= TARGET_EXP, cached."""
    key = (ticker, right)
    if key in _chains:
        return _chains[key]
    out = None
    try:
        st = Stock(ticker, "SMART", "USD")
        if ib.qualifyContracts(st):
            params = ib.reqSecDefOptParams(ticker, "", "STK", st.conId)
            # Adjusted classes (2AMZN, ...) also list on SMART and IBKR rejects
            # API orders on them ("Flex options") — only the standard class,
            # named like the symbol, is orderable.
            p = next((x for x in params
                      if x.exchange == "SMART" and x.tradingClass == ticker),
                     None) or next((x for x in params if x.exchange == "SMART"),
                                   params[0])
            exps = sorted(e for e in p.expirations if e >= TARGET_EXP)
            if exps:
                cds = ib.reqContractDetails(
                    Option(ticker, exps[0], 0, "C" if right == "Call" else "P",
                           exchange="SMART", currency="USD"))
                std = {c.contract.strike for c in cds
                       if c.contract.tradingClass == ticker}
                strikes = sorted(std or {c.contract.strike for c in cds})
                if strikes:
                    out = (exps[0], strikes)
    except Exception as e:
        print(f"    resolver error {ticker}: {e}", flush=True)
    _chains[key] = out
    return out


def main() -> None:
    rows = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows.sort(key=lambda m: m["message_id"])

    # Resume support: skip anything already in the results file, append after it.
    done = set()
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["message_id"])
            except Exception:
                pass
    if done:
        rows = [m for m in rows if m["message_id"] not in done]
        print(f"resuming: {len(done)} already done, {len(rows)} remaining", flush=True)

    print(f"simulation start: {len(rows)} eligible messages, "
          f"budget ${ibc._order_budget():,.0f}, clientId {os.environ['IBKR_CLIENT_ID']}",
          flush=True)

    HALT.write_text("simulation running")
    usage = Counter()

    def on_usage(u):
        usage["in"] += getattr(u, "input_tokens", 0)
        usage["out"] += getattr(u, "output_tokens", 0)

    resolver = IB()
    resolver.connect("127.0.0.1", ibc._port(), clientId=79, timeout=20)

    stats = Counter()
    diverged = []
    t0 = time.time()

    with RESULTS.open("a", encoding="utf-8") as out:
        for i, m in enumerate(rows, 1):
            mid = m["message_id"]
            img = IMG_DIR / (m.get("image_file") or "")
            rec = {"message_id": mid, "date": m["date_utc"][:10]}

            r = read_signal(img, m.get("text") or "", m["date_utc"][:10],
                            on_usage=on_usage)
            expected = text_rule(m.get("text") or "")
            rec.update(action=r.action, reason=r.reason, ticker=r.ticker,
                       right=r.right, strike=r.strike, target=r.first_target,
                       confidence=r.confidence, expected=expected)
            stats[f"action:{r.action}"] += 1

            if r.action != expected:
                # chart images legitimately diverge (text rules can't see images)
                kind = "explained" if r.reason in ("chart_image", "unreadable") \
                       else "UNEXPLAINED"
                rec["divergence"] = kind
                stats[f"diverge:{kind}"] += 1
                if kind == "UNEXPLAINED":
                    diverged.append((mid, expected, r.action, r.reason))

            if r.action in ("buy", "buy_more", "exit") and r.ticker and r.strike \
                    and r.right:
                chain = resolve(resolver, r.ticker, r.right)
                if not chain:
                    rec["order"] = "skip:no_chain"
                    stats["order:skip_no_chain"] += 1
                else:
                    exp, strikes = chain
                    snapped = min(strikes, key=lambda s: abs(s - float(r.strike)))
                    entry = r.price_ask or r.price_last or r.price_bid
                    d = {"ticker": r.ticker, "option_type": r.right,
                         "strike": snapped,
                         "expiry": f"{exp[:4]}-{exp[4:6]}-{exp[6:]}",
                         "first_target": r.first_target, "limit_price": entry}
                    rec["resolved"] = f"{r.ticker} {snapped} {r.right} {exp}"
                    if r.action in ("buy", "buy_more") \
                            and available_funds(resolver) < LOW_FUNDS:
                        flatten(" (low funds)")
                        stats["flatten:low_funds"] += 1
                    try:
                        if r.action == "buy":
                            res = ibc._place_bracket_sync(d)
                        elif r.action == "buy_more":
                            res = ibc._buy_more_sync(d)
                        else:
                            res = ibc._emergency_exit_sync(d)
                    except Exception as e:
                        res = {"success": False, "error": f"crash: {e!r}"}
                    rec["order"] = {k: res.get(k) for k in
                                    ("success", "acted", "skip_reason", "error",
                                     "qty", "status", "filled", "bought",
                                     "held_before", "held_after", "sell_status")}
                    if res.get("success") and res.get("acted", True):
                        stats[f"order:{r.action}_ok"] += 1
                    elif res.get("success"):
                        stats[f"order:{r.action}_skip"] += 1
                    else:
                        stats[f"order:{r.action}_FAIL"] += 1

            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()

            if i % 25 == 0:
                el = time.time() - t0
                print(f"[{i}/{len(rows)}] {el/60:.0f}min  "
                      f"{dict(stats)}", flush=True)

    resolver.disconnect()

    # ---- flatten ----
    print("flattening...", flush=True)
    n_cancel, n_close, left = flatten(" (final)")

    HALT.unlink(missing_ok=True)     # restore armed state

    # ---- summary ----
    cost = usage["in"] * 1.00 / 1e6 + usage["out"] * 5.00 / 1e6
    # Recompute stats over the FULL results file so a resumed run reports the
    # whole simulation, not just its own segment.
    full = Counter()
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        full[f"action:{r.get('action')}"] += 1
        if r.get("divergence"):
            full[f"diverge:{r['divergence']}"] += 1
        o = r.get("order")
        if isinstance(o, dict):
            if o.get("success") and o.get("acted", True):
                full["order:ok"] += 1
            elif o.get("success"):
                full["order:skip"] += 1
            else:
                full["order:FAIL"] += 1
        elif o == "skip:no_chain":
            full["order:skip_no_chain"] += 1
    print("\n" + "=" * 60, flush=True)
    print("SIMULATION COMPLETE", flush=True)
    print(f"runtime (this segment): {(time.time()-t0)/3600:.2f} h", flush=True)
    print(f"api cost (this segment): ${cost:.2f}  "
          f"({usage['in']:,} in / {usage['out']:,} out tokens)", flush=True)
    for k in sorted(full):
        print(f"  {k:28} {full[k]}", flush=True)
    print(f"flatten          : {n_cancel} orders cancelled, "
          f"{n_close} positions closed, leftovers: {left or 'none'}", flush=True)
    if diverged:
        print(f"\nUNEXPLAINED divergences ({len(diverged)}):", flush=True)
        for mid, exp_, got, why in diverged[:40]:
            print(f"  msg {mid}: expected {exp_}, got {got} ({why})", flush=True)


if __name__ == "__main__":
    main()
