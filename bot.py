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
    TICKER, OPTION_TYPE, STRIKE, DATE, PRICE, QTY, CONFIRM,
    handle_entry, ticker_input, option_type_callback, strike_input,
    date_input, price_input, qty_input, confirm_callback,
    cancel_command, help_command, set_authorized_users,
    sleep_command, logout_command, details_command, wake_up_command,
    POS_CLOSE_INPUT, POS_CLOSE_CONFIRM,
    positions_command, pos_close_select, pos_close_input, pos_close_confirm,
    ORD_ACTION, ORD_NEW_PRICE, ORD_MODIFY_CONFIRM,
    orders_command, ord_select, ord_action, ord_new_price, ord_modify_confirm,
    sig_price_input, sig_confirm_callback,
    LOGIN_MODE,
    login_command, login_mode_callback,
)
from tg.signal_listener import start_signal_listener

load_dotenv()

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

    async def post_init(application: Application) -> None:
        if os.getenv("TELEGRAM_API_ID") and os.getenv("API_HASH"):
            start_signal_listener(application, user_ids)  # starts daemon thread, not a task
        else:
            logger.warning("TELEGRAM_API_ID / API_HASH not set — signal listener disabled.")

    app = Application.builder().token(bot_token).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[
            # triggers: "buy", "sell", or a full one-line order starting with buy/sell
            MessageHandler(filters.Regex(r"(?i)^(buy|sell)(\s+.*)?$"), handle_entry),
        ],
        states={
            TICKER:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ticker_input)],
            OPTION_TYPE: [CallbackQueryHandler(option_type_callback)],
            STRIKE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, strike_input)],
            DATE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, date_input)],
            PRICE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, price_input)],
            QTY:         [MessageHandler(filters.TEXT & ~filters.COMMAND, qty_input)],
            CONFIRM:     [CallbackQueryHandler(confirm_callback)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
        per_message=False,
    )

    pos_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"(?i)^open\s+positions?$"), positions_command)],
        states={
            POS_CLOSE_INPUT:   [CallbackQueryHandler(pos_close_select)],
            POS_CLOSE_CONFIRM: [
                CallbackQueryHandler(pos_close_confirm, pattern=f"^({cb.CONFIRM}|{cb.CANCEL}|{cb.CHANGE_PRICE})$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pos_close_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
        per_message=False,
    )

    ord_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"(?i)^pending\s+orders?$"), orders_command)],
        states={
            ORD_ACTION: [CallbackQueryHandler(ord_action, pattern=f"^({cb.ORD_CANCEL}|{cb.ORD_MODIFY}|{cb.ORD_BACK}|{cb.CANCEL})$"),
                         CallbackQueryHandler(ord_select, pattern=r"^osel:")],
            ORD_NEW_PRICE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ord_new_price)],
            ORD_MODIFY_CONFIRM: [CallbackQueryHandler(ord_modify_confirm, pattern=f"^({cb.CONFIRM}|{cb.CANCEL})$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
        per_message=False,
    )

    login_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"(?i)^login$"), login_command)],
        states={
            LOGIN_MODE: [CallbackQueryHandler(login_mode_callback, pattern=f"^({cb.LOGIN_LIVE}|{cb.LOGIN_PAPER})$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(pos_conv)
    app.add_handler(ord_conv)
    app.add_handler(login_conv)
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^wake\s+up$"), wake_up_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^details$"), details_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^sleep$"), sleep_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^logout$"), logout_command))
    # Signal confirmation — uses sig_confirm/sig_cancel (no collision with existing flows)
    app.add_handler(CallbackQueryHandler(sig_confirm_callback, pattern=f"^({cb.SIG_CONFIRM}|{cb.SIG_CANCEL})$"))
    # Signal price input — last in group 0; only runs when user is not in any ConversationHandler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, sig_price_input))
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
