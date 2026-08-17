import functools
import http.server
import json
import os
import re
import secrets
import subprocess
import asyncio
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import ConversationHandler, ContextTypes

from . import callbacks as cb
from . import messages as msg
from .keyboards import (
    option_type_keyboard, confirm_keyboard, confirm_change_keyboard,
    positions_keyboard, order_list_keyboard, order_action_keyboard,
    signal_confirm_keyboard, signal_confirm_change_keyboard,
    login_mode_keyboard, switch_to_market_keyboard, guard_snooze_keyboard,
    manual_retry_keyboard,
)
from ibkr.client import (
    place_order as ibkr_place_order,
    place_bracket_order as ibkr_place_bracket_order,
    get_bracket_status as ibkr_get_bracket_status,
    get_position as ibkr_get_position,
    get_account_summary as ibkr_get_account_summary,
    get_open_positions as ibkr_get_open_positions,
    get_pending_orders as ibkr_get_pending_orders,
    cancel_order as ibkr_cancel_order,
    modify_order as ibkr_modify_order,
    get_market_data as ibkr_get_market_data,
    switch_to_market as ibkr_switch_to_market,
    orderbook_check as ibkr_orderbook_check,
    morning_tp_sweep as ibkr_morning_tp_sweep,
    last_account as ibkr_last_account,
)

# Conversation states
TICKER, OPTION_TYPE, STRIKE, DATE, PRICE, QTY, CONFIRM = range(7)
# Positions states
POS_CLOSE_INPUT, POS_CLOSE_CONFIRM = range(10, 12)
# Orders states
ORD_ACTION, ORD_NEW_PRICE, ORD_MODIFY_CONFIRM = range(20, 23)
# Login states
LOGIN_MODE, LOGIN_ID, LOGIN_PASSWORD = range(30, 33)
# Order size state
SIZE_INPUT = 40
# Morning-sweep delay state
DELAY_INPUT = 41


def _sweep_delay() -> int:
    """Seconds after the 09:30 ET open before the TP re-arm sweep runs.
    Adjustable from Telegram via `delay`; clamped to [0 s, 4 h]."""
    try:
        return max(0, min(14400, int(os.getenv("SWEEP_DELAY_SECONDS", "130"))))
    except ValueError:
        return 130


def _parse_delay(text: str):
    """'130' = seconds, '2:10' = minutes:seconds. None when unparseable."""
    t = text.strip().replace(" ", "")
    try:
        if ":" in t:
            m, s = t.split(":", 1)
            val = int(m) * 60 + int(s)
        else:
            val = int(t)
    except ValueError:
        return None
    return val if 0 <= val <= 14400 else None


def _fmt_delay(seconds: int) -> str:
    return f"{seconds // 60}min {seconds % 60}s" if seconds >= 60 else f"{seconds}s"


# Command words that must ESCAPE any waiting prompt instead of being swallowed
# as its input. Root cause (2026-08-15, the client's "delay loop"): PTB keeps
# conversations per chat and several can be active at once; a hanging prompt's
# TEXT handler ate every later command and /cancel only ended the FIRST active
# conversation. With this, typing any command inside a prompt exits it cleanly.
COMMAND_WORDS = (r"(?i)^\s*(size|delay|status|open\s+positions?|"
                 r"pending\s+orders?|wake\s+up|sleep|details|login|logout|help)\s*$")

_authorized_ids: set[int] = set()
_active_login_server = None  # currently-running login HTTP server (so a new login can replace it)

# "Switch to MARKET" offers, keyed by the resting order's id. In-memory on purpose:
# after a bot restart the button answers with M2M_EXPIRED instead of guessing at a
# contract it no longer knows. callback_data is capped at 64 bytes, so the contract
# details cannot ride inside the button itself.
_m2m_store: dict[int, dict] = {}


def offer_switch_to_market(order_id: int, info: dict):
    """Register an unfilled order and return the keyboard for its notification."""
    _m2m_store[int(order_id)] = {**info, "order_id": int(order_id)}
    return switch_to_market_keyboard(int(order_id))


# Missed signals awaiting a manual "Place at MARKET" press, keyed by the channel
# message id. In-memory like the m2m store: a restart turns stale buttons into
# a polite "expired" answer instead of trading on forgotten context.
_retry_store: dict[int, dict] = {}


def offer_manual_retry(sig: dict):
    """Register a failed buy and return the retry keyboard for its notification."""
    key = int(sig.get("message_id") or 0)
    if not key:
        return None
    _retry_store[key] = dict(sig)
    return manual_retry_keyboard(key)

# ── Risk guards ────────────────────────────────────────────────────────────────
# Kill switch. Persisted as a file (NOT in memory) so a halt survives a bot restart
# and can also be tripped/cleared from the shell. Set by `sleep`, cleared by
# `wake up` / `login`.
_HALT_FILE = Path(__file__).resolve().parent.parent / ".trading_halted"


def _trading_halted() -> bool:
    return _HALT_FILE.exists()


def _set_trading_halt(halted: bool) -> None:
    try:
        if halted:
            _HALT_FILE.write_text(datetime.now().isoformat(timespec="seconds"))
        else:
            _HALT_FILE.unlink(missing_ok=True)
    except OSError:
        pass  # never let the guard file break a command


def _order_budget() -> float:
    """Dollars spent per automated signal. Mirrors ibkr.client._order_budget."""
    try:
        return max(1.0, float(os.getenv("ORDER_BUDGET_USD", "1000")))
    except ValueError:
        return 1000.0


def set_authorized_users(user_ids: list[int]) -> None:
    global _authorized_ids
    _authorized_ids = set(user_ids)


def authorized(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if _authorized_ids and update.effective_user.id not in _authorized_ids:
            if update.effective_message:
                await update.effective_message.reply_text(msg.UNAUTHORIZED)
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


@authorized
async def leave_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A command word arrived while a prompt was waiting — leave the prompt
    cleanly instead of swallowing the command as invalid input (the client's
    'delay loop', 2026-08-15)."""
    context.user_data.clear()
    await update.message.reply_text(msg.LEFT_PROMPT, parse_mode="Markdown")
    return ConversationHandler.END


# ── Full order parser ──────────────────────────────────────────────────────────

def _parse_mmdd(date_str: str) -> str | None:
    """Parse DDMM into YYYY-MM-DD. Returns None on failure."""
    today = datetime.today().date()
    if len(date_str) != 4 or not date_str.isdigit():
        return None
    try:
        dd, mm = int(date_str[:2]), int(date_str[2:])
        expiry = datetime(today.year, mm, dd).date()
        if expiry < today:
            expiry = datetime(today.year + 1, mm, dd).date()
        return expiry.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_price(price_str: str) -> tuple[str, float | None] | None:
    """
    Returns (order_type, limit_price) or None on failure.
    order_type: 'mkt' | 'limit'
    mkt → backend resolves to bid (buy) or mid (sell) automatically.
    """
    p = price_str.lower()
    if p in ("mkt", "market"):
        return "mkt", None
    try:
        val = float(p)
        if val <= 0:
            return None
        return "limit", val
    except ValueError:
        return None


def _parse_qty(qty_str: str, position: int = 0) -> int | None:
    """Resolve a quantity. `position` may be negative (short) — magnitude is what matters."""
    s = qty_str.strip().lower()
    held = abs(position)
    if s == "all":
        return held if held > 0 else None
    if s.endswith("%"):
        try:
            pct = float(s[:-1])
            if not (0 < pct <= 100) or held <= 0:
                return None
            return max(1, round(held * pct / 100))
        except ValueError:
            return None
    try:
        val = int(s)
        return val if val > 0 else None
    except ValueError:
        return None


def parse_full_order(text: str) -> dict | str:
    """
    Parse a one-line order string.
    Format: action ticker c/pSTRIKE DDMM price qty
    Returns a filled order dict on success, or an error string on failure.
    """
    parts = text.strip().split()
    if len(parts) != 6:
        return (
            "One-line format needs 6 parts:\n"
            "`buy tsla c500 0605 1.8 2`\n"
            "`buy tsla c500 0605 mkt 2`"
        )

    action_str, ticker_str, contract_str, date_str, price_str, qty_str = parts
    action_str = action_str.lower()

    if action_str not in ("buy", "sell"):
        return "First word must be `buy` or `sell`."

    ticker = ticker_str.upper()
    if not ticker.isalpha() or len(ticker) > 10:
        return "Invalid ticker symbol."

    contract_str = contract_str.lower()
    if not contract_str or contract_str[0] not in ("c", "p"):
        return "Contract must start with `c` or `p` — e.g. `c500` or `p480`."
    option_type = "Call" if contract_str[0] == "c" else "Put"
    try:
        strike = float(contract_str[1:])
        if strike <= 0:
            raise ValueError
    except ValueError:
        return "Invalid strike in contract — e.g. `c500` or `p480.5`."

    expiry = _parse_mmdd(date_str)
    if not expiry:
        return "Invalid date. Use DDMM — e.g. `0605` for May 6."

    price_result = _parse_price(price_str)
    if price_result is None:
        return "Price must be a number or `mkt`."
    order_type, limit_price = price_result

    qty_lower = qty_str.lower()
    if qty_lower == "all" or qty_lower.endswith("%"):
        if action_str != "sell":
            return "Percentage quantity only works for sell orders."
        size_raw = qty_lower  # resolved later after fetching position
    elif qty_str.isdigit() and int(qty_str) > 0:
        size_raw = int(qty_str)
    else:
        return "Quantity must be a positive number (or `50%`/`all` for sell orders)."

    return {
        "action":      action_str.capitalize(),
        "ticker":      ticker,
        "option_type": option_type,
        "strike":      strike,
        "expiry":      expiry,
        "order_type":  order_type,
        "limit_price": limit_price,
        "size":        size_raw,
    }


# ── Entry point ────────────────────────────────────────────────────────────────

@authorized
async def handle_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    tokens = text.split()

    # Pre-warm: kick off gateway silently so it's ready by confirm time
    if not _gateway_up():
        _start_watchdog()

    if len(tokens) == 1:
        # Step-by-step: user typed just "buy" or "sell"
        context.user_data.clear()
        context.user_data["action"] = tokens[0].capitalize()
        await update.message.reply_text(
            f"*{context.user_data['action']}*\n\n"
            f"Enter *ticker* (e.g. `TSLA`):",
            parse_mode="Markdown",
        )
        return TICKER

    # One-line order
    result = parse_full_order(text)
    if isinstance(result, str):
        await update.message.reply_text(result, parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data.update(result)

    # For sell orders always fetch position (for display + % resolution)
    if result["action"].lower() == "sell":
        position = await ibkr_get_position(result)
        context.user_data["position"] = position
        if isinstance(result["size"], str):
            qty = _parse_qty(result["size"], position)
            if not qty:
                await update.message.reply_text(
                    f"Could not resolve `{result['size']}` — you hold *{position}* contract(s). Enter a number instead.",
                    parse_mode="Markdown",
                )
                return ConversationHandler.END
            context.user_data["size"] = qty

    mkt = await ibkr_get_market_data(context.user_data) if _gateway_up() else None
    await update.message.reply_text(
        msg.order_summary(context.user_data, mkt),
        reply_markup=confirm_change_keyboard(),
        parse_mode="Markdown",
    )
    return CONFIRM


# ── Step 1: Ticker ─────────────────────────────────────────────────────────────

@authorized
async def ticker_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ticker = update.message.text.strip().upper()
    if not ticker.isalpha() or len(ticker) > 10:
        await update.message.reply_text(
            "Invalid ticker. Letters only (e.g. `TSLA`, `SPY`).",
            parse_mode="Markdown",
        )
        return TICKER

    context.user_data["ticker"] = ticker
    await update.message.reply_text(
        f"{msg.progress(context.user_data)}Choose option type:",
        reply_markup=option_type_keyboard(),
        parse_mode="Markdown",
    )
    return OPTION_TYPE


# ── Step 2: Call / Put ─────────────────────────────────────────────────────────

@authorized
async def option_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["option_type"] = query.data
    await query.edit_message_text(
        f"{msg.progress(context.user_data)}Enter *strike price* (e.g. `500`):",
        parse_mode="Markdown",
    )
    return STRIKE


# ── Step 3: Strike ─────────────────────────────────────────────────────────────

@authorized
async def strike_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        strike = float(text)
        if strike <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Invalid strike. Enter a positive number (e.g. `500` or `499.5`).",
            parse_mode="Markdown",
        )
        return STRIKE

    context.user_data["strike"] = strike
    await update.message.reply_text(
        f"{msg.progress(context.user_data)}Enter *expiry date* (DDMM, e.g. `0506` for May 6):",
        parse_mode="Markdown",
    )
    return DATE


# ── Step 4: Date ───────────────────────────────────────────────────────────────

@authorized
async def date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    expiry = _parse_mmdd(update.message.text.strip())
    if not expiry:
        await update.message.reply_text(
            "Invalid date. Use DDMM — e.g. `0605` for May 6.",
            parse_mode="Markdown",
        )
        return DATE

    context.user_data["expiry"] = expiry
    await update.message.reply_text(
        f"{msg.progress(context.user_data)}"
        f"Enter *price*:\n"
        f"• A number for limit order — e.g. `3.50`\n"
        f"• `mkt` for market order",
        parse_mode="Markdown",
    )
    return PRICE


# ── Step 5: Price ──────────────────────────────────────────────────────────────

@authorized
async def price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    result = _parse_price(update.message.text.strip())
    if result is None:
        await update.message.reply_text(
            "Enter a number (e.g. `3.50`) or `mkt`.",
            parse_mode="Markdown",
        )
        return PRICE

    order_type, limit_price = result
    context.user_data["order_type"]  = order_type
    context.user_data["limit_price"] = limit_price

    # One-liner or returning via Change Price — size already set, skip QTY
    if context.user_data.get("size") is not None:
        mkt = await ibkr_get_market_data(context.user_data) if _gateway_up() else None
        await update.message.reply_text(
            msg.order_summary(context.user_data, mkt),
            reply_markup=confirm_change_keyboard(),
            parse_mode="Markdown",
        )
        return CONFIRM

    position_line = ""
    qty_hint = "• A number — e.g. `2`"
    if context.user_data.get("action", "").lower() == "sell":
        position = await ibkr_get_position(context.user_data)
        context.user_data["position"] = position
        if position > 0:
            position_line = f"You hold *{position}* contract(s).\n\n"
            qty_hint += "\n• Percentage — e.g. `50%`\n• `all` to close full position"

    await update.message.reply_text(
        f"{msg.progress(context.user_data)}{position_line}"
        f"Enter *quantity*:\n{qty_hint}",
        parse_mode="Markdown",
    )
    return QTY


# ── Step 6: Quantity ───────────────────────────────────────────────────────────

@authorized
async def qty_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    position = context.user_data.get("position", 0)
    qty = _parse_qty(text, position)
    if qty is None:
        hint = ", `50%`, or `all`" if position > 0 else ""
        await update.message.reply_text(
            f"Invalid quantity. Enter a positive integer{hint}.",
            parse_mode="Markdown",
        )
        return QTY

    context.user_data["size"] = qty
    mkt = await ibkr_get_market_data(context.user_data) if _gateway_up() else None
    await update.message.reply_text(
        msg.order_summary(context.user_data, mkt),
        reply_markup=confirm_change_keyboard(),
        parse_mode="Markdown",
    )
    return CONFIRM


# ── Confirmation ───────────────────────────────────────────────────────────────

@authorized
async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CHANGE_PRICE:
        await query.edit_message_text(
            f"{msg.progress(context.user_data)}"
            f"Enter new price:\n"
            f"• A number for limit — e.g. `3.50`\n"
            f"• `mkt` for market",
            parse_mode="Markdown",
        )
        return PRICE

    if query.data == cb.CANCEL:
        await query.edit_message_text(msg.CANCELLED, parse_mode="Markdown")
        context.user_data.clear()
        return ConversationHandler.END

    # Risk guards — checked BEFORE _ensure_gateway so a halt can't be bypassed
    # by the gateway being restarted on demand.
    if _trading_halted():
        await query.edit_message_text(msg.TRADING_HALTED, parse_mode="Markdown")
        context.user_data.clear()
        return ConversationHandler.END

    if not await _ensure_gateway(query.edit_message_text):
        return ConversationHandler.END

    await query.edit_message_text("Placing order with IBKR...")

    order_data = dict(context.user_data)
    result = await ibkr_place_order(order_data)

    if result["success"]:
        await query.edit_message_text(
            msg.order_placed(order_data, result),
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            msg.order_failed(result["error"]),
            parse_mode="Markdown",
        )

    context.user_data.clear()
    return ConversationHandler.END


# ── Gateway helpers ────────────────────────────────────────────────────────────

def _watchdog_running() -> bool:
    r = subprocess.run(["tmux", "has-session", "-t", "gatewaywatchdog"], capture_output=True)
    return r.returncode == 0

def _gateway_up() -> bool:
    port = os.getenv("IBKR_PORT", "4002")
    r = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True)
    return f":{port}" in r.stdout

def _start_watchdog(reset: bool = False):
    """
    Start the gateway watchdog. reset=True restarts it even if running — needed
    because a watchdog that hit its failure cap parks forever, and only a fresh
    process gets a fresh counter.
    """
    if reset:
        subprocess.run(["tmux", "kill-session", "-t", "gatewaywatchdog"],
                       capture_output=True)
    if reset or not _watchdog_running():
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", "gatewaywatchdog", "/root/restart_gateway.sh"],
            capture_output=True,
        )


def _data_competing() -> bool:
    """True when a competing phone/web session holds our market data share."""
    try:
        r = subprocess.run(["timeout", "75", "python3", "/root/data_probe.py"],
                           capture_output=True)
        return r.returncode in (1, 124)
    except Exception:
        return False    # never let the probe break wake up


def _update_ibc_config(ibkr_id: str, password: str, mode: str) -> None:
    """Update IBC config.ini with new credentials and trading mode."""
    config_path = "/root/IBC/config.ini"
    with open(config_path, "r") as f:
        content = f.read()
    content = re.sub(r"^IbLoginId=.*$",  f"IbLoginId={ibkr_id}",  content, flags=re.MULTILINE)
    content = re.sub(r"^IbPassword=.*$", f"IbPassword={password}", content, flags=re.MULTILINE)
    content = re.sub(r"^TradingMode=.*$",f"TradingMode={mode}",    content, flags=re.MULTILINE)
    with open(config_path, "w") as f:
        f.write(content)


def _update_watchdog_script(port: str, mode: str) -> None:
    """Update restart_gateway.sh with the new port and trading mode."""
    script_path = "/root/restart_gateway.sh"
    with open(script_path, "r") as f:
        content = f.read()
    content = re.sub(r"grep -q :\d{4}",  f"grep -q :{port}", content)
    content = re.sub(r"Port \d{4} down",  f"Port {port} down", content)
    content = re.sub(r"--mode=\w+",       f"--mode={mode}",   content)
    with open(script_path, "w") as f:
        f.write(content)


def _update_env_value(key: str, value: str) -> None:
    """
    Update a key in .env and in the running process environment.

    Both halves matter: os.environ makes it take effect on the next order without
    a restart, and the .env write makes it survive one.
    """
    env_path = "/root/bot/.env"
    with open(env_path, "r") as f:
        lines = f.readlines()
    new_lines, found = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")
    with open(env_path, "w") as f:
        f.writelines(new_lines)
    os.environ[key] = value


def _update_env_port(port: str) -> None:
    """Update IBKR_PORT in .env and in the running process environment."""
    _update_env_value("IBKR_PORT", port)


async def _ensure_gateway(notify) -> bool:
    """
    Ensure gateway is up. Starts watchdog if needed and waits up to 2 min.
    notify: async callable matching reply_text / edit_message_text signature.
    Returns True when ready, False on timeout.
    """
    if _gateway_up():
        return True
    # reset=True: a watchdog parked at its failure cap never retries on its own,
    # so a deliberate wake must hand it a fresh counter.
    _start_watchdog(reset=True)
    await notify(msg.WAKING_UP, parse_mode="Markdown")
    for _ in range(24):
        await asyncio.sleep(5)
        if _gateway_up():
            await asyncio.sleep(15)  # wait for IBC paper disclaimer acceptance
            return True
    await notify(msg.WAKE_UP_TIMEOUT, parse_mode="Markdown")
    return False


# ── Login (web-based credential input) ────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>IBKR Bot Login</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:sans-serif;background:#1a1a2e;display:flex;align-items:center;justify-content:center;min-height:100vh}}
    .card{{background:#16213e;border-radius:12px;padding:32px;width:100%;max-width:380px;box-shadow:0 8px 32px rgba(0,0,0,.4)}}
    h2{{color:#e94560;margin-bottom:8px;font-size:1.3rem}}
    p{{color:#a8a8b3;font-size:.85rem;margin-bottom:24px;line-height:1.5}}
    label{{color:#c8c8d4;font-size:.8rem;display:block;margin-bottom:4px}}
    input{{width:100%;padding:10px 12px;background:#0f3460;border:1px solid #2a2a4a;border-radius:6px;color:#fff;font-size:.95rem;margin-bottom:16px;outline:none}}
    input:focus{{border-color:#e94560}}
    button{{width:100%;padding:12px;background:#e94560;color:#fff;border:none;border-radius:6px;font-size:1rem;cursor:pointer;font-weight:600}}
    button:hover{{background:#c73652}}
    .mode{{color:#4cc9f0;font-weight:600;margin-bottom:20px;font-size:.9rem}}
  </style>
</head>
<body>
  <div class="card">
    <h2>🔐 IBKR Bot Login</h2>
    <p>Credentials sent directly to the server — never through Telegram.</p>
    <div class="mode">Mode: {label}</div>
    <form method="POST">
      <input type="hidden" name="token" value="{token}">
      <label>IBKR Username</label>
      <input type="text" name="username" placeholder="Username" required autofocus autocomplete="username">
      <label>IBKR Password</label>
      <input type="password" name="password" placeholder="Password" required autocomplete="current-password">
      <label>Account ID — only if this login has MULTIPLE accounts</label>
      <input type="text" name="account" placeholder="e.g. U1234567 — leave empty otherwise" autocomplete="off">
      <button type="submit">Connect →</button>
    </form>
  </div>
</body>
</html>"""

_SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head><title>Connected</title>
<style>body{{font-family:sans-serif;background:#1a1a2e;color:#4cc9f0;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}}</style>
</head>
<body><div><h2>✅ Credentials received</h2><p style="color:#a8a8b3;margin-top:12px">Return to Telegram — the gateway is starting.</p></div></body>
</html>"""


async def _do_login(ibkr_id: str, password: str, mode: str, account: str,
                    application, chat_id: int) -> None:
    """Runs on PTB's event loop — updates config, restarts gateway, shows result."""
    port  = "4001" if mode == "live" else "4002"
    label = "Live Trading" if mode == "live" else "Paper Trading"

    status = await application.bot.send_message(
        chat_id,
        f"Switching to *{label}*…"
        + (f"\nPinning account *{account}*." if account else ""),
        parse_mode="Markdown",
    )
    # Halt STAYS SET until the new gateway is verified up (fix, 2026-08-07): the
    # switch takes ~2 minutes and a signal arriving mid-switch used to fire into
    # a half-logged-in gateway and die messily (the 08-05 TSLA signal). Halted,
    # it is dropped deliberately instead; a FAILED switch leaves the bot halted.
    _set_trading_halt(True)
    _update_ibc_config(ibkr_id, password, mode)
    _update_watchdog_script(port, mode)
    _update_env_port(port)
    # Pin (or clear) the target sub-account: every order gets stamped with it,
    # every read filters by it. Empty = single-account login, legacy behavior.
    _update_env_value("IBKR_ACCOUNT", account)

    subprocess.run(["tmux", "kill-session", "-t", "gatewaywatchdog"], capture_output=True)
    subprocess.run(["pkill", "-f", "ibgateway"], capture_output=True)
    await asyncio.sleep(3)
    _start_watchdog()
    await status.edit_text(f"Starting *{label}* gateway…", parse_mode="Markdown")

    for _ in range(24):
        await asyncio.sleep(5)
        if _gateway_up():
            await asyncio.sleep(15)
            break

    if not _gateway_up():
        await status.edit_text(
            "*Gateway did not start.*\n\nCheck credentials and try again. "
            "_Trading stays halted until a successful login or `wake up`._",
            parse_mode="Markdown",
        )
        return

    _set_trading_halt(False)   # gateway verified up — NOW the bot may trade
    summary = await ibkr_get_account_summary()
    if summary["success"]:
        await status.edit_text(msg.wake_up_ok(summary), parse_mode="Markdown")
    else:
        # A pinned account that does not exist under this login lands here with
        # the available account list in the error — the loudest possible flag.
        await status.edit_text(
            f"*{label} gateway is up* ⚠️\n\n"
            f"`{summary.get('error', 'Could not fetch account details')}`\n\n"
            f"If the pinned account is wrong, run `login` again with the "
            f"correct Account ID.",
            parse_mode="Markdown",
        )


def _start_login_server(token: str, port: int, mode: str,
                        application, ptb_loop, chat_id: int) -> None:
    """Start a temporary HTTP server that accepts credentials once, then shuts down.

    If a previous login server is still running (e.g. the user tapped the other
    trading-mode button within the 2-min window), it is closed first so the port
    can be rebound — otherwise the second tap would fail with 'address in use'.
    """
    global _active_login_server
    label = "Live Trading" if mode == "live" else "Paper Trading"

    # Tear down any previous login server so we can rebind the port
    if _active_login_server is not None:
        try:
            _active_login_server.server_close()
        except Exception:
            pass
        _active_login_server = None

    class Handler(http.server.BaseHTTPRequestHandler):
        _done = False

        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if params.get("token", [None])[0] != token or Handler._done:
                self.send_response(403); self.end_headers(); return
            page = _LOGIN_HTML.format(token=token, label=label)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())

        def do_POST(self):
            if Handler._done:
                self.send_response(403); self.end_headers(); return
            length   = int(self.headers.get("Content-Length", 0))
            body     = self.rfile.read(length).decode()
            params   = urllib.parse.parse_qs(body)
            ibkr_id  = params.get("username", [""])[0].strip()
            password = params.get("password", [""])[0].strip()
            account  = params.get("account",  [""])[0].strip().upper()
            tkn      = params.get("token",    [""])[0]
            if not ibkr_id or not password or tkn != token:
                self.send_response(400); self.end_headers(); return
            Handler._done = True
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML.encode())
            asyncio.run_coroutine_threadsafe(
                _do_login(ibkr_id, password, mode, account, application, chat_id),
                ptb_loop,
            )

    class _ReuseServer(http.server.HTTPServer):
        allow_reuse_address = True  # rebind immediately even if a socket lingers in TIME_WAIT

    server = _ReuseServer(("0.0.0.0", port), Handler)
    _active_login_server = server

    def _serve():
        global _active_login_server
        server.timeout = 1
        deadline = time.time() + 120
        while not Handler._done and time.time() < deadline:
            try:
                server.handle_request()
            except OSError:
                break  # socket was closed by a newer login starting up
        try:
            server.server_close()
        except Exception:
            pass
        if _active_login_server is server:
            _active_login_server = None

    threading.Thread(target=_serve, daemon=True, name=f"login-{port}").start()


@authorized
async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "*IBKR Login*\n\nSelect trading mode:",
        reply_markup=login_mode_keyboard(),
        parse_mode="Markdown",
    )
    return LOGIN_MODE


@authorized
async def login_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mode  = "live" if query.data == cb.LOGIN_LIVE else "paper"
    label = "Live Trading" if mode == "live" else "Paper Trading"

    token = secrets.token_urlsafe(12)
    port  = int(os.getenv("LOGIN_PORT", "7823"))
    vps_ip = os.getenv("VPS_IP", "127.0.0.1")

    ptb_loop = asyncio.get_event_loop()
    try:
        _start_login_server(token, port, mode, context.application, ptb_loop, query.message.chat_id)
    except Exception as e:
        await query.edit_message_text(
            f"Could not start the login page: `{e}`\n\nTry again in a moment.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await query.edit_message_text(
        f"*{label}* selected.\n\n"
        f"🔐 Open this link in your browser to enter credentials:\n"
        f"`http://{vps_ip}:{port}?token={token}`\n\n"
        f"_Link expires in 2 minutes. Credentials go directly to the server — not through Telegram._",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


@authorized
async def wake_up_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _set_trading_halt(False)  # waking up clears the kill switch set by `sleep`
    if not await _ensure_gateway(update.message.reply_text):
        return

    # Waking up ALWAYS takes market-data priority back (owner, 2026-08-05): the
    # explicit command wins. If a phone/web session grabbed the data share, re-login
    # to seize it — that session loses its data, by design. The BACKGROUND watchdog
    # probe stays gated behind DATA_PRIORITY=true so the bot never kicks the
    # client's live session on its own — only when the user says `wake up`.
    if await asyncio.to_thread(_data_competing):
        await update.message.reply_text(msg.RECLAIMING_DATA, parse_mode="Markdown")
        subprocess.run(["pkill", "-f", "ibgateway"], capture_output=True)
        await asyncio.sleep(5)
        if not await _ensure_gateway(update.message.reply_text):
            return

    # Order-book confirmation rides in the SAME message as the account status:
    # quote one near-the-money option live and show best bid/ask, so the user can
    # SEE the bot owns the live data feed (mid-limit orders will depend on it once
    # the account is live).
    summary = await ibkr_get_account_summary()
    book = await ibkr_orderbook_check()
    head = (msg.wake_up_ok(summary) if summary["success"]
            else "Gateway is up but could not fetch account details.")
    await update.message.reply_text(
        head + "\n\n" + msg.orderbook_line(book) + "\n\n"
        + msg.settings_line(_order_budget(), _fmt_delay(_sweep_delay())),
        parse_mode="Markdown")


@authorized
async def sleep_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Kill switch: set the halt flag FIRST so no in-flight confirmation can slip an
    # order through by restarting the gateway via _ensure_gateway().
    _set_trading_halt(True)
    subprocess.run(["tmux", "kill-session", "-t", "gatewaywatchdog"], capture_output=True)
    subprocess.run(["pkill", "-f", "ibgateway"], capture_output=True)
    await update.message.reply_text(msg.SLEEPING, parse_mode="Markdown")


@authorized
async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Graceful IBKR logout — sends SIGTERM so Gateway closes the session cleanly."""
    subprocess.run(["tmux", "kill-session", "-t", "gatewaywatchdog"], capture_output=True)
    await update.message.reply_text("Logging out from IBKR…")

    # SIGTERM allows Gateway to log out properly before exiting
    subprocess.run(["pkill", "-SIGTERM", "-f", "ibgateway"], capture_output=True)

    # Wait up to 15s for clean shutdown
    for _ in range(5):
        await asyncio.sleep(3)
        r = subprocess.run(["pgrep", "-f", "ibgateway"], capture_output=True)
        if r.returncode != 0:
            break

    # Force kill if still hanging
    subprocess.run(["pkill", "-9", "-f", "ibgateway"], capture_output=True)

    await update.message.reply_text(msg.LOGGED_OUT, parse_mode="Markdown")


# ── Order size ─────────────────────────────────────────────────────────────────

@authorized
async def size_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """`size` — asks for the per-signal dollar budget."""
    await update.message.reply_text(
        msg.size_prompt(_order_budget()),
        parse_mode="Markdown",
    )
    return SIZE_INPUT


@authorized
async def size_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The dollar amount to spend per automated signal."""
    text = update.message.text.strip().lstrip("$").replace(",", "")
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text(msg.SIZE_INVALID, parse_mode="Markdown")
        return SIZE_INPUT

    if amount < 1:
        await update.message.reply_text(msg.SIZE_INVALID, parse_mode="Markdown")
        return SIZE_INPUT

    old = _order_budget()
    # Whole dollars — _order_budget() floats it back out anyway.
    _update_env_value("ORDER_BUDGET_USD", str(int(amount)))
    await update.message.reply_text(
        msg.size_set(old, _order_budget()),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


@authorized
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`status` — instant state card incl. size and sweep delay (owner, 2026-08-16)."""
    await update.message.reply_text(
        msg.status_card({
            "mode": "LIVE" if os.getenv("IBKR_PORT", "4002") == "4001" else "PAPER",
            "halted": _trading_halted(),
            "gateway_up": _gateway_up(),
            "account": (os.getenv("IBKR_ACCOUNT", "").strip().upper()
                        or ibkr_last_account()),
            "budget": _order_budget(),
            "delay": _fmt_delay(_sweep_delay()),
        }),
        parse_mode="Markdown",
    )


# ── Morning-sweep delay ────────────────────────────────────────────────────────

@authorized
async def delay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """`delay` — asks for the after-open sweep delay."""
    await update.message.reply_text(
        msg.delay_prompt(_fmt_delay(_sweep_delay())),
        parse_mode="Markdown",
    )
    return DELAY_INPUT


@authorized
async def delay_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Seconds (or M:SS) after the 09:30 ET open before positions are re-checked."""
    val = _parse_delay(update.message.text)
    if val is None:
        await update.message.reply_text(msg.DELAY_INVALID, parse_mode="Markdown")
        return DELAY_INPUT
    old = _sweep_delay()
    _update_env_value("SWEEP_DELAY_SECONDS", str(val))
    await update.message.reply_text(
        msg.delay_set(_fmt_delay(old), _fmt_delay(_sweep_delay())),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


@authorized
async def details_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_gateway(update.message.reply_text):
        return
    # Same combined view as `wake up`: account status + the live order-book check
    # in one message.
    summary = await ibkr_get_account_summary()
    book = await ibkr_orderbook_check()
    head = (msg.wake_up_ok(summary) if summary["success"]
            else f"Could not fetch account details:\n{summary['error']}")
    await update.message.reply_text(head + "\n\n" + msg.orderbook_line(book),
                                    parse_mode="Markdown")


# ── Fallbacks ──────────────────────────────────────────────────────────────────

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(msg.CANCELLED, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(msg.HELP, parse_mode="Markdown")


# ── Signal confirmation handlers ───────────────────────────────────────────────

_SIG_FIELD_PROMPTS = {
    "ticker":      "Enter *ticker* (e.g. `SPX`, `TSLA`):",
    "option_type": "Enter *option type* — `call` or `put`:",
    "strike":      "Enter *strike price* (e.g. `500`):",
    "expiry":      "Enter *expiry date* (DDMM — e.g. `0506` for May 6):",
}


@authorized
async def sig_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catches text input when user is asked to supply missing signal fields or a price.
    Registered last in group 0 — only runs when no ConversationHandler claimed the update.
    Silently no-ops if there is no pending signal awaiting input.
    """
    sig = context.user_data.get("pending_signal")
    if not sig:
        return

    state = sig.get("state")
    text  = update.message.text.strip()

    # ── Fill in missing critical fields one at a time ──────────────────────────
    if state == "awaiting_fields":
        missing = sig.get("missing_fields", [])
        if not missing:
            sig["state"] = "awaiting_price"
            context.user_data["pending_signal"] = sig
            await update.message.reply_text(msg.signal_missing_price(sig), parse_mode="Markdown")
            return

        field = missing[0]

        if field == "ticker":
            val = text.upper()
            if not val.isalpha() or not (1 <= len(val) <= 6):
                await update.message.reply_text("Enter a valid ticker — letters only, e.g. `SPX`.", parse_mode="Markdown")
                return
            sig["ticker"] = val

        elif field == "option_type":
            lower = text.lower()
            if lower in ("call", "c"):
                sig["option_type"] = "Call"
            elif lower in ("put", "p"):
                sig["option_type"] = "Put"
            else:
                await update.message.reply_text("Enter `call` or `put`.", parse_mode="Markdown")
                return

        elif field == "strike":
            try:
                val = float(text)
                if val <= 0:
                    raise ValueError
                sig["strike"] = val
            except ValueError:
                await update.message.reply_text("Enter a positive number — e.g. `500`.", parse_mode="Markdown")
                return

        elif field == "expiry":
            expiry = _parse_mmdd(text)
            if not expiry:
                await update.message.reply_text("Use DDMM format — e.g. `0506` for May 6.", parse_mode="Markdown")
                return
            sig["expiry"] = expiry

        missing.pop(0)
        sig["missing_fields"] = missing
        context.user_data["pending_signal"] = sig

        if missing:
            await update.message.reply_text(_SIG_FIELD_PROMPTS[missing[0]], parse_mode="Markdown")
        else:
            # All critical fields filled — ask for price+qty and place directly
            sig["state"] = "awaiting_edit"
            context.user_data["pending_signal"] = sig
            await update.message.reply_text(
                "All fields complete. Enter price and quantity:\n"
                "• `3.50 10` — limit at $3.50, qty 10\n"
                "• `mkt 5` — market, qty 5",
                parse_mode="Markdown",
            )
        return

    # ── Edit order (price + qty) → place directly ────────────────────────────
    if state == "awaiting_edit":
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text(
                "Enter price and quantity:\n• `3.50 10` — limit at $3.50, qty 10\n• `mkt 5` — market, qty 5",
                parse_mode="Markdown",
            )
            return
        price_result = _parse_price(parts[0])
        if price_result is None:
            await update.message.reply_text(
                "Invalid price. Use a number (e.g. `3.50`) or `mkt`.",
                parse_mode="Markdown",
            )
            return
        try:
            qty = int(parts[1])
            if qty <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Invalid quantity. Enter a positive integer.", parse_mode="Markdown")
            return

        # Risk guards — this path places immediately with no second confirmation
        if _trading_halted():
            await update.message.reply_text(msg.TRADING_HALTED, parse_mode="Markdown")
            return
        order_type, limit_price = price_result
        sig["order_type"]  = order_type
        sig["limit_price"] = limit_price
        sig["size"]        = qty

        if not await _ensure_gateway(update.message.reply_text):
            return
        await update.message.reply_text("Placing order with IBKR...")

        order_data = dict(sig)
        for k in ("state", "entry_price", "missing_fields"):
            order_data.pop(k, None)

        result = await ibkr_place_order(order_data)
        context.user_data.pop("pending_signal", None)

        if result["success"]:
            await update.message.reply_text(msg.order_placed(order_data, result), parse_mode="Markdown")
        else:
            await update.message.reply_text(msg.order_failed(result["error"]), parse_mode="Markdown")
        return

    # ── Price input ────────────────────────────────────────────────────────────
    if state != "awaiting_price":
        return

    result = _parse_price(text)
    if result is None:
        await update.message.reply_text(
            "Enter a price — e.g. `1.50` — or `mkt` for market order.",
            parse_mode="Markdown",
        )
        return

    order_type, limit_price = result
    sig["order_type"]  = order_type
    sig["limit_price"] = limit_price
    sig["state"]       = "awaiting_confirm"
    context.user_data["pending_signal"] = sig

    mkt = await ibkr_get_market_data(sig) if _gateway_up() else None
    await update.message.reply_text(
        msg.signal_header(sig["action"].upper()) + "\n\n" + msg.order_summary(sig, mkt),
        reply_markup=signal_confirm_change_keyboard(),
        parse_mode="Markdown",
    )


async def _watch_bracket_fill(application, chat_ids, sig: dict, result: dict) -> None:
    """
    Poll a working bracket and report once the buy fills, confirming the take-profit
    went live. Fire-and-forget: a failure here must never affect the order itself.

    chat_ids takes a list so ONE watcher serves everybody. Running one per user meant
    two pollers waking on the same schedule and fighting over the same IBKR clientId.
    """
    if isinstance(chat_ids, int):
        chat_ids = [chat_ids]

    async def tell(text: str, reply_markup=None) -> None:
        for cid in chat_ids:
            try:
                await application.bot.send_message(cid, text, parse_mode="Markdown",
                                                   reply_markup=reply_markup)
            except Exception as e:
                print(f"[automated_bot] fill notify {cid} failed: {e}", flush=True)

    parent_id = result.get("order_id")
    child_id = result.get("exit_id")
    try:
        for i, delay in enumerate((30, 60, 120, 300, 600)):
            await asyncio.sleep(delay)
            st = await ibkr_get_bracket_status(parent_id, child_id)
            if not st.get("success"):
                continue
            if st.get("parent_filled"):
                await tell(msg.bracket_fill_update(sig, st))
                return
            if st.get("parent_gone"):
                # Left the book with no execution — cancelled, rejected or expired.
                # Reporting this as a fill would be the worst possible lie.
                await tell(msg.bracket_gone(sig, parent_id))
                return
            # AUTO-FALLBACK (owner, 2026-08-06): a mid-limit entry still unfilled
            # ~90s in is re-sent at MARKET for the remainder, take-profit
            # re-attached for the full quantity. switch_to_market re-checks the
            # live book, so a fill that lands in the race window wins instead.
            if i == 1 and str(result.get("entry_type", "")).startswith("limit"):
                r = await ibkr_switch_to_market({
                    "kind": "entry", "order_id": parent_id,
                    "ticker": sig.get("ticker"),
                    "option_type": sig.get("option_type"),
                    "strike": sig.get("strike"), "expiry": sig.get("expiry"),
                    "target": result.get("target"),
                })
                if r.get("success"):
                    await tell(msg.auto_m2m("entry", sig, r))
                    return
                # Not switchable -> it filled or left the book in the race window;
                # the next status check reports what actually happened.
        kb = offer_switch_to_market(parent_id, {
            "kind": "entry",
            "ticker": sig.get("ticker"), "option_type": sig.get("option_type"),
            "strike": sig.get("strike"), "expiry": sig.get("expiry"),
            "target": result.get("target"),
        })
        await tell(
            f"*Order `{parent_id}` still unfilled* after ~18 minutes.\n"
            f"Check `pending orders` to amend or cancel — or resend what is left "
            f"as a market order:", reply_markup=kb)
    except Exception as e:
        print(f"[automated_bot] fill watcher error: {e}", flush=True)


# ── Guard: sleep / no-data / gateway-lost notifier ─────────────────────────────
# One episode-based state machine (owner spec, 2026-08-10):
#   sleep        — halted. First alert 1 HOUR into sleep.
#   no_data      — awake, gateway up, but 10197/354: the live data is not ours.
#                  First alert on first detection (probed every ~3 min).
#   gateway_lost — awake but the API port is dead. First alert after a ~3-min
#                  debounce (a wake-up relogin takes ~2 min and must not alarm).
# Once alerting, it fires EVERY MINUTE (client spec, 2026-08-10). A snooze —
# 15 min or 12 h — is a PAUSE: quiet for that long, then the 1-minute alerts
# resume. Alerts stop for good only when the condition clears, which sends one
# all-clear and resets the episode. Weekends (America/New_York) are fully
# silent. State persists on disk so bot restarts (deploys) neither re-fire nor
# forget a snooze.
_GUARD_STATE_FILE = Path(__file__).resolve().parent.parent / ".guard_state.json"
GUARD_CHECK_EVERY = 60          # seconds between checks
GUARD_SLEEP_AFTER = 300         # sleep: first reminder 5 min in (owner, 2026-08-13)
GUARD_DOWN_STREAK = 3           # gateway: consecutive failed checks before alarming
GUARD_DATA_EVERY = 3            # data: probe every Nth cycle (~3 min)


def _guard_load() -> dict:
    try:
        return json.loads(_GUARD_STATE_FILE.read_text())
    except Exception:
        return {}


def _guard_save(state: dict) -> None:
    try:
        _GUARD_STATE_FILE.write_text(json.dumps(state))
    except OSError:
        pass


def _guard_weekend() -> bool:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).weekday() >= 5


async def guard_loop(application, user_ids: list[int]) -> None:
    async def tell(text: str, reply_markup=None) -> None:
        for uid in user_ids:
            try:
                await application.bot.send_message(uid, text, parse_mode="Markdown",
                                                   reply_markup=reply_markup)
            except Exception as e:
                print(f"[guard] notify {uid} failed: {e}", flush=True)

    down_streak = 0
    data_tick = 0

    while True:
        await asyncio.sleep(GUARD_CHECK_EVERY)
        try:
            if _guard_weekend():
                continue
            now = time.time()
            state = _guard_load()
            prev = state.get("cond")

            # ---- detect the current condition ----
            if _trading_halted():
                cond, book = "sleep", None
                down_streak = 0
            elif not _gateway_up():
                down_streak += 1
                # during the debounce keep an existing episode, never start one
                cond = "gateway_lost" if (down_streak >= GUARD_DOWN_STREAK
                                          or prev == "gateway_lost") else prev
                book = None
            else:
                down_streak = 0
                data_tick += 1
                if data_tick >= GUARD_DATA_EVERY or prev == "no_data":
                    data_tick = 0
                    book = await ibkr_orderbook_check()
                    cond = ("no_data" if (book.get("competing") or book.get("no_sub"))
                            else None)
                else:
                    # between probes: keep a running no_data episode, start nothing
                    cond, book = (prev, state.get("book")) if prev == "no_data" \
                        else (None, None)

            # ---- episode transitions ----
            if cond != prev:
                if prev in ("no_data", "gateway_lost") and cond is None:
                    await tell(msg.guard_resolved(prev))
                state = {}
                if cond is not None:
                    if cond == "sleep":
                        try:
                            first_at = _HALT_FILE.stat().st_mtime + GUARD_SLEEP_AFTER
                        except OSError:
                            first_at = now + GUARD_SLEEP_AFTER
                    else:
                        first_at = now          # instant
                    state = {"cond": cond, "since": now, "snooze": None,
                             "next_at": first_at,
                             "book": {k: book.get(k) for k in ("competing", "no_sub")}
                                     if book else None}

            # ---- fire when due ----
            if state.get("cond") and state.get("next_at") is not None \
                    and now >= state["next_at"]:
                c = state["cond"]
                if c == "sleep":
                    try:
                        hours = (now - _HALT_FILE.stat().st_mtime) / 3600
                    except OSError:
                        hours = 1.0
                    text = msg.sleep_reminder(hours)
                elif c == "gateway_lost":
                    text = msg.guard_gateway_down(down_streak * GUARD_CHECK_EVERY // 60)
                else:
                    text = msg.guard_no_data(state.get("book") or {})
                await tell(text, reply_markup=guard_snooze_keyboard())
                # Every minute until resolved; a snooze only pushes next_at out
                # once, after which the 1-minute drumbeat resumes.
                state["next_at"] = now + GUARD_CHECK_EVERY

            _guard_save(state)
        except Exception as e:
            print(f"[guard] check failed: {e}", flush=True)


@authorized
async def guard_snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Snooze button under any guard alert: sets the re-alert cadence."""
    query = update.callback_query
    await query.answer()
    try:
        seconds = int(query.data[len(cb.GUARD_SNOOZE_PREFIX):])
    except (ValueError, TypeError):
        return
    state = _guard_load()
    if state.get("cond"):
        state["snooze"] = seconds
        state["next_at"] = time.time() + seconds
        _guard_save(state)
    try:
        await query.edit_message_reply_markup(None)
    except Exception:
        pass
    await query.message.reply_text(msg.guard_snoozed(seconds), parse_mode="Markdown")


async def morning_tp_loop(application, user_ids: list[int]) -> None:
    """
    Daily at 09:32:10 America/New_York (2min10s after the open), weekdays: the
    take-profit re-arm sweep. TPs are DAY orders (owner, 2026-08-12), so
    positions held overnight wake up unprotected — the sweep re-places
    yesterday's target, or sells at market when the price gapped above it.
    Only BOT-managed positions (TP registry) are touched, never manual ones.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")

    async def tell(text: str, reply_markup=None) -> None:
        for uid in user_ids:
            try:
                await application.bot.send_message(uid, text, parse_mode="Markdown",
                                                   reply_markup=reply_markup)
            except Exception as e:
                print(f"[tp-sweep] notify {uid} failed: {e}", flush=True)

    while True:
        # Wait for the next slot = 09:30 ET open + the ADJUSTABLE delay
        # (`delay` command). Recomputed every minute, so a change applies
        # immediately — even the same morning before the sweep — no restart.
        while True:
            now = datetime.now(NY)
            nxt = (now.replace(hour=9, minute=30, second=0, microsecond=0)
                   + timedelta(seconds=_sweep_delay()))
            if nxt <= now:
                nxt += timedelta(days=1)
            while nxt.weekday() >= 5:             # Sat/Sun -> Monday
                nxt += timedelta(days=1)
            remaining = (nxt - datetime.now(NY)).total_seconds()
            if remaining <= 60:
                await asyncio.sleep(max(0.5, remaining))
                break
            await asyncio.sleep(60)

        try:
            if _trading_halted():
                await tell("*Morning TP sweep skipped* 😴 — the bot is asleep, "
                           "overnight positions have NO take-profit resting. "
                           "Send `wake up` and the sweep will run next open "
                           "(or re-arm by hand today).")
            elif not _gateway_up():
                await tell("*Morning TP sweep FAILED* 🛑 — gateway is down. "
                           "Overnight positions have NO take-profit resting. "
                           "Send `wake up`.")
            else:
                results = await ibkr_morning_tp_sweep()
                await tell(msg.tp_sweep_report(results))
                # Sweep sells with a remainder (ladder ended unfilled): NO
                # automatic market — hand each one its button.
                for r in results:
                    held = r.get("held") or 0
                    filled = float(r.get("filled") or 0)
                    if (r.get("action") == "market_sell" and filled < held
                            and r.get("last_order_id")):
                        kb = offer_switch_to_market(r["last_order_id"], {
                            "kind": "exit", "ticker": r.get("ticker"),
                            "option_type": r.get("option_type"),
                            "strike": r.get("strike"), "expiry": r.get("expiry"),
                        })
                        await tell(
                            f"⚠️ *{r.get('ticker')} {r.get('strike')} "
                            f"{r.get('option_type')}* — "
                            f"{int(held - filled)} contract(s) still unsold "
                            f"after the ladder. Sell the rest at market:",
                            reply_markup=kb)
        except Exception as e:
            print(f"[tp-sweep] failed: {e}", flush=True)
            try:
                await tell(f"*Morning TP sweep crashed* 🛑 — `{e!r}`. "
                           f"Check positions by hand.")
            except Exception:
                pass

        await asyncio.sleep(120)   # step past the slot so it never double-fires


async def premarket_reminder_loop(application, user_ids: list[int]) -> None:
    """
    Daily pre-market check, 09:00 America/New_York (30 minutes before the open),
    weekdays only. Bot up and armed -> account status + live order-book status.
    Asleep or halted -> a reminder to send `wake up`. Runs as a task on PTB's loop;
    a bot restart simply recomputes the next slot.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")

    while True:
        now = datetime.now(NY)
        nxt = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        while nxt.weekday() >= 5:                 # Sat/Sun -> Monday
            nxt += timedelta(days=1)
        await asyncio.sleep(max(1.0, (nxt - datetime.now(NY)).total_seconds()))

        try:
            if _gateway_up() and not _trading_halted():
                summary = await ibkr_get_account_summary()
                book = await ibkr_orderbook_check()
                if summary.get("success"):
                    text = msg.premarket_up(summary, msg.orderbook_line(book))
                else:
                    text = ("*Pre-market check* ⚠️ — the gateway is up but the "
                            "account did not answer. Send `wake up` to re-check.")
            else:
                text = msg.PREMARKET_WAKE
            for uid in user_ids:
                try:
                    await application.bot.send_message(uid, text, parse_mode="Markdown")
                except Exception as e:
                    print(f"[premarket] notify {uid} failed: {e}", flush=True)
        except Exception as e:
            print(f"[premarket] check failed: {e}", flush=True)

        await asyncio.sleep(120)   # step past 09:00 so the same slot never fires twice


@authorized
async def to_market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the Switch-to-MARKET button under an unfilled-order notification."""
    query = update.callback_query
    await query.answer()

    try:
        order_id = int(query.data[len(cb.M2M_PREFIX):])
    except (ValueError, TypeError):
        return

    info = _m2m_store.get(order_id)
    if info is None:
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(msg.M2M_EXPIRED, parse_mode="Markdown")
        return

    # Drop the button everywhere it can be dropped BEFORE acting: two users pressing
    # it (or one pressing twice) must not send two market orders. The re-check inside
    # switch_to_market backstops the race, but not offering it twice is cheaper.
    _m2m_store.pop(order_id, None)
    try:
        await query.edit_message_reply_markup(None)
    except Exception:
        pass    # the other user's copy keeps its button; the store is already empty

    await query.message.reply_text("Switching to market…", parse_mode="Markdown")
    result = await ibkr_switch_to_market(info)
    if not result.get("success"):
        # Not carried out — offering again costs nothing and keeps the user unstuck.
        _m2m_store[order_id] = info
    await query.message.reply_text(msg.m2m_result(result), parse_mode="Markdown")


@authorized
async def manual_retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The user asked to place a missed signal at MARKET (auto-retry failed)."""
    query = update.callback_query
    await query.answer()
    try:
        key = int(query.data[len(cb.RETRY_PREFIX):])
    except (ValueError, TypeError):
        return
    sig = _retry_store.pop(key, None)
    if sig is None:
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        await query.message.reply_text(msg.RETRY_EXPIRED, parse_mode="Markdown")
        return
    try:
        await query.edit_message_reply_markup(None)   # consume: no double-fire
    except Exception:
        pass
    await query.message.reply_text("Placing at MARKET…", parse_mode="Markdown")

    attempt = dict(sig)
    attempt["force_market"] = True
    result = await ibkr_place_bracket_order(attempt)

    uids = sorted(_authorized_ids)
    async def tell(text: str, reply_markup=None) -> None:
        for uid in uids:
            try:
                await context.application.bot.send_message(
                    uid, text, parse_mode="Markdown", reply_markup=reply_markup)
            except Exception as e:
                print(f"[retry] notify {uid} failed: {e}", flush=True)

    if not result.get("success"):
        print(f"[automated_bot] MANUAL RETRY FAILED {sig.get('ticker')}: "
              f"{result.get('error')}", flush=True)
        _retry_store[key] = dict(sig)                 # failed again — re-arm
        await tell(msg.buy_failed(sig, result.get("error", "unknown error")),
                   reply_markup=manual_retry_keyboard(key))
        return
    if result.get("routed") == "buy_more":
        if result.get("acted"):
            await tell(msg.buy_more_done(sig, result))
        else:
            print(f"[automated_bot] manual retry routed skip — "
                  f"{result.get('skip_reason')}", flush=True)
        return
    await tell(msg.bracket_placed(sig, result))
    qty = result.get("qty", 0)
    if not (qty and float(result.get("filled") or 0) >= qty):
        asyncio.create_task(
            _watch_bracket_fill(context.application, uids, dict(sig), result))


@authorized
async def sig_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles Confirm / Cancel on a signal order summary."""
    query = update.callback_query
    await query.answer()

    sig = context.user_data.get("pending_signal")
    if not sig:
        await query.edit_message_text("No pending signal order.")
        return

    if query.data == cb.SIG_CANCEL:
        context.user_data.pop("pending_signal", None)
        await query.edit_message_text("Signal cancelled.")
        return

    # Automated pipeline: one tap places the whole bracket — buy at the ask sized by
    # the dollar budget, with the take-profit attached. Nothing to type.
    if sig.get("pipeline") == "automated" and sig.get("action", "").lower() == "buy":
        if _trading_halted():
            await query.edit_message_text(msg.TRADING_HALTED, parse_mode="Markdown")
            context.user_data.pop("pending_signal", None)
            return
        if not await _ensure_gateway(query.edit_message_text):
            return
        await query.edit_message_text("Placing bracket order with IBKR...")
        result = await ibkr_place_bracket_order(sig)
        context.user_data.pop("pending_signal", None)
        await query.edit_message_text(
            msg.bracket_placed(sig, result) if result["success"]
            else msg.order_failed(result["error"]),
            parse_mode="Markdown",
        )
        # If the buy is still working, keep watching so the user is told when it fills
        # and whether the take-profit actually went live.
        if result.get("success") and float(result.get("filled") or 0) < result.get("qty", 0):
            asyncio.create_task(_watch_bracket_fill(
                context.application, query.message.chat_id, dict(sig), result))
        return

    # Confirm — ask for price+qty, then place directly (no second confirm screen)
    sig["state"] = "awaiting_edit"
    context.user_data["pending_signal"] = sig
    price_hint = f"${sig['limit_price']}" if sig.get("limit_price") else "mkt"
    qty_hint   = sig.get("size", 1)
    await query.edit_message_text(
        f"Enter price and quantity:\n"
        f"• `3.50 10` — limit at $3.50, qty 10\n"
        f"• `mkt 5` — market, qty 5\n\n"
        f"Scraped: `{price_hint} {qty_hint}`",
        parse_mode="Markdown",
    )


# ── Open Positions ─────────────────────────────────────────────────────────────

@authorized
async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Display-only since 2026-08-05 (owner request): no Close buttons — closing
    # goes through the signal flow (emergency exit) or the manual `sell` flow.
    positions = await ibkr_get_open_positions()
    context.user_data["positions"] = positions
    await update.message.reply_text(
        msg.positions_list(positions),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


@authorized
async def pos_close_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CANCEL:
        await query.edit_message_text("Cancelled.", parse_mode="Markdown")
        return ConversationHandler.END

    parts = query.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return POS_CLOSE_INPUT  # ignore stray callback, stay in state
    idx = int(parts[1])
    positions = context.user_data.get("positions", [])
    if idx >= len(positions):
        await query.edit_message_text("Position no longer available.")
        return ConversationHandler.END

    context.user_data["closing_pos"] = positions[idx]
    await query.edit_message_text(
        msg.position_close_prompt(positions[idx]),
        parse_mode="Markdown",
    )
    return POS_CLOSE_CONFIRM


@authorized
async def pos_close_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "0":
        positions = context.user_data.get("positions", [])
        await update.message.reply_text(
            msg.positions_list(positions),
            reply_markup=positions_keyboard(positions) if positions else None,
            parse_mode="Markdown",
        )
        return POS_CLOSE_INPUT

    parts = text.split()
    p = context.user_data.get("closing_pos", {})

    if len(parts) != 2:
        await update.message.reply_text(
            "Enter quantity and price — e.g. `5 mkt` or `10 1.80`",
            parse_mode="Markdown",
        )
        return POS_CLOSE_CONFIRM

    qty_str, price_str = parts
    qty = _parse_qty(qty_str, p.get("qty", 0))
    if not qty:
        await update.message.reply_text("Invalid quantity.", parse_mode="Markdown")
        return POS_CLOSE_CONFIRM

    price_result = _parse_price(price_str)
    if price_result is None:
        await update.message.reply_text("Invalid price — use a number or `mkt`.", parse_mode="Markdown")
        return POS_CLOSE_CONFIRM

    order_type, limit_price = price_result
    context.user_data["close_qty"]        = qty
    context.user_data["close_order_type"] = order_type
    context.user_data["close_limit_price"] = limit_price

    mkt = await ibkr_get_market_data(p) if _gateway_up() else None
    await update.message.reply_text(
        msg.position_close_summary(p, qty, order_type, limit_price, mkt),
        reply_markup=confirm_change_keyboard(),
        parse_mode="Markdown",
    )
    return POS_CLOSE_CONFIRM


@authorized
async def pos_close_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CHANGE_PRICE:
        p = context.user_data["closing_pos"]
        await query.edit_message_text(
            msg.position_close_prompt(p),
            parse_mode="Markdown",
        )
        return POS_CLOSE_CONFIRM

    if query.data == cb.CANCEL:
        await query.edit_message_text(msg.CANCELLED, parse_mode="Markdown")
        return ConversationHandler.END

    p = context.user_data["closing_pos"]
    # Closing a LONG position sells; closing a SHORT position buys back.
    # Hardcoding "Sell" would double a short instead of closing it.
    close_action = "Sell" if p.get("qty", 0) >= 0 else "Buy"
    order_data = {
        "action":      close_action,
        "ticker":      p["ticker"],
        "option_type": p["option_type"],
        "strike":      p["strike"],
        "expiry":      p["expiry"],
        "order_type":  context.user_data["close_order_type"],
        "limit_price": context.user_data["close_limit_price"],
        "size":        context.user_data["close_qty"],
    }

    if _trading_halted():
        await query.edit_message_text(msg.TRADING_HALTED, parse_mode="Markdown")
        return ConversationHandler.END
    if not await _ensure_gateway(query.edit_message_text):
        return ConversationHandler.END

    await query.edit_message_text("Placing close order...")
    result = await ibkr_place_order(order_data)

    if result["success"]:
        await query.edit_message_text(
            msg.order_placed(order_data, result), parse_mode="Markdown"
        )
    else:
        await query.edit_message_text(
            msg.order_failed(result["error"]), parse_mode="Markdown"
        )
    return ConversationHandler.END


# ── Pending Orders ─────────────────────────────────────────────────────────────

@authorized
async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Display-only since 2026-08-15 (owner): fully automated bot — no manual
    # cancel/modify buttons. The bot manages its own orders; recovery actions
    # exist only as the buttons the bot itself offers on its notifications.
    orders = await ibkr_get_pending_orders()
    await update.message.reply_text(
        msg.pending_orders_list(orders),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


@authorized
async def ord_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CANCEL:
        await query.edit_message_text("Done.", parse_mode="Markdown")
        return ConversationHandler.END

    parts = query.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return ORD_ACTION  # ignore stray callback, stay in state
    idx = int(parts[1])
    orders = context.user_data.get("orders", [])
    if idx >= len(orders):
        await query.edit_message_text("Order no longer available.")
        return ConversationHandler.END

    context.user_data["selected_order"] = orders[idx]
    await query.edit_message_text(
        msg.order_detail(orders[idx]),
        reply_markup=order_action_keyboard(),
        parse_mode="Markdown",
    )
    return ORD_ACTION


@authorized
async def ord_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    o = context.user_data.get("selected_order", {})

    if query.data == cb.ORD_BACK:
        orders = context.user_data.get("orders", [])
        await query.edit_message_text(
            msg.pending_orders_list(orders),
            reply_markup=order_list_keyboard(orders),
            parse_mode="Markdown",
        )
        return ORD_ACTION

    if query.data == cb.ORD_CANCEL:
        await query.edit_message_text(f"Cancelling order #{o['order_id']}...")
        result = await ibkr_cancel_order(o["order_id"])
        if result["success"]:
            await query.edit_message_text(
                f"*Order #{o['order_id']} cancelled.*", parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"*Cancel failed:*\n`{result['error']}`", parse_mode="Markdown"
            )
        return ConversationHandler.END

    if query.data == cb.ORD_MODIFY:
        if o.get("order_type") != "limit":
            await query.edit_message_text(
                "Only limit orders can be modified.", parse_mode="Markdown"
            )
            return ConversationHandler.END
        await query.edit_message_text(
            f"Enter new price for order #{o['order_id']}:\n`2.50` for limit  •  `mkt` for market",
            parse_mode="Markdown",
        )
        return ORD_NEW_PRICE

    return ORD_ACTION


@authorized
async def ord_new_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    o = context.user_data["selected_order"]

    if text in ("mkt", "market"):
        context.user_data["new_price"] = None
    else:
        try:
            val = float(text)
            if val <= 0:
                raise ValueError
            context.user_data["new_price"] = val
        except ValueError:
            await update.message.reply_text(
                "Enter a price — e.g. `2.50` — or `mkt` for market order.",
                parse_mode="Markdown",
            )
            return ORD_NEW_PRICE

    await update.message.reply_text(
        msg.order_modify_confirm(o, context.user_data["new_price"]),
        reply_markup=confirm_keyboard(),
        parse_mode="Markdown",
    )
    return ORD_MODIFY_CONFIRM


@authorized
async def ord_modify_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == cb.CANCEL:
        await query.edit_message_text(msg.CANCELLED, parse_mode="Markdown")
        return ConversationHandler.END

    # Modify is cancel+replace — it places a new order, so the halt applies.
    # (Plain cancel is deliberately still allowed: it only reduces exposure.)
    if _trading_halted():
        await query.edit_message_text(msg.TRADING_HALTED, parse_mode="Markdown")
        return ConversationHandler.END

    o = context.user_data["selected_order"]
    new_price = context.user_data["new_price"]
    await query.edit_message_text(f"Modifying order #{o['order_id']}...")
    result = await ibkr_modify_order(o["order_id"], new_price, o)

    if result["success"]:
        price_display = f"${new_price}" if new_price is not None else "market"
        await query.edit_message_text(
            f"*Order #{o['order_id']} replaced — new order #{result['new_order_id']} at {price_display}*",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            f"*Modify failed:*\n`{result['error']}`", parse_mode="Markdown"
        )
    return ConversationHandler.END
