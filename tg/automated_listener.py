"""
tg/automated_listener.py — Telethon listener backed by the Claude reader.

Same daemon-thread design as tg/signal_listener.py (isolated event loop, immune to
PTB cancellation), but the perception layer is different:

    old (signal_listener.py) : Google Vision OCR + regex  ->  parse_order()
    new (this file)          : hardcoded prefilter        ->  Claude reads the card

Orders are placed automatically — there is no Confirm step. The bot only speaks up
for outcomes worth interrupting someone for: a buy that filled, a position we exited
on an emergency exit, and anything that failed.

Selected with AUTOMATED_BOT=true in .env. Exactly one listener runs — see bot.py.
"""

import asyncio
import os
import re
import tempfile
import threading
import time
import traceback
from datetime import date
from pathlib import Path

from telethon import TelegramClient, events

from automated_bot import prefilter, read_signal
from tg import messages as msg
from ibkr.client import (
    place_bracket_order as ibkr_place_bracket_order,
    emergency_exit as ibkr_emergency_exit,
    buy_more as ibkr_buy_more,
    place_take_profit as ibkr_place_take_profit,
    switch_to_market as ibkr_switch_to_market,
)
from tg.handlers import (
    _gateway_up, _ensure_gateway, _trading_halted, _watch_bracket_fill,
    offer_switch_to_market, offer_manual_retry,
)

API_ID         = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH       = os.getenv("API_HASH", "")
SIGNAL_CHANNEL = int(os.getenv("SIGNAL_CHANNEL", "0"))

SESSION_FILE = str(Path(__file__).parent.parent / "listener")

# Buys that filled WITHOUT a target, keyed by channel message id. The author's
# habit (seen live 2026-08-05: AMD entry filled 21s before the الاهداف line
# existed) is to post the card and edit the targets in seconds later — when the
# edit lands, we pull the target from the new text and place the missing TP.
_AWAITING_TP: dict[int, tuple[float, dict]] = {}
_AWAITING_TP_TTL = 6 * 3600


def _target_from_text(text: str) -> float | None:
    """First number on the targets line — same rule the reader prompt states."""
    for line in (text or "").split("\n"):
        # BOTH forms, checked separately: the plural اهداف has an alef between the
        # dal and fa (ه-د-ا-ف), so "هدف" is NOT a substring of it.
        if "هدف" in line or "اهداف" in line:
            m = re.search(r"(\d+(?:[.,]\d+)?)", line)
            if m:
                return float(m.group(1).replace(",", "."))
    return None


async def _quiet(*_args, **_kwargs) -> None:
    """A notify() that says nothing — so waking the gateway stays silent."""
    return None


# Connection-class failures worth one retry. Deliberate refusals (contract not
# found, target below entry, over budget, no price) must NEVER match — retrying
# a refusal would just refuse again, and retrying a rejected judgement call is
# how double orders happen.
_TRANSIENT_MARKS = (
    "timeouterror", "timed out", "connectionrefused", "connection refused",
    "connectionreset", "connection reset", "connect call failed",
    "socket disconnect", "peer closed", "not connected", "api connection failed",
    "connectionerror",
)


def _transient(error: str) -> bool:
    e = (error or "").lower()
    return any(m in e for m in _TRANSIENT_MARKS)


async def _place_with_retry(place, sig: dict, kind: str) -> dict:
    """
    One automatic retry, 30s later, for transient failures only (born from the
    SKHY 2026-08-11 incident: a seconds-long gateway stall ate a valid signal).

    Small known race, accepted: if the first attempt's error fired AFTER the
    order actually reached IBKR, a retry could double up — but connection-class
    errors happen while talking to the gateway, before placement, and the
    bracket path re-checks resting orders on every attempt, which routes a
    repeat into the position-aware buy_more flow instead of a second bracket.
    """
    result = await place(sig)
    if not result.get("success") and _transient(result.get("error", "")):
        print(f"[automated_bot] {kind} transient failure — retrying in 30s: "
              f"{result.get('error')}", flush=True)
        await asyncio.sleep(30)
        result = await place(sig)
        result["retried"] = True
        print(f"[automated_bot] {kind} retry result: "
              f"success={result.get('success')} {result.get('error') or ''}",
              flush=True)
    return result


async def _execute_signal(application, user_ids: list[int], sig: dict) -> None:
    """
    Runs ONCE on PTB's event loop and places the order itself.

    Executing once and then notifying everyone is deliberate: fanning the execution
    out per user would place one order per person for a single signal.
    """
    async def tell(text: str, reply_markup=None) -> None:
        for uid in user_ids:
            try:
                await application.bot.send_message(uid, text, parse_mode="Markdown",
                                                   reply_markup=reply_markup)
            except Exception as e:
                print(f"[automated_bot] notify {uid} failed: {e}", flush=True)

    action = sig.get("signal_action")
    is_buy = action == "buy"
    is_buy_more = action == "buy_more"

    # The kill switch stops new entries only — and buy_more IS a new entry, it
    # increases exposure. Refusing an exit while halted would strand a position we
    # are already holding — the moment you most need out.
    if (is_buy or is_buy_more) and _trading_halted():
        print(f"[automated_bot] trading halted — {action} dropped", flush=True)
        return

    try:
        if not _gateway_up() and not await _ensure_gateway(_quiet):
            await tell(f"*Signal arrived but the gateway is down* 🛑\n\n"
                       f"{msg._contract_line(sig)}\n\nNothing was placed.")
            return

        if is_buy:
            result = await _place_with_retry(ibkr_place_bracket_order, sig, "BUY")
            if not result.get("success"):
                # The error string used to exist ONLY in the Telegram message —
                # untraceable server-side (learned the hard way, SKHY 2026-08-11).
                print(f"[automated_bot] BUY FAILED {sig.get('ticker')}: "
                      f"{result.get('error')}", flush=True)
                if result.get("no_data"):
                    # Parsed fine, nothing placed (owner, 2026-08-14): the user
                    # decides whether to buy at market without a price.
                    await tell(msg.buy_no_data(sig),
                               reply_markup=offer_manual_retry(sig))
                    return
                # Auto-retry already ran and lost — hand the user the button to
                # fire one more attempt, forced to MARKET.
                await tell(msg.buy_failed(sig, result.get("error", "unknown error")),
                           reply_markup=offer_manual_retry(sig))
                return
            # No target in the arrival text -> remember this buy; the author
            # usually edits the الاهداف line in within a minute and the edit
            # handler will place the missing take-profit then.
            if not result.get("target") and sig.get("message_id"):
                _AWAITING_TP[sig["message_id"]] = (time.time(), dict(sig))
            if result.get("routed") == "buy_more":
                # Second round on a contract we hold — the bracket path delegated
                # to buy_more, so report it as an average-in, not a fresh entry.
                if result.get("acted"):
                    await tell(msg.buy_more_done(sig, result))
                else:
                    print(f"[automated_bot] routed buy skipped — "
                          f"{result.get('skip_reason')}", flush=True)
                return
            qty = result.get("qty", 0)
            filled = float(result.get("filled") or 0)
            if qty and filled >= qty:
                await tell(msg.bracket_placed(sig, result))   # fully filled
            elif str(result.get("entry_type", "")).startswith("limit (ladder"):
                # Ladder ended with a remainder (owner spec 2026-08-14: NO
                # automatic market). The button buys exactly what is left.
                rest = int(qty - filled)
                kb = (offer_manual_retry({**sig, "force_qty": rest})
                      if rest > 0 else None)
                await tell(msg.bracket_placed(sig, result), reply_markup=kb)
            else:
                # Atomic no-data path: a resting parent order — the fill
                # watcher owns it. ONE watcher for everybody.
                asyncio.create_task(
                    _watch_bracket_fill(application, list(user_ids), dict(sig), result))
            return

        if is_buy_more:
            # المتوسط — average into a contract we hold: cancel the take-profit,
            # buy more at market, re-place the take-profit for the whole position.
            result = await _place_with_retry(ibkr_buy_more, sig, "BUY_MORE")
            if not result.get("success"):
                print(f"[automated_bot] BUY_MORE FAILED {sig.get('ticker')}: "
                      f"{result.get('error')}", flush=True)
                await tell(msg.buy_more_failed(sig, result.get("error", "unknown error")))
                return
            if result.get("acted"):
                await tell(msg.buy_more_done(sig, result))
            else:
                print(f"[automated_bot] buy_more skipped — {result.get('skip_reason')}",
                      flush=True)
            return

        # emergency exit — خفف with no الهدف. The reader already applied that rule,
        # so everything reaching here is an exit; all that is left is "do we hold it".
        result = await _place_with_retry(ibkr_emergency_exit, sig, "EXIT")
        if result.get("no_data"):
            # Parsed fine, position and take-profit left untouched (owner,
            # 2026-08-14). The button sells what is held at market on press —
            # keyed on the resting TP so the press cancels it first.
            key = result.get("tp_order_id") or sig.get("message_id") or 0
            kb = offer_switch_to_market(key, {
                "kind": "exit", "ticker": sig.get("ticker"),
                "option_type": sig.get("option_type"),
                "strike": sig.get("strike"), "expiry": sig.get("expiry"),
            }) if key else None
            await tell(msg.exit_no_data(sig, result), reply_markup=kb)
            return
        if not result.get("success"):
            print(f"[automated_bot] EXIT FAILED {sig.get('ticker')}: "
                  f"{result.get('error')}", flush=True)
            await tell(msg.exit_failed(sig, result.get("error", "unknown error")))
            return
        if result.get("acted"):
            kb = None
            # The mid-price sell is still resting — offer the escape hatch: one tap
            # replaces it with a market sell of whatever is left.
            if (result.get("order_id")
                    and float(result.get("filled") or 0) < result.get("held", 0)):
                kb = offer_switch_to_market(result["order_id"], {
                    "kind": "exit",
                    "ticker": sig.get("ticker"),
                    "option_type": sig.get("option_type"),
                    "strike": sig.get("strike"), "expiry": sig.get("expiry"),
                })
            await tell(msg.exit_done(sig, result), reply_markup=kb)
            # No auto-switch (owner spec 2026-08-14): market only ever happens
            # from the button. The ladder already ran its full course.
        else:
            print(f"[automated_bot] exit skipped — {result.get('skip_reason')}",
                  flush=True)

    except Exception as e:
        # This coroutine is dispatched with run_coroutine_threadsafe and nobody ever
        # reads the returned Future, so without this the signal would vanish in total
        # silence — no log line, no message, no order.
        print(f"[automated_bot] EXECUTE FAILED {sig.get('ticker')}: {e!r}", flush=True)
        traceback.print_exc()
        try:
            await tell(f"*Signal handling crashed* 🛑\n\n"
                       f"{msg._contract_line(sig)}\n\n`{e}`\n\n"
                       f"Check `open positions` and `pending orders`.")
        except Exception:
            pass    # if Telegram is what broke, keep the traceback above


async def _auto_exit_to_market(application, user_ids: list[int],
                               sig: dict, result: dict) -> None:
    """
    AUTO-FALLBACK (owner, 2026-08-06): a mid-limit exit still unfilled ~45s after
    placement is re-sent at MARKET for the remainder. Consuming the button offer
    first makes auto and manual mutually exclusive — whoever moves first wins.
    """
    from tg.handlers import _m2m_store

    async def tell(text: str) -> None:
        for uid in user_ids:
            try:
                await application.bot.send_message(uid, text, parse_mode="Markdown")
            except Exception as e:
                print(f"[automated_bot] notify {uid} failed: {e}", flush=True)

    await asyncio.sleep(45)
    info = _m2m_store.pop(result["order_id"], None)
    if info is None:
        return          # the user already pressed the button (or the bot restarted)
    try:
        r = await ibkr_switch_to_market(info)
        if r.get("success"):
            await tell(msg.auto_m2m("exit", sig, r))
        else:
            # Nothing to act on (or it failed) — hand the ticket BACK so the
            # button still answers if the user disagrees with our reading. The
            # MU incident taught us: a consumed ticket + a no-op = a dead end.
            _m2m_store[result["order_id"]] = info
            print(f"[automated_bot] exit auto-fallback skipped — {r.get('error')} "
                  f"(button re-armed)", flush=True)
    except Exception as e:
        print(f"[automated_bot] EXIT AUTO-FALLBACK FAILED: {e!r}", flush=True)
        traceback.print_exc()


async def _place_tp_from_edit(application, user_ids: list[int], sig: dict) -> None:
    """Runs on PTB's loop: add the take-profit the arrival text was missing."""
    async def tell(text: str) -> None:
        for uid in user_ids:
            try:
                await application.bot.send_message(uid, text, parse_mode="Markdown")
            except Exception as e:
                print(f"[automated_bot] notify {uid} failed: {e}", flush=True)

    try:
        result = await ibkr_place_take_profit(sig)
        if not result.get("success"):
            await tell(msg.tp_add_failed(sig, result.get("error", "unknown error")))
        elif result.get("acted"):
            await tell(msg.tp_added(sig, result))
        else:
            print(f"[automated_bot] edit-TP skipped — {result.get('skip_reason')}",
                  flush=True)
    except Exception as e:
        print(f"[automated_bot] EDIT-TP FAILED {sig.get('ticker')}: {e!r}", flush=True)
        traceback.print_exc()


async def _handle_edit(message, application, user_ids, ptb_loop) -> None:
    """
    A channel message was edited. Only interesting when it is a buy we executed
    whose arrival text had no target — pull the first target from the edited
    text and place the missing take-profit. Everything else is ignored.
    """
    entry = _AWAITING_TP.get(message.id)
    if entry is None:
        return
    ts, sig = entry
    if time.time() - ts > _AWAITING_TP_TTL:
        _AWAITING_TP.pop(message.id, None)
        return
    target = _target_from_text(message.text or "")
    if target is None:
        return                    # edited, but still no targets line — keep waiting
    _AWAITING_TP.pop(message.id, None)
    sig = dict(sig)
    sig["first_target"] = target
    print(f"[automated_bot] msg {message.id} edited -> target {target}, "
          f"placing missing TP", flush=True)
    asyncio.run_coroutine_threadsafe(
        _place_tp_from_edit(application, user_ids, sig), ptb_loop)


async def _handle_message(client, message, application, user_ids, ptb_loop) -> None:
    # Stage 1 — hardcoded gate. Text-only messages never reach Claude.
    if not prefilter.allows(message.photo):
        return

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp = Path(f.name)
    try:
        await client.download_media(message.photo, file=str(tmp))
        # Stage 2 — Claude. Blocking HTTP, so keep it off this thread's event loop.
        result = await asyncio.to_thread(
            read_signal, tmp, message.text or "", date.today().isoformat()
        )
    finally:
        tmp.unlink(missing_ok=True)

    if result.action == "ignore":
        print(f"[automated_bot] msg {message.id} ignored ({result.reason})", flush=True)
        return

    entry = result.price_ask or result.price_last or result.price_bid
    sig = {
        "message_id":   message.id,
        "action":       "Buy" if result.action in ("buy", "buy_more") else "Sell",
        # The word Claude actually returned — "buy" or "exit". "action" above
        # collapses to Buy/Sell because IBKR only understands those; this keeps the
        # real intent alive so an exit is never mistaken for a fresh short.
        "signal_action": result.action,
        "ticker":       result.ticker,
        "option_type":  result.right,
        "strike":       result.strike,
        "expiry":       result.expiry,
        "size":         1,
        "order_type":   "limit" if entry else None,
        "limit_price":  entry,
        "pipeline":     "automated",
        "first_target": result.first_target,
    }
    print(f"[automated_bot] msg {message.id} -> {result.action} "
          f"{result.ticker} {result.strike} {result.right} {result.expiry}", flush=True)

    # Once, not per user — this places a real order.
    asyncio.run_coroutine_threadsafe(
        _execute_signal(application, user_ids, sig), ptb_loop
    )


async def _listener_loop(application, user_ids, ptb_loop) -> None:
    if not SIGNAL_CHANNEL or not API_ID or not API_HASH:
        print("[automated_bot] SIGNAL_CHANNEL / TELEGRAM_API_ID / API_HASH missing — "
              "listener disabled.", flush=True)
        return

    backoff = 30
    while True:
        client = None
        try:
            client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
            await client.start()
            print(f"[automated_bot] Listening on channel {SIGNAL_CHANNEL} "
                  f"(Claude reader)", flush=True)

            @client.on(events.NewMessage(chats=SIGNAL_CHANNEL))
            async def on_new_message(event):
                try:
                    await _handle_message(client, event.message, application,
                                          user_ids, ptb_loop)
                except Exception as e:
                    print(f"[automated_bot] Error handling message: {e}", flush=True)

            @client.on(events.MessageEdited(chats=SIGNAL_CHANNEL))
            async def on_message_edited(event):
                try:
                    await _handle_edit(event.message, application, user_ids, ptb_loop)
                except Exception as e:
                    print(f"[automated_bot] Error handling edit: {e}", flush=True)

            await client.run_until_disconnected()

        except Exception as e:
            print(f"[automated_bot] Disconnected ({e}), retrying in {backoff}s...",
                  flush=True)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        await asyncio.sleep(backoff)


def start_automated_listener(application, user_ids: list[int]) -> None:
    """Start Telethon in a daemon thread with its own event loop (see session 9)."""
    ptb_loop = asyncio.get_event_loop()

    def _thread_main():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_listener_loop(application, user_ids, ptb_loop))
        except Exception as e:
            print(f"[automated_bot] Thread crashed: {e}", flush=True)
        finally:
            loop.close()

    threading.Thread(target=_thread_main, daemon=True,
                     name="automated-listener").start()
    print("[automated_bot] Daemon thread started", flush=True)
