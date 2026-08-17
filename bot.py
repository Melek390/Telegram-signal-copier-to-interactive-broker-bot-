import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())  # must run before any ib_insync import on Python 3.12+

import os
import logging

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

from tg import callbacks as cb
from tg.handlers import (
    cancel_command, help_command, set_authorized_users,
    sleep_command, logout_command, details_command, wake_up_command,
    positions_command, orders_command, status_command,
    sig_price_input, sig_confirm_callback, to_market_callback,
    premarket_reminder_loop, guard_loop, guard_snooze_callback,
    morning_tp_loop, manual_retry_callback,
    LOGIN_MODE,
    login_command, login_mode_callback,
    SIZE_INPUT, size_command, size_input,
    DELAY_INPUT, delay_command, delay_input,
    COMMAND_WORDS, leave_prompt,
)
from tg.signal_listener import start_signal_listener
from tg.automated_listener import start_automated_listener

load_dotenv()


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")

    raw_ids = os.getenv("AUTHORIZED_USER_IDS", os.getenv("AUTHORIZED_USER_ID", ""))
    user_ids = [int(uid.strip()) for uid in raw_ids.split(",") if uid.strip().isdigit()]
    set_authorized_users(user_ids)

    # Exactly one signal pipeline may run.
    #   SIGNAL_LISTENER — the original Google Vision OCR + regex path
    #   AUTOMATED_BOT   — the Claude reader path (automated_bot/)
    use_vision    = _flag("SIGNAL_LISTENER")
    use_automated = _flag("AUTOMATED_BOT")

    if use_vision and use_automated:
        raise ValueError(
            "SIGNAL_LISTENER and AUTOMATED_BOT are both enabled in .env. "
            "They would both answer the same channel message — enable exactly one."
        )

    async def post_init(application: Application) -> None:
        # Daily 09:00 ET pre-market status/reminder — independent of the listener,
        # so it starts even when the Telethon credentials are missing.
        asyncio.create_task(premarket_reminder_loop(application, user_ids))
        # Sleep/gateway guard: hourly sleep reminders with snooze, and an alarm
        # when the gateway dies outside sleep mode.
        asyncio.create_task(guard_loop(application, user_ids))
        # 09:32:10 ET daily: re-arm DAY take-profits on overnight bot positions
        # (market-sell the ones that gapped above yesterday's target).
        asyncio.create_task(morning_tp_loop(application, user_ids))
        if not (os.getenv("TELEGRAM_API_ID") and os.getenv("API_HASH")):
            logger.warning("TELEGRAM_API_ID / API_HASH not set — no signal listener.")
            return
        if use_automated:
            logger.info("Signal pipeline: AUTOMATED_BOT (Claude reader)")
            start_automated_listener(application, user_ids)   # daemon thread, not a task
        elif use_vision:
            logger.info("Signal pipeline: SIGNAL_LISTENER (Google Vision OCR)")
            start_signal_listener(application, user_ids)
        else:
            logger.warning("Both SIGNAL_LISTENER and AUTOMATED_BOT are off — "
                           "signals will be ignored. Manual orders still work.")

    app = Application.builder().token(bot_token).post_init(post_init).build()

    # Manual order entry and manual position/order management were REMOVED on
    # 2026-08-15 (owner): the bot is fully automated. `open positions` and
    # `pending orders` are plain display-only commands now; the only manual
    # actions left are the recovery buttons the bot itself offers.

    login_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"(?i)^\s*login\s*$"), login_command)],
        states={
            LOGIN_MODE: [CallbackQueryHandler(login_mode_callback, pattern=f"^({cb.LOGIN_LIVE}|{cb.LOGIN_PAPER})$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
        per_message=False,
    )

    size_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"(?i)^\s*size\s*$"), size_command)],
        states={
            SIZE_INPUT: [MessageHandler(filters.Regex(COMMAND_WORDS), leave_prompt), MessageHandler(filters.TEXT & ~filters.COMMAND, size_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
        per_message=False,
    )

    delay_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"(?i)^\s*delay\s*$"), delay_command)],
        states={
            DELAY_INPUT: [MessageHandler(filters.Regex(COMMAND_WORDS), leave_prompt), MessageHandler(filters.TEXT & ~filters.COMMAND, delay_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*status\s*$"), status_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*open\s+positions?\s*$"), positions_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*pending\s+orders?\s*$"), orders_command))
    app.add_handler(login_conv)
    app.add_handler(size_conv)
    app.add_handler(delay_conv)
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*wake\s+up\s*$"), wake_up_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*details\s*$"), details_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*sleep\s*$"), sleep_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*logout\s*$"), logout_command))
    # Signal confirmation — uses sig_confirm/sig_cancel (no collision with existing flows)
    app.add_handler(CallbackQueryHandler(sig_confirm_callback, pattern=f"^({cb.SIG_CONFIRM}|{cb.SIG_CANCEL})$"))
    # Switch-to-MARKET button on unfilled automated orders
    app.add_handler(CallbackQueryHandler(to_market_callback, pattern=f"^{cb.M2M_PREFIX}"))
    # Snooze buttons on the sleep-guard reminder
    app.add_handler(CallbackQueryHandler(guard_snooze_callback, pattern=f"^{cb.GUARD_SNOOZE_PREFIX}"))
    # "Place at MARKET" button on a missed signal whose auto-retry also failed
    app.add_handler(CallbackQueryHandler(manual_retry_callback, pattern=f"^{cb.RETRY_PREFIX}"))
    # Signal price input — last in group 0; only runs when user is not in any ConversationHandler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, sig_price_input))
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
