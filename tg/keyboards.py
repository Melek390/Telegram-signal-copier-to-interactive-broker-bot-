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


def signal_confirm_change_keyboard() -> InlineKeyboardMarkup:
    """Confirm / Cancel — used on all signal order summaries."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm", callback_data=cb.SIG_CONFIRM),
        InlineKeyboardButton("Cancel",  callback_data=cb.SIG_CANCEL),
    ]])
