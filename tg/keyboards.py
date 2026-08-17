from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from . import callbacks as cb


def option_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Call", callback_data=cb.CALL),
        InlineKeyboardButton("Put",  callback_data=cb.PUT),
    ]])


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm", callback_data=cb.CONFIRM),
        InlineKeyboardButton("Cancel",  callback_data=cb.CANCEL),
    ]])


def positions_keyboard(positions: list) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(positions):
        label = f"Close {i+1}  —  {p['ticker']} {p['option_type'][0]}{int(p['strike']) if p['strike'] == int(p['strike']) else p['strike']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{cb.POS_CLOSE_PREFIX}{i}")])
    rows.append([InlineKeyboardButton("Done", callback_data=cb.CANCEL)])
    return InlineKeyboardMarkup(rows)


def order_list_keyboard(orders: list) -> InlineKeyboardMarkup:
    rows = []
    for i, o in enumerate(orders):
        if o.get("manual"):
            continue    # TWS/web orders (orderId 0) cannot be cancelled or modified from the API
        strike = int(o['strike']) if o['strike'] == int(o['strike']) else o['strike']
        label = f"Order {i+1}  —  {o['action']} {o['qty']}x {o['ticker']} {o['option_type'][0]}{strike}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{cb.ORD_SELECT_PREFIX}{i}")])
    rows.append([InlineKeyboardButton("Done", callback_data=cb.CANCEL)])
    return InlineKeyboardMarkup(rows)


def order_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Cancel Order", callback_data=cb.ORD_CANCEL),
        InlineKeyboardButton("Modify Price",  callback_data=cb.ORD_MODIFY),
    ], [
        InlineKeyboardButton("Back",          callback_data=cb.ORD_BACK),
    ]])


def confirm_change_keyboard() -> InlineKeyboardMarkup:
    """Confirm / Change Price / Cancel — used on new order summaries."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm",      callback_data=cb.CONFIRM),
        InlineKeyboardButton("Change Price", callback_data=cb.CHANGE_PRICE),
        InlineKeyboardButton("Cancel",       callback_data=cb.CANCEL),
    ]])


def login_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Live Trading",  callback_data=cb.LOGIN_LIVE),
        InlineKeyboardButton("Paper Trading", callback_data=cb.LOGIN_PAPER),
    ]])


def signal_confirm_keyboard() -> InlineKeyboardMarkup:
    """Confirm / Cancel — used when asking user to enter price first."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm", callback_data=cb.SIG_CONFIRM),
        InlineKeyboardButton("Cancel",  callback_data=cb.SIG_CANCEL),
    ]])


def switch_to_market_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """One button under an unfilled-order notification: replace it with a market order."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Switch to MARKET ⚡",
                             callback_data=f"{cb.M2M_PREFIX}{order_id}"),
    ]])


def manual_retry_keyboard(key: int) -> InlineKeyboardMarkup:
    """One button under a failed-buy notification: try again at MARKET."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Place at MARKET ⚡",
                             callback_data=f"{cb.RETRY_PREFIX}{key}"),
    ]])


def guard_snooze_keyboard() -> InlineKeyboardMarkup:
    """Snooze options on the sleep-mode reminder."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Snooze 15 min", callback_data=f"{cb.GUARD_SNOOZE_PREFIX}900"),
        InlineKeyboardButton("Snooze 12 h",   callback_data=f"{cb.GUARD_SNOOZE_PREFIX}43200"),
    ]])


def signal_confirm_change_keyboard() -> InlineKeyboardMarkup:
    """Confirm / Cancel — used on all signal order summaries."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm", callback_data=cb.SIG_CONFIRM),
        InlineKeyboardButton("Cancel",  callback_data=cb.SIG_CANCEL),
    ]])
