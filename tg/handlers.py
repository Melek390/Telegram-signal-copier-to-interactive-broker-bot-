import functools
import http.server
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
    login_mode_keyboard,
)
from ibkr.client import (
    place_order as ibkr_place_order,
    get_position as ibkr_get_position,
    get_account_summary as ibkr_get_account_summary,
    get_open_positions as ibkr_get_open_positions,
    get_pending_orders as ibkr_get_pending_orders,
    cancel_order as ibkr_cancel_order,
    modify_order as ibkr_modify_order,
    get_market_data as ibkr_get_market_data,
)

# Conversation states
TICKER, OPTION_TYPE, STRIKE, DATE, PRICE, QTY, CONFIRM = range(7)
# Positions states
POS_CLOSE_INPUT, POS_CLOSE_CONFIRM = range(10, 12)
# Orders states
ORD_ACTION, ORD_NEW_PRICE, ORD_MODIFY_CONFIRM = range(20, 23)
# Login states
LOGIN_MODE, LOGIN_ID, LOGIN_PASSWORD = range(30, 33)

_authorized_ids: set[int] = set()
_active_login_server = None  # currently-running login HTTP server (so a new login can replace it)

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


def _max_contracts() -> int:
    """Per-order contract cap. Options are x100, so a stray digit is very expensive."""
    try:
        return max(1, int(os.getenv("MAX_CONTRACTS_PER_ORDER", "50")))
    except ValueError:
        return 50


def _qty_over_cap(qty) -> bool:
    try:
        return int(qty) > _max_contracts()
    except (TypeError, ValueError):
        return False


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

    size = context.user_data.get("size")
    if _qty_over_cap(size):
        await query.edit_message_text(
            msg.qty_over_cap(int(size), _max_contracts()), parse_mode="Markdown"
        )
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

def _start_watchdog():
    if not _watchdog_running():
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", "gatewaywatchdog", "/root/restart_gateway.sh"],
            capture_output=True,
        )


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


def _update_env_port(port: str) -> None:
    """Update IBKR_PORT in .env and in the running process environment."""
    env_path = "/root/bot/.env"
    with open(env_path, "r") as f:
        lines = f.readlines()
    new_lines, found = [], False
    for line in lines:
        if line.startswith("IBKR_PORT="):
            new_lines.append(f"IBKR_PORT={port}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"IBKR_PORT={port}\n")
    with open(env_path, "w") as f:
        f.writelines(new_lines)
    os.environ["IBKR_PORT"] = port


async def _ensure_gateway(notify) -> bool:
    """
    Ensure gateway is up. Starts watchdog if needed and waits up to 2 min.
    notify: async callable matching reply_text / edit_message_text signature.
    Returns True when ready, False on timeout.
    """
    if _gateway_up():
        return True
    _start_watchdog()
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


async def _do_login(ibkr_id: str, password: str, mode: str, application, chat_id: int) -> None:
    """Runs on PTB's event loop — updates config, restarts gateway, shows result."""
    port  = "4001" if mode == "live" else "4002"
    label = "Live Trading" if mode == "live" else "Paper Trading"

    status = await application.bot.send_message(
        chat_id, f"Switching to *{label}*…", parse_mode="Markdown"
    )
    _set_trading_halt(False)  # a deliberate login clears the kill switch
    _update_ibc_config(ibkr_id, password, mode)
    _update_watchdog_script(port, mode)
    _update_env_port(port)

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
            "*Gateway did not start.*\n\nCheck credentials and try again.",
            parse_mode="Markdown",
        )
        return

    summary = await ibkr_get_account_summary()
    if summary["success"]:
        await status.edit_text(msg.wake_up_ok(summary), parse_mode="Markdown")
    else:
        await status.edit_text(
            f"*{label} gateway is up* ✓\n\nCould not fetch account details — try `details`.",
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
            tkn      = params.get("token",    [""])[0]
            if not ibkr_id or not password or tkn != token:
                self.send_response(400); self.end_headers(); return
            Handler._done = True
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML.encode())
            asyncio.run_coroutine_threadsafe(
                _do_login(ibkr_id, password, mode, application, chat_id),
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
    summary = await ibkr_get_account_summary()
    if summary["success"]:
        await update.message.reply_text(msg.wake_up_ok(summary), parse_mode="Markdown")
    else:
        await update.message.reply_text("Gateway is up but could not fetch account details.")


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


@authorized
async def details_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_gateway(update.message.reply_text):
        return
    summary = await ibkr_get_account_summary()
    if summary["success"]:
        await update.message.reply_text(msg.wake_up_ok(summary), parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"Could not fetch account details:\n{summary['error']}"
        )


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
        if _qty_over_cap(qty):
            await update.message.reply_text(
                msg.qty_over_cap(qty, _max_contracts()), parse_mode="Markdown"
            )
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
    positions = await ibkr_get_open_positions()
    context.user_data["positions"] = positions
    await update.message.reply_text(
        msg.positions_list(positions),
        reply_markup=positions_keyboard(positions) if positions else None,
        parse_mode="Markdown",
    )
    return POS_CLOSE_INPUT if positions else ConversationHandler.END


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
    if _qty_over_cap(order_data["size"]):
        await query.edit_message_text(
            msg.qty_over_cap(int(order_data["size"]), _max_contracts()), parse_mode="Markdown"
        )
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
    orders = await ibkr_get_pending_orders()
    context.user_data["orders"] = orders
    await update.message.reply_text(
        msg.pending_orders_list(orders),
        reply_markup=order_list_keyboard(orders) if orders else None,
        parse_mode="Markdown",
    )
    return ORD_ACTION if orders else ConversationHandler.END


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
