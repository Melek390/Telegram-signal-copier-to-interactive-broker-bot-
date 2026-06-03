"""
tg/signal_listener.py — Telethon background task that watches the signal channel.

Runs in a SEPARATE DAEMON THREAD with its own event loop, isolated from PTB's loop.
This prevents PTB's CancelledError from killing the listener on bot restarts/cleanup.

On each new channel message:
  1. Classify BUY/SELL from Arabic keywords (كول/بوت/خفف)
  2. Require an attached image — messages without images are skipped
  3. OCR the image (Google Vision / pytesseract) to extract order fields
  4. Store pending_signal in application.user_data[user_id]
  5. Send user a confirmation message; if fields are missing, ask one by one
"""

import asyncio
import os
import tempfile
import threading
from pathlib import Path

from telethon import TelegramClient, events

from signal_parser import classify_text, ocr_image, parse_order
from tg import messages as msg
from tg import keyboards as kb
from ibkr.client import get_market_data as ibkr_get_market_data
from tg.handlers import _gateway_up

API_ID         = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH       = os.getenv("API_HASH", "")
SIGNAL_CHANNEL = int(os.getenv("SIGNAL_CHANNEL", "0"))

SESSION_FILE = str(Path(__file__).parent.parent / "listener")

_OPT_FULL = {"C": "Call", "P": "Put"}


async def _send_signal(application, user_id, direction, order_raw, missing_critical):
    """Runs on PTB's event loop — safe to access user_data and send messages."""
    ud = application.user_data[user_id]

    if missing_critical:
        sig = {
            **order_raw,
            "order_type":    None,
            "limit_price":   None,
            "state":         "awaiting_fields",
            "missing_fields": list(missing_critical),
        }
        ud["pending_signal"] = sig
        await application.bot.send_message(
            chat_id=user_id,
            text=msg.signal_partial(direction, order_raw, missing_critical),
            parse_mode="Markdown",
        )
        return

    header = msg.signal_header(direction)

    # Always show summary with Confirm | Cancel — Confirm will ask for price+qty then place directly
    sig = {
        **order_raw,
        "order_type":  "limit" if order_raw.get("entry_price") else None,
        "limit_price": order_raw.get("entry_price"),
        "state":       "awaiting_confirm",
    }
    ud["pending_signal"] = sig
    mkt = await ibkr_get_market_data(sig) if _gateway_up() else None
    await application.bot.send_message(
        chat_id=user_id,
        text=header + "\n\n" + msg.order_summary(sig, mkt),
        reply_markup=kb.signal_confirm_change_keyboard(),
        parse_mode="Markdown",
    )


async def _handle_message(client, message, application, user_ids: list[int], ptb_loop) -> None:
    text      = message.text or ""
    direction = classify_text(text)

    if not direction or not message.photo:
        return

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp = Path(f.name)
    try:
        await client.download_media(message.photo, file=str(tmp))
        ocr_text  = await asyncio.to_thread(ocr_image, tmp)
        order_raw = parse_order(ocr_text)
    finally:
        tmp.unlink(missing_ok=True)

    if order_raw.get("option_type"):
        order_raw["option_type"] = _OPT_FULL.get(order_raw["option_type"], order_raw["option_type"])

    order_raw["action"] = "Buy" if direction == "BUY" else "Sell"
    order_raw["size"]   = 1

    missing_critical = [
        f for f in ("ticker", "option_type", "strike", "expiry")
        if not order_raw.get(f)
    ]

    for user_id in user_ids:
        # Schedule PTB-side work on PTB's event loop (fire and forget)
        asyncio.run_coroutine_threadsafe(
            _send_signal(application, user_id, direction, order_raw, missing_critical),
            ptb_loop,
        )


async def _listener_loop(application, user_ids: list[int], ptb_loop) -> None:
    """Telethon reconnect loop — runs inside the daemon thread's own event loop."""
    backoff = 30
    while True:
        try:
            client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
            await client.start()
            print(f"[signal_listener] Listening on channel {SIGNAL_CHANNEL}", flush=True)

            @client.on(events.NewMessage(chats=SIGNAL_CHANNEL))
            async def on_new_message(event):
                try:
                    await _handle_message(client, event.message, application, user_ids, ptb_loop)
                except Exception as e:
                    print(f"[signal_listener] Error handling message: {e}", flush=True)

            await client.run_until_disconnected()

        except Exception as e:
            print(f"[signal_listener] Disconnected ({e}), retrying in {backoff}s...", flush=True)

        await asyncio.sleep(backoff)


def start_signal_listener(application, user_ids: list[int]) -> None:
    """
    Starts Telethon in a daemon thread with its own event loop.
    Isolated from PTB's event loop — PTB cancellations cannot kill it.
    Called directly from post_init (not as asyncio.create_task).
    """
    ptb_loop = asyncio.get_event_loop()

    def _thread_main():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_listener_loop(application, user_ids, ptb_loop))
        except Exception as e:
            print(f"[signal_listener] Thread crashed: {e}", flush=True)
        finally:
            loop.close()

    thread = threading.Thread(target=_thread_main, daemon=True, name="signal-listener")
    thread.start()
    print(f"[signal_listener] Daemon thread started", flush=True)
