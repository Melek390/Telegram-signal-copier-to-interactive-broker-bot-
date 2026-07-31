HELP = (
    "*IBKR Options Bot*\n\n"
    "*One-line order:*\n"
    "`buy tsla c500 0605 1.8 2`   — limit at $1.80\n"
    "`buy tsla c500 0605 mkt 2`   — market order\n\n"
    "*Format:* `action ticker c/p+strike DDMM price qty`\n"
    "Market orders: buy fills at bid, sell fills at mid.\n\n"
    "*Step by step:* just type `buy` or `sell`\n\n"
    "*Account:*\n"
    "`details`         — show account summary\n"
    "`open positions`  — view & close positions\n"
    "`pending orders`  — view, cancel & modify orders\n\n"
    "Use /cancel at any time to start over."
)

WAKING_UP = "Connecting to IBKR, please wait..."

SLEEPING = (
    "*Bot is sleeping*\n\n"
    "Gateway disconnected. Watchdog stopped.\n"
    "You can now log into your IBKR account freely.\n\n"
    "Type `wake up` when you want to trade again."
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
    lines.append("\nSelect a position to close:")
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
        lines.append(
            f"{i+1}.  *{o['action']}*  {o['qty']}x  {o['ticker']}  {o['option_type']}  {strike}  •  exp {o['expiry']}\n"
            f"     {price}  •  `{o['status']}`"
        )
    lines.append("\nSelect an order to manage:")
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
