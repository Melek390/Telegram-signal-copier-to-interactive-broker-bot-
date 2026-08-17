HELP = (
    "*IBKR Options Bot* — fully automated\n\n"
    "Signals from the channel are traded automatically. Manual actions exist "
    "only as the buttons the bot offers on its own notifications.\n\n"
    "*Account:*\n"
    "`status`          — bot state, size & delay settings\n"
    "`details`         — account summary + live order-book status\n"
    "`open positions`  — view positions\n"
    "`pending orders`  — view working orders\n"
    "`size`            — set $ spent per signal\n"
    "`delay`           — set the after-open position re-check delay\n\n"
    "*Session:*\n"
    "`wake up`         — start gateway & resume trading\n"
    "`sleep`           — 🛑 halt trading & stop gateway\n"
    "`logout`          — clean IBKR logout\n"
    "`login`           — switch paper/live account\n\n"
    "Use /cancel at any time to leave a prompt."
)


LEFT_PROMPT = ("✋ Left the previous prompt — it was still waiting for input.\n"
               "Send your command again now.")

WAKING_UP = "Connecting to IBKR, please wait..."

RECLAIMING_DATA = (
    "*Reclaiming market data* 📡\n\n"
    "A phone/web login is holding the live data share. Restarting the gateway to "
    "take it back — *that session will lose market data.*\n\n"
    "About a minute..."
)

def _broker_note(reason: str) -> str:
    """
    Raw IBKR error strings are written for machines. Clean them up and, for the
    codes we know, lead with a plain-English translation.
    """
    if not reason:
        return ""
    r = reason.replace("<br>", " ").replace("\n", " ").strip()
    plain = []
    if "10197" in r:
        plain.append("no live market data at that moment (another session held it)")
    if "aggressive" in r:
        plain.append("the broker refused the limit price as too far from the "
                     "real market")
    if "Available Funds" in r or "insufficient" in r.lower():
        plain.append("the account did not have enough funds")
    head = ("In plain words: " + "; ".join(plain) + ".\n" if plain else "")
    return f"\n\n{head}_Broker details: {r}_"


def exit_done(sig: dict, r: dict) -> str:
    """We closed (or are closing) a position on a خفف-with-no-target signal."""
    head = f"🚨 *EMERGENCY EXIT — {_contract_line(sig)}*\n\n"
    body = "The channel says get out — closing the whole position.\n\n"

    if r.get("cancelled_id"):
        body += (f"✔️ Old take-profit at *${r['limit_price']}* cancelled "
                 f"(order `{r['cancelled_id']}`)\n")

    et = str(r.get("exit_type", ""))
    rejected = "rejected" in et
    filled = float(r.get("filled") or 0)
    held = r.get("held", 0)

    if held and filled >= held:
        how = "at market" if et.startswith("market") else "with the price ladder"
        body += (f"✔️ *SOLD all {int(filled)} contract(s)* {how} — "
                 f"fill price *${r.get('avg_price')}*\n")
        if rejected:
            body += (f"   _(the broker refused the mid-price sell at "
                     f"${r.get('exit_px')}, so it went straight to market)_\n")
        body += "\n*Position is CLOSED* ✅"
    elif filled > 0:
        body += (f"⏳ *Partly done*: sold {int(filled)} of {held} so far — "
                 f"order `{r.get('order_id')}` is `{r.get('status')}`.\n")
        if rejected:
            body += ("   _(the mid-price sell was refused, the rest was re-sent "
                     "at market)_\n")
        body += ("\nThe ladder has ended — *tap the button below* to sell the "
                 "rest at market.")
    else:
        body += (f"⏳ *Not sold yet* — the sell ladder for {held} contract(s) "
                 f"ended (`{r.get('status')}`).\n"
                 "\n*Tap the button below* to sell at market.")
    return head + body + _broker_note(r.get("reason", ""))


def buy_no_data(sig: dict) -> str:
    """Signal parsed fine, but there is no live data to price the entry."""
    return (f"📵 *Signal read — NOT executed: {_contract_line(sig)}*\n\n"
            f"I parsed the signal correctly "
            f"(target {('$' + str(sig.get('first_target'))) if sig.get('first_target') else '—'}, "
            f"card price {('$' + str(sig.get('limit_price'))) if sig.get('limit_price') else '—'}), "
            f"but I have *no live market data* to price the entry — another "
            f"session is holding it.\n\n"
            f"Nothing was placed. *Tap the button* to buy at market anyway, "
            f"or ignore to skip this signal. (`wake up` reclaims the data for "
            f"next time.)")


def exit_no_data(sig: dict, r: dict) -> str:
    """Exit signal parsed fine, but no live data — position left untouched."""
    tp = (f"the take-profit at *${r['limit_price']}* is still resting"
          if r.get("limit_price") else "no take-profit is resting")
    return (f"📵 *EXIT signal read — NOT executed: {_contract_line(sig)}*\n\n"
            f"The channel says get out of the *{r.get('held')}* contract(s) we "
            f"hold, but I have *no live market data* to run the sell ladder — "
            f"another session is holding it.\n\n"
            f"I touched nothing: {tp}.\n"
            f"*Tap the button* to sell everything at market now, or `wake up` "
            f"to reclaim the data and resend the signal flow.")


def exit_failed(sig: dict, error: str) -> str:
    """An exit we could not carry out. Silence here would hide an open position."""
    return (f"*EXIT FAILED — {_contract_line(sig)}* 🛑\n\n"
            f"`{error}`\n\n"
            f"*You may still be holding this.* Check `open positions`.")


M2M_EXPIRED = ("This button has expired (the bot restarted since it was sent). "
               "Use `pending orders` to modify or cancel the order.")

RETRY_EXPIRED = ("This button has expired (the bot restarted since it was sent). "
                 "Place the order through the manual `buy` flow instead.")


def tp_added(sig: dict, r: dict) -> str:
    """The channel edited the targets in after our entry filled."""
    return (f"*Take-profit added — {_contract_line(sig)}* ✅\n\n"
            f"The channel edited the targets in after our entry filled.\n"
            f"SELL {r['held']} @ *${r['target']}* DAY — order `{r['order_id']}`, "
            f"status `{r.get('status')}`")


def tp_add_failed(sig: dict, error: str) -> str:
    return (f"*Take-profit could NOT be added — {_contract_line(sig)}* 🛑\n\n"
            f"`{error}`\n\n"
            f"*The position has no exit resting.* Place the sell by hand — "
            f"check `open positions`.")


PREMARKET_WAKE = ("*Market opens in 30 minutes* ⏰\n\n"
                  "The bot is asleep — send `wake up` so it is ready before the open.")


def sleep_reminder(hours_asleep: float) -> str:
    """Nudge once the bot has been asleep past the grace period (5 min)."""
    dur = (f"{hours_asleep * 60:.0f} min" if hours_asleep < 1
           else f"{hours_asleep:.1f} h")
    return (f"*Bot is in sleep mode* 😴 — for {dur} now.\n\n"
            f"No signals are being traded and the gateway is down.\n"
            f"Send `wake up` to resume, or snooze this reminder:")


def guard_gateway_down(minutes_down: int) -> str:
    """The guard caught the gateway dead OUTSIDE sleep mode."""
    return (f"*GUARD ALERT* 🚨 — *gateway connection LOST outside sleep mode.*\n\n"
            f"The bot should be awake but IB Gateway has not responded for "
            f"~{minutes_down} min — a crash, a failed `wake up`, or a login "
            f"problem. *Signals CANNOT trade right now.*\n\n"
            f"Send `wake up` to restart the gateway.")


def guard_no_data(book: dict) -> str:
    """Awake, gateway up, but the live market data is not ours."""
    if book.get("competing"):
        why = ("another session holds the live market data (error 10197) — "
               "probably the client's phone/web login")
    else:
        why = "this account is NOT subscribed to live market data (error 354)"
    return (f"*GUARD* ⚠️ — bot is awake but *without live market data*.\n\n"
            f"Cause: {why}.\n\n"
            f"Orders still work: sized from the signal card, placed at market.\n"
            f"Send `wake up` to reclaim the data — or snooze if this is intentional:")


def guard_snoozed(seconds: int) -> str:
    label = "15 minutes" if seconds < 3600 else "12 hours"
    return (f"Snoozed 😴 for {label} — after that, the 1-minute alerts resume "
            f"unless the issue is resolved by then.")


def guard_resolved(cond: str) -> str:
    what = ("live market data is back with the bot" if cond == "no_data"
            else "gateway connection is restored")
    return f"*Guard* ✅ — {what}. No more alerts for this."


def tp_sweep_report(results: list) -> str:
    """Outcome of the 09:32:10 ET take-profit re-arm sweep."""
    if not results:
        return ("*Morning TP sweep* ☀️ — nothing to do "
                "(no bot-managed positions without a resting sell).")
    lines = ["*Morning TP sweep* ☀️ — position re-check after the open:\n"]
    for r in results:
        s = r.get("strike")
        strike = int(s) if s and s == int(s) else s
        name = f"{r.get('ticker')} {r.get('option_type', '')} {strike} exp {r.get('expiry')}"
        a = r.get("action")
        if a == "market_sell":
            lines.append(
                f"• {name}\n  Price *${r.get('mid')}* is ABOVE yesterday's target "
                f"*${r['target']}* → *SOLD {r.get('held')}* ⚡ "
                f"({r.get('how', 'market')}) — "
                f"filled {int(r.get('filled') or 0)} @ ${r.get('avg_price')} "
                f"(`{r.get('status')}`)")
        elif a == "rearmed":
            lines.append(
                f"• {name}\n  Price {('$' + str(r.get('mid'))) if r.get('mid') else 'unavailable'} "
                f"below target → *TP re-armed* ✅ SELL {r.get('held')} @ "
                f"*${r['target']}* DAY (order `{r.get('order_id')}`)")
        elif a == "skip":
            lines.append(f"• {name}\n  Skipped — {r.get('detail')}.")
        else:
            lines.append(f"• {name}\n  ⚠️ {r.get('detail')}")
    return "\n".join(lines)


def premarket_up(summary: dict, book_line: str) -> str:
    """Daily 09:00 ET status when the bot is already running."""
    return ("*Pre-market check* ☀️ — market opens in 30 minutes.\n\n"
            "*Bot is UP* ✅\n"
            f"Account `{summary['account']}`\n"
            f"Net liq *${summary['net_liq']:,.2f}* — "
            f"available *${summary['avail_funds']:,.2f}* — "
            f"{summary['open_pos']} open position(s)\n\n"
            + book_line +
            "\n\n_A ⚠️ no-quote book before the open can be normal, not a failure — "
            "full quotes appear at 09:30 ET (13:30 UTC). If it still shows ⚠️ "
            "after the open, send `wake up` to re-check._")


def orderbook_line(r: dict) -> str:
    """
    Live order-book confirmation for the wake-up message. A two-sided quote on the
    sample contract proves the live options feed is ours — the thing mid-limit
    orders will depend on once the account is live.
    """
    if not r.get("success"):
        return (f"*Live order book* ⚠️ — check failed: `{r.get('error')}`")
    if r.get("competing"):
        return ("*Live order book* 🛑 — your live session holds the market data "
                "(error 10197). The bot will NOT kick it — orders still work, "
                "sized on the signal card price instead of live quotes.")
    bid, ask = r.get("bid"), r.get("ask")
    if bid and ask:
        return (f"*Live order book* ✅ — subscribed.\n"
                f"Sample {r['desc']}:  bid *${bid}* / ask *${ask}*")
    if r.get("no_sub"):
        return (f"*Live order book* 🛑 — *NOT subscribed to live market data* "
                f"(sample {r['desc']} answered \"not subscribed\"). Orders still "
                f"work — sizing falls back to the signal card price — but mid-price "
                f"orders will need a live data subscription on this account.")
    return (f"*Live order book* ⚠️ — no two-sided quote on the sample "
            f"({r['desc']}: bid {bid or '—'} / ask {ask or '—'}). "
            f"Normal outside market hours; otherwise check the data subscription.")


def auto_m2m(leg: str, sig: dict, r: dict) -> str:
    """Automatic mid-limit -> market fallback fired (no button press involved)."""
    what = "entry buy" if leg == "entry" else "exit sell"
    return (f"*AUTO fallback — {_contract_line(sig)}*\n"
            f"The mid-price {what} did not fill in time.\n\n" + m2m_result(r))


def m2m_result(r: dict) -> str:
    """Outcome of the Switch-to-MARKET button."""
    if not r.get("success"):
        return f"*Switch to market failed* 🛑\n\n`{r.get('error')}`"
    filled = float(r.get("filled") or 0)
    head = f"*Switched to MARKET* ⚡ — {r['action']} {r['qty']} contract(s)\n\n"
    if filled >= r["qty"]:
        body = f"*FILLED* ✅ at *${r.get('avg_price')}* (order `{r['order_id']}`)"
    else:
        body = (f"Market order sent — status `{r.get('status')}`, "
                f"order `{r['order_id']}`")
    if r.get("exit_id"):
        body += (f"\nTake-profit re-attached at *${r['target']}* DAY "
                 f"(order `{r['exit_id']}`)")
    if r.get("reason"):
        body += _broker_note(r["reason"])
    return head + body


def buy_more_done(sig: dict, r: dict) -> str:
    """Averaged into a held position on a المتوسط signal."""
    head = f"*Averaged In — {_contract_line(sig)}*\n\n"

    body = ""
    if r.get("cancelled_id"):
        body += (f"Cancelled take-profit at *${r['old_target']}* "
                 f"(order `{r['cancelled_id']}`)\n")

    bought = float(r.get("bought") or 0)
    if bought > 0:
        body += (f"*BOUGHT {int(bought)} more @ ${r.get('avg_price')}* ✅\n"
                 f"Position: {r['held_before']} → *{r['held_after']}* contract(s)\n")
    else:
        body += (f"⚠️ Buy did not fill — status `{r.get('buy_status')}`, "
                 f"order `{r.get('buy_id')}`. Position unchanged "
                 f"at {r['held_before']}.\n")
    if r.get("fallback_qty"):
        body += (f"_Unfilled {r['fallback_qty']} auto-switched to MARKET — "
                 f"status `{r.get('fallback_status')}`._\n")

    if r.get("sell_id"):
        body += (f"\n*Take-profit re-placed* ✅\n"
                 f"SELL {r['sell_qty']} @ *${r['target']}* DAY — "
                 f"order `{r['sell_id']}`, status `{r.get('sell_status')}`")
    else:
        body += ("\n*⚠️ NO take-profit is resting.* "
                 "Check `pending orders` and place the exit by hand.")

    if r.get("price_src") == "signal card":
        body += f"\n_Sized on the card price ${r['entry_est']} — no live quote._"
    if r.get("reason"):
        body += _broker_note(r["reason"])
    return head + body


def buy_more_failed(sig: dict, error: str) -> str:
    """A buy_more that broke mid-sequence — the take-profit may have been cancelled."""
    return (f"*AVERAGE-IN FAILED — {_contract_line(sig)}* 🛑\n\n"
            f"`{error}`\n\n"
            f"*The take-profit may have been cancelled without a replacement.* "
            f"Check `open positions` and `pending orders` now.")


def buy_failed(sig: dict, error: str) -> str:
    return (f"*Buy not placed — {_contract_line(sig)}*\n\n`{error}`")


def settings_line(budget: float, delay: str) -> str:
    """Size + sweep-delay footer for the wake-up card (owner, 2026-08-16)."""
    return (f"Size *${budget:,.0f}* per signal  ·  "
            f"Delay *{delay}* after the open")


def status_card(s: dict) -> str:
    """Instant bot-state card for the `status` command (no broker round-trips)."""
    return (
        "*Bot Status*\n\n"
        f"Mode     :  *{s['mode']}*\n"
        f"Trading  :  {'🛑 HALTED (asleep)' if s['halted'] else '✅ ARMED'}\n"
        f"Gateway  :  {'✅ up' if s['gateway_up'] else '🛑 DOWN'}\n"
        f"Account  :  `{s['account'] or '—'}`\n\n"
        f"Size     :  *${s['budget']:,.0f}* per signal\n"
        f"Delay    :  *{s['delay']}* after the 09:30 ET open\n"
    )


def delay_prompt(current: str) -> str:
    return (f"*Morning sweep delay*\n\n"
            f"How long after the 09:30 ET open should the bot re-check open "
            f"positions (re-arm take-profits / sell gapped winners)?\n\n"
            f"Current: *{current}*\n\n"
            f"Reply with seconds (`130`) or minutes:seconds (`2:10`).\n"
            f"Type /cancel to leave it unchanged.")


def delay_set(old: str, new: str) -> str:
    return (f"*Morning sweep delay updated* ✅\n\n"
            f"{old}  →  *{new}* after the open.\n"
            f"Applies from the next sweep — even later this morning if it has "
            f"not run yet.")


DELAY_INVALID = (
    "Please reply with seconds (`130`) or minutes:seconds (`2:10`), "
    "up to 4 hours.\n\nType /cancel to leave it unchanged."
)


SIZE_INVALID = (
    "Please enter a dollar amount, for example `1000`.\n\n"
    "Type /cancel to leave it unchanged."
)


def size_prompt(current: float) -> str:
    return (
        "*Order Size*\n\n"
        f"Currently *${current:,.0f}* per signal.\n\n"
        "How many dollars should each signal spend?\n"
        "_Reply with a number, e.g._ `1000`\n\n"
        "Quantity is this budget ÷ the live ask. "
        "This budget is the *only* limit on order size."
    )


def size_set(old: float, new: float) -> str:
    return (
        "*Order size updated* ✅\n\n"
        f"${old:,.0f}  →  *${new:,.0f}* per signal\n\n"
        "_Applies to the next signal — no restart needed._"
    )

SLEEPING = (
    "*Bot is sleeping — trading halted* 🛑\n\n"
    "Gateway disconnected. Watchdog stopped.\n"
    "*No orders can be placed until you wake the bot* — this survives a bot restart.\n"
    "You can now log into your IBKR account freely.\n\n"
    "Type `wake up` when you want to trade again."
)

TRADING_HALTED = (
    "*Trading is halted* 🛑\n\n"
    "The bot is asleep, so this order was *not* placed.\n\n"
    "Type `wake up` to resume trading."
)


LOGGED_OUT = (
    "*Logged out from IBKR* ✓\n\n"
    "Session closed cleanly.\n"
    "Type `login` to reconnect when you're ready to trade."
)

WAKE_UP_TIMEOUT = (
    "*Gateway did not respond in time*\n\n"
    "The watchdog is still running and will keep retrying.\n"
    "Try again in 1-2 minutes."
)

CANCELLED    = "Order cancelled. Type *buy* or *sell* to start a new order."
UNAUTHORIZED = "Unauthorized."


def wake_up_ok(summary: dict) -> str:
    return (
        f"*Bot is awake*\n\n"
        f"Gateway       :  connected\n"
        f"Account       :  `{summary['account']}`\n"
        f"Net Liq       :  *${summary['net_liq']:,.2f}*\n"
        f"Avail Funds   :  *${summary['avail_funds']:,.2f}*\n"
        f"Cash          :  *${summary['cash']:,.2f}*\n"
        f"Open Positions:  *{summary['open_pos']}*\n\n"
        f"Ready to trade."
    )


def progress(d: dict) -> str:
    parts = []
    if d.get("action"):
        parts.append(f"*{d['action']}*")
    if d.get("ticker"):
        parts.append(d["ticker"])
    if d.get("option_type"):
        parts.append(d["option_type"])
    if d.get("strike") is not None:
        s = d["strike"]
        parts.append(str(int(s) if s == int(s) else s))
    if d.get("expiry"):
        parts.append(d["expiry"])
    if d.get("order_type"):
        if d["order_type"] == "limit":
            parts.append(f"${d['limit_price']}")
        else:
            parts.append(d["order_type"].upper())
    return (" · ".join(parts) + "\n\n") if parts else ""


def _mkt_line(mkt: dict | None) -> str:
    """Formats market data line. Returns empty string if no data."""
    if not mkt or not mkt.get("success"):
        return ""
    parts = []
    if mkt.get("bid")  is not None: parts.append(f"Bid ${mkt['bid']:.2f}")
    if mkt.get("ask")  is not None: parts.append(f"Ask ${mkt['ask']:.2f}")
    if mkt.get("last") is not None: parts.append(f"Last ${mkt['last']:.2f}")
    if not parts:
        return ""
    label = "Market ~" if mkt.get("delayed") else "Market"
    return f"{label}  :  {' · '.join(parts)}\n"


def _price_line(d: dict) -> str:
    ot = d.get("order_type", "mkt")
    if ot == "limit":
        return f"Price   :  *Limit @ ${d['limit_price']}*\n"
    action = d.get("action", "").lower()
    smart = "bid" if action == "buy" else "mid"
    return f"Price   :  *Market ({smart})*\n"


def order_summary(d: dict, mkt: dict | None = None) -> str:
    s = d["strike"]
    strike_display = int(s) if s == int(s) else s
    return (
        f"*Order Summary*\n\n"
        f"Action  :  *{d['action']}*\n"
        f"Ticker  :  *{d['ticker']}*\n"
        f"Type    :  *{d['option_type']}*\n"
        f"Strike  :  *{strike_display}*\n"
        f"Expiry  :  *{d['expiry']}*\n"
        f"{_price_line(d)}"
        f"{_mkt_line(mkt)}"
        f"Qty     :  *{d['size']}*\n"
        + (f"Holding :  *{d['position']}* contract(s)\n" if d.get("position") else "")
        + f"\nConfirm order?"
    )


def order_placed(d: dict, result: dict) -> str:
    s = d["strike"]
    strike_display = int(s) if s == int(s) else s
    reason = result.get("reason", "")
    reason_line = f"Reason    :  {reason}\n" if reason else ""
    return (
        f"*Order Placed*\n\n"
        f"{d['action']} {d['size']} x {d['ticker']} "
        f"{strike_display} {d['option_type']} {d['expiry']}\n"
        f"{_price_line(d)}\n"
        f"Order ID  :  `{result['order_id']}`\n"
        f"Status    :  `{result['status']}`\n"
        f"Filled    :  `{result['filled']}`\n"
        f"{reason_line}\n"
        f"Type *buy* or *sell* to place another order."
    )


def positions_list(positions: list) -> str:
    if not positions:
        return "*Open Positions*\n\nNo open option positions."
    lines = ["*Open Positions*\n"]
    for i, p in enumerate(positions):
        s = p["strike"]
        strike = int(s) if s == int(s) else s
        expiry = p["expiry"]
        avg = f"avg cost ${p['avg_cost']:.2f}" if p["avg_cost"] else ""
        lines.append(
            f"{i+1}.  *{p['ticker']}*  {p['option_type']}  {strike}  •  exp {expiry}\n"
            f"     Qty: *{p['qty']}*  {avg}"
        )
    return "\n".join(lines)


def position_close_prompt(p: dict) -> str:
    s = p["strike"]
    strike = int(s) if s == int(s) else s
    qty      = p.get("qty", 0)
    held     = abs(qty)                      # examples must be positive, even for shorts
    side_note = " (short)" if qty < 0 else ""
    return (
        f"*Close Position*\n\n"
        f"{p['ticker']}  {p['option_type']}  {strike}  •  exp {p['expiry']}\n"
        f"You hold: *{qty}* contract(s){side_note}\n\n"
        f"Enter quantity and price:\n"
        f"• `{held} mkt`   — close all at market\n"
        f"• `5 mkt`        — partial at market\n"
        f"• `{held} 1.80`  — close all at limit $1.80\n\n"
        f"Type `0` to go back."
    )


def position_close_summary(p: dict, qty: int, order_type: str, limit_price, mkt: dict | None = None) -> str:
    s = p["strike"]
    strike = int(s) if s == int(s) else s
    price_line = f"*Limit @ ${limit_price}*" if order_type == "limit" else "*Market*"
    # Long positions are closed by selling, short positions by buying back
    close_action = "Sell" if p.get("qty", 0) >= 0 else "Buy"
    return (
        f"*Close Order Summary*\n\n"
        f"{close_action}  {qty}x  {p['ticker']}  {p['option_type']}  {strike}  •  exp {p['expiry']}\n"
        f"Price  :  {price_line}\n"
        f"{_mkt_line(mkt)}"
        f"\nConfirm?"
    )


def pending_orders_list(orders: list) -> str:
    if not orders:
        return "*Pending Orders*\n\nNo pending orders."
    lines = ["*Pending Orders*\n"]
    for i, o in enumerate(orders):
        s = o["strike"]
        strike = int(s) if s == int(s) else s
        price = f"Limit ${o['limit_price']}" if o["order_type"] == "limit" else "Market"
        if o.get("manual"):
            price += "  · placed manually"
        lines.append(
            f"{i+1}.  *{o['action']}*  {o['qty']}x  {o['ticker']}  {o['option_type']}  {strike}  •  exp {o['expiry']}\n"
            f"     {price}  •  `{o['status']}`"
        )
    return "\n".join(lines)


def order_detail(o: dict) -> str:
    s = o["strike"]
    strike = int(s) if s == int(s) else s
    price = f"Limit ${o['limit_price']}" if o["order_type"] == "limit" else "Market"
    return (
        f"*Order #{o['order_id']}*\n\n"
        f"{o['action']}  {o['qty']}x  {o['ticker']}  {o['option_type']}  {strike}  •  exp {o['expiry']}\n"
        f"Price  :  {price}\n"
        f"Status :  `{o['status']}`\n\n"
        f"What would you like to do?"
    )


def order_modify_confirm(o: dict, new_price) -> str:
    s = o["strike"]
    strike = int(s) if s == int(s) else s
    action = o.get("action", "").lower()
    new_price_display = f"*${new_price}*" if new_price is not None else f"*Market ({'bid' if action == 'buy' else 'mid'})*"
    return (
        f"*Modify Order #{o['order_id']}*\n\n"
        f"{o['action']}  {o['qty']}x  {o['ticker']}  {o['option_type']}  {strike}  •  exp {o['expiry']}\n"
        f"Old price  :  ${o['limit_price']}\n"
        f"New price  :  {new_price_display}\n\n"
        f"Confirm change?"
    )


def order_failed(error: str) -> str:
    return (
        f"*Order Failed*\n\n"
        f"`{error}`\n\n"
        f"Type *buy* or *sell* to try again."
    )


def signal_partial(direction: str, order: dict, missing: list) -> str:
    """Shown when OCR could not read some critical fields. Shows what was parsed, asks for first missing."""
    label = "BUY" if direction == "BUY" else "SELL"
    _opt = {"C": "Call", "P": "Put"}
    lines = [f"*Signal detected: {label}*\n", "*Partial read — some fields missing:*\n"]
    field_display = {
        "ticker":      ("Ticker ", order.get("ticker")),
        "option_type": ("Type   ", _opt.get(order.get("option_type", ""), order.get("option_type"))),
        "strike":      ("Strike ", str(int(order["strike"])) if order.get("strike") and order["strike"] == int(order["strike"]) else str(order.get("strike")) if order.get("strike") else None),
        "expiry":      ("Expiry ", order.get("expiry")),
    }
    for field, (label_str, val) in field_display.items():
        if field in missing:
            lines.append(f"{label_str}:  _missing_")
        elif val:
            lines.append(f"{label_str}:  *{val}*")
    prompts = {
        "ticker":      "Enter *ticker* (e.g. `SPX`, `TSLA`):",
        "option_type": "Enter *option type* — `call` or `put`:",
        "strike":      "Enter *strike price* (e.g. `500`):",
        "expiry":      "Enter *expiry date* (DDMM — e.g. `0506` for May 6):",
    }
    lines.append("\n" + prompts.get(missing[0], f"Enter {missing[0]}:"))
    return "\n".join(lines)


def _contract_line(sig: dict) -> str:
    s = sig["strike"]
    strike = int(s) if s == int(s) else s
    return f"{sig['ticker']}  {strike}  {sig['option_type']}  exp {sig['expiry']}"


def bracket_placed(sig: dict, r: dict) -> str:
    """
    Automated entry result. Reports each leg separately so it is obvious which steps
    completed: the buy, its fill, and whether the take-profit is actually resting.
    """
    filled = float(r.get("filled") or 0)
    qty = r["qty"]
    fully = filled >= qty and qty > 0

    # ---- step 1: the buy ----
    if fully:
        avg = r.get("avg_price") or r["entry"]
        step1 = (f"*1. BUY — FILLED* ✅\n"
                 f"     {int(filled)} contract(s) @ *${avg}*\n"
                 f"     Cost: *${filled * float(avg) * 100:,.2f}*\n")
    elif filled > 0:
        step1 = (f"*1. BUY — PARTIALLY FILLED* ⏳\n"
                 f"     {int(filled)} of {qty} @ ${r.get('avg_price') or r['entry']}\n")
    else:
        how = (f"limit *${r['entry']}* (mid)"
               if str(r.get("entry_type", "")).startswith("limit") else "*MARKET*")
        step1 = (f"*1. BUY — WORKING* ⏳\n"
                 f"     {qty} contract(s) at {how}\n"
                 f"     Status: `{r['status']}`\n")
    step1 += f"     Order `{r['order_id']}`\n"

    # Sized off the card when the live quote was unavailable — say so, because the
    # fill can land some way from that estimate.
    if r.get("price_src") == "signal card":
        step1 += (f"     _Sized on the card price ${r['entry']} — no live quote "
                  f"(competing live session)._\n")

    ladder = str(r.get("entry_type", "")).startswith("limit (ladder")

    # ---- step 2: the take-profit ----
    if not r.get("target"):
        step2 = ("\n*2. SELL at target — NOT PLACED* ⚠️\n"
                 "     The signal had no target price.\n")
    elif fully or (ladder and r.get("exit_id")):
        step2 = (f"\n*2. SELL at 1st target — ACTIVE* ✅\n"
                 f"     SELL {int(filled)} limit *${r['target']}* DAY, "
                 f"order `{r['exit_id']}`\n"
                 f"     Status: `{r.get('exit_status') or 'Submitted'}`\n")
    else:
        step2 = (f"\n*2. SELL at 1st target — ARMED* 🕓\n"
                 f"     Limit *${r['target']}* DAY, order `{r['exit_id']}`\n"
                 f"     Status: `{r.get('exit_status') or 'PreSubmitted'}` "
                 f"— activates the moment the buy fills\n")

    extra = ""
    if r.get("reason"):
        extra += _broker_note(r["reason"]) + "\n"
    if not fully and ladder:
        rest = int(qty - filled)
        extra += (f"\n⚠️ _The ladder ended with {rest} of {qty} NOT bought — "
                  f"tap the button below to buy the rest at market, or ignore "
                  f"to let it go._")
    elif not fully:
        extra += "\n_You'll get a follow-up when the buy fills._"

    return (f"*Automated Order — {_contract_line(sig)}*\n\n{step1}{step2}{extra}")


def bracket_fill_update(sig: dict, st: dict) -> str:
    """Follow-up once the buy fills, confirming the take-profit actually went live."""
    qty = st.get("filled_qty")
    avg = st.get("avg_price")
    fill_line = (f"Filled *{int(qty)}* @ *${avg}*\n" if qty and avg
                 else "The buy has filled.\n")
    if st.get("exit_status"):
        exit_line = (f"*SELL at 1st target is now live* ✅\n"
                     f"Limit ${sig.get('first_target')} DAY — status "
                     f"`{st['exit_status']}`")
    else:
        exit_line = ("⚠️ *The take-profit is not in the open orders.*\n"
                     "Check `pending orders` — the position may be unprotected.")
    return f"*Fill Update — {_contract_line(sig)}*\n\n{fill_line}\n{exit_line}"


def bracket_gone(sig: dict, order_id) -> str:
    """
    The buy left the order book with no execution behind it — cancelled, expired at the
    close, or rejected. Never report this as a fill.
    """
    return (
        f"*Order Not Working — {_contract_line(sig)}*\n\n"
        f"Order `{order_id}` is no longer in the open orders and *no fill was "
        f"recorded*.\n\n"
        f"That means it was cancelled, rejected, or expired at the close — "
        f"*you have no position and no exit order.*\n\n"
        f"Check `pending orders`, and `details` to confirm."
    )


def signal_header(direction: str) -> str:
    label = "BUY" if direction == "BUY" else "SELL"
    return f"Signal detected: *{label}*"


def signal_missing_price(d: dict) -> str:
    """Partial signal summary shown when OCR could not extract a price."""
    s = d["strike"]
    strike_display = int(s) if s == int(s) else s
    return (
        f"*Order Summary*\n\n"
        f"Action  :  *{d['action']}*\n"
        f"Ticker  :  *{d['ticker']}*\n"
        f"Type    :  *{d['option_type']}*\n"
        f"Strike  :  *{strike_display}*\n"
        f"Expiry  :  *{d['expiry']}*\n"
        f"Price   :  _missing_\n"
        f"Qty     :  *{d['size']}*\n\n"
        f"Enter price (e.g. `1.50`) or `mkt` for market:"
    )
