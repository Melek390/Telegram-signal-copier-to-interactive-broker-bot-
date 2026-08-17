import os
import json
import math
import asyncio
import functools
import threading
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

from ib_insync import IB, Option, MarketOrder, LimitOrder

_RIGHT = {"CALL": "C", "PUT": "P"}

# IBKR allows ONE connection per clientId. Every function that owns orders has to
# connect as the same id — cancel and modify only work from the client that placed
# the order, and reqAllOpenOrders() would adopt other clients' orders and cancel them
# on disconnect. So they cannot be given separate ids; they have to take turns.
#
# Nothing enforced this until 2026-08-03. The Confirm step used to serialise orders
# by hand; once signals started executing unattended, a fill-watcher poll could
# collide with an emergency exit and the exit would fail with the position still open.
#
# threading.RLock, not asyncio.Lock: these run in asyncio.to_thread workers and the
# Telethon listener has its own event loop, so the guard must be loop-independent.
# Re-entrant because _place_bracket_sync delegates to _buy_more_sync (both
# serialized) when a buy signal lands on a contract whose take-profit is resting.
_IB_LOCK = threading.RLock()


def _serialized(fn):
    """Serialise a clientId-owning IBKR session against all the others."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _IB_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _host() -> str:
    return os.getenv("IBKR_HOST", "127.0.0.1")

def _port() -> int:
    return int(os.getenv("IBKR_PORT", "4002"))

def _cid() -> int:
    return int(os.getenv("IBKR_CLIENT_ID", "1"))

def _account() -> str:
    """
    The pinned sub-account (IBKR_ACCOUNT, set from the login page). Empty means
    single-account login — legacy behavior, IBKR routes to the session default.
    A login with MULTIPLE sub-accounts (e.g. shares + options) MUST pin one:
    without it, orders go to whichever account IBKR considers the default.
    """
    return os.getenv("IBKR_ACCOUNT", "").strip().upper()


def _same_account(order) -> bool:
    """Is this (possibly manual) order in our pinned account? No pin = yes."""
    a = _account()
    return not a or (getattr(order, "account", "") or a) == a


class _AccountIB(IB):
    """
    IB with the pinned account stamped on EVERY outgoing order — one chokepoint
    instead of ~20 construction sites, and future code inherits it for free.
    """
    def placeOrder(self, contract, order):
        a = _account()
        if a and not getattr(order, "account", ""):
            order.account = a
        return super().placeOrder(contract, order)


# ── Take-profit registry ───────────────────────────────────────────────────────
# TPs are DAY orders (owner, 2026-08-12): they die at the close, and the morning
# sweep re-arms them 2min10s after the open. The registry records every TP the
# BOT places — contract + target — so the sweep knows which positions are
# bot-managed (manual positions are never touched) and what yesterday's target
# was. Keyed by "TICKER|EXPIRY|STRIKE|RIGHT".
_TP_REGISTRY = Path(__file__).resolve().parent.parent / ".tp_registry.json"


def _tp_key(d: dict) -> str:
    return (f"{d['ticker']}|{d['expiry']}|{float(d['strike'])}|"
            f"{d['option_type'].capitalize()}")


def _tp_registry_all() -> dict:
    try:
        return json.loads(_TP_REGISTRY.read_text())
    except Exception:
        return {}


def _tp_registry_save(reg: dict) -> None:
    try:
        _TP_REGISTRY.write_text(json.dumps(reg, indent=1))
    except OSError:
        pass


def _tp_registry_set(d: dict, target: float, qty: int) -> None:
    reg = _tp_registry_all()
    reg[_tp_key(d)] = {"ticker": d["ticker"], "expiry": d["expiry"],
                       "strike": float(d["strike"]),
                       "option_type": d["option_type"].capitalize(),
                       "target": float(target), "qty": int(qty)}
    _tp_registry_save(reg)


def _tp_registry_remove(d: dict) -> None:
    reg = _tp_registry_all()
    if reg.pop(_tp_key(d), None) is not None:
        _tp_registry_save(reg)


def _min_tick(ib: IB, contract) -> float:
    """
    Contract's minimum price variation. IBKR rejects any limit price that is not a
    multiple of this (Error 110) — e.g. SPX trades in 0.05/0.10, not 0.01.
    Falls back to 0.01 when details are unavailable.
    """
    try:
        details = ib.reqContractDetails(contract)
        if details:
            tick = float(getattr(details[0], "minTick", 0) or 0)
            if tick > 0:
                return tick
    except Exception:
        pass
    return 0.01


def _round_to_tick(price: float, tick: float) -> float:
    """Snap a price to the nearest valid tick multiple."""
    if not tick or tick <= 0:
        return round(price, 2)
    steps = round(price / tick)
    # 6dp guard against binary-float artifacts (e.g. 3*0.05 -> 0.15000000000000002)
    return round(round(steps * tick, 6), 6)


def _resolve_bid_mid(ib: IB, contract, price_type: str, tick: float | None = None) -> float:
    """Fetch live bid/mid price for a qualified contract, snapped to a valid tick."""
    ticker = ib.reqMktData(contract, "", snapshot=True)
    ib.sleep(5)
    bid = ticker.bid
    ask = ticker.ask

    if math.isnan(bid) or bid <= 0 or math.isnan(ask) or ask <= 0:
        raise ValueError("no_market_data")

    raw = bid if price_type == "bid" else (bid + ask) / 2
    if tick is None:
        tick = _min_tick(ib, contract)
    return _round_to_tick(raw, tick)


@_serialized
def _place_order_sync(d: dict) -> dict:
    """
    Blocking IBKR call. Runs in its own thread + event loop so it never
    blocks the Telegram bot's async event loop.
    """
    # There is no upper bound on size — the per-order contract cap was removed on
    # 2026-08-03 at the owner's request. Quantity is whatever is asked for.
    try:
        qty = int(d["size"])
    except (KeyError, TypeError, ValueError):
        return {"success": False, "error": f"Invalid quantity: {d.get('size')!r}"}
    if qty <= 0:
        return {"success": False, "error": f"Quantity must be positive (got {qty})."}

    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)

        contract = Option(
            symbol=d["ticker"],
            lastTradeDateOrContractMonth=d["expiry"].replace("-", ""),
            strike=float(d["strike"]),
            right=_RIGHT[d["option_type"].upper()],
            exchange="SMART",
            currency="USD",
            multiplier="100",
        )

        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return {
                "success": False,
                "error": (
                    f"Contract not found: {d['ticker']} {d['option_type']} "
                    f"{d['strike']} exp {d['expiry']}. "
                    "Make sure the strike and expiry exist in IBKR's option chain."
                ),
            }

        order_type = d.get("order_type", "mkt")
        action     = d["action"].upper()
        tick       = _min_tick(ib, contract)

        if order_type == "limit":
            # Snap to a valid tick — IBKR rejects off-tick prices with Error 110
            order = LimitOrder(
                action=action,
                totalQuantity=d["size"],
                lmtPrice=_round_to_tick(float(d["limit_price"]), tick),
            )

        else:
            # mkt → buy fills at bid, sell fills at mid
            # if market is closed / no data, fall back to a true market order
            smart_type = "bid" if action == "BUY" else "mid"
            try:
                lmt_price = _resolve_bid_mid(ib, contract, smart_type, tick)
                order = LimitOrder(
                    action=action,
                    totalQuantity=d["size"],
                    lmtPrice=lmt_price,
                )
            except ValueError:
                order = MarketOrder(action=action, totalQuantity=d["size"])

        # Codes to ignore: informational TIF notice + market data farm status messages
        IGNORED_CODES = {
            0,     # no error
            10349, # TIF set to DAY (informational)
            2100, 2101, 2102, 2103, 2104, 2105,  # market data farm
            2106, 2107, 2108, 2109, 2110, 2119,  # market data farm
            2158,  # sec-def data farm
        }
        captured_errors: list[str] = []

        def _on_error(reqId, errorCode, errorString, contract):
            if errorCode not in IGNORED_CODES:
                captured_errors.append(f"Error {errorCode}: {errorString}")

        ib.errorEvent += _on_error
        trade = ib.placeOrder(contract, order)
        ib.sleep(6)
        ib.errorEvent -= _on_error

        # Also check advancedError and whyHeld fields
        extra = []
        if trade.orderStatus.whyHeld:
            extra.append(f"Hold reason: {trade.orderStatus.whyHeld}")
        if getattr(trade, 'advancedError', ''):
            extra.append(trade.advancedError)

        # captured_errors (from errorEvent) already covers everything in trade.log
        all_reasons = captured_errors + extra
        reason = " | ".join(all_reasons) if all_reasons else ""

        return {
            "success":  True,
            "order_id": trade.order.orderId,
            "status":   trade.orderStatus.status,
            "filled":   trade.orderStatus.filled,
            "reason":   reason,
        }

    except ConnectionRefusedError:
        return {
            "success": False,
            "error": (
                f"Connection refused at {_host()}:{_port()}. "
                "Make sure IB Gateway or TWS is running and API connections are enabled."
            ),
        }
    except TimeoutError:
        return {
            "success": False,
            "error": f"Connection timed out at {_host()}:{_port()}.",
        }
    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}

    finally:
        if ib.isConnected():
            ib.disconnect()


def _get_position_sync(d: dict) -> int:
    """Returns current position size for the contract (0 if not held)."""
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid() + 1, timeout=10)
        ib.sleep(1)
        target_right  = _RIGHT[d["option_type"].upper()]
        target_expiry = d["expiry"].replace("-", "")
        for pos in ib.positions(_account()):
            c = pos.contract
            if (c.symbol == d["ticker"] and
                    c.right == target_right and
                    abs(c.strike - float(d["strike"])) < 0.01 and
                    c.lastTradeDateOrContractMonth == target_expiry):
                return int(pos.position)
        return 0
    except Exception:
        return 0
    finally:
        if ib.isConnected():
            ib.disconnect()


async def get_position(d: dict) -> int:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _get_position_sync(d)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


# Last account code seen by a successful summary — lets the instant `status`
# card show the real account without a broker round-trip (and without a pin).
_LAST_ACCOUNT = ""


def last_account() -> str:
    return _LAST_ACCOUNT


def _get_account_summary_sync() -> dict:
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid() + 2, timeout=10)
        ib.sleep(1)

        acct = _account()
        if acct:
            # Pinned: read THIS sub-account only, and fail loudly if the login
            # does not actually contain it.
            rows = [r for r in ib.accountSummary() if r.account == acct]
            if not rows:
                return {"success": False, "error":
                        f"Pinned account {acct} not found under this login "
                        f"(available: {', '.join(ib.managedAccounts()) or 'none'})."}
            vals = {r.tag: r.value for r in rows if r.currency in ("USD", "")}
            vals["AccountCode"] = acct
        else:
            vals = {v.tag: v.value for v in ib.accountValues()
                    if v.currency in ("USD", "")}
        positions = ib.positions(_account())

        global _LAST_ACCOUNT
        if vals.get("AccountCode"):
            _LAST_ACCOUNT = vals["AccountCode"]

        return {
            "success":      True,
            "account":      vals.get("AccountCode", "—"),
            "net_liq":      float(vals.get("NetLiquidation", 0)),
            "avail_funds":  float(vals.get("AvailableFunds", 0)),
            "cash":         float(vals.get("TotalCashValue", 0)),
            "open_pos":     len(positions),
        }
    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


def _get_open_positions_sync() -> list:
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid() + 3, timeout=10)
        ib.sleep(1)
        result = []
        for pos in ib.positions(_account()):
            c = pos.contract
            if c.secType == "OPT" and pos.position != 0:
                expiry = c.lastTradeDateOrContractMonth
                if len(expiry) == 8:
                    expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
                result.append({
                    "ticker":      c.symbol,
                    "option_type": "Call" if c.right == "C" else "Put",
                    "strike":      c.strike,
                    "expiry":      expiry,
                    "qty":         int(pos.position),
                    "avg_cost":    round(pos.avgCost / 100, 2),
                })
        return result
    except Exception:
        return []
    finally:
        if ib.isConnected():
            ib.disconnect()


@_serialized
def _get_pending_orders_sync() -> list:
    ib = _AccountIB()
    try:
        # Connect as the SAME clientId that placed the bot's orders. reqAllOpenOrders
        # also returns orders placed MANUALLY in TWS/web (clientId 0) — verified
        # empirically 2026-08-05 that listing them does NOT adopt or cancel them.
        # The session-6 adoption bug involved orders from OTHER API clientIds; every
        # bot order lives on this base clientId, so none exist to be adopted.
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)
        ib.reqAllOpenOrders()
        ib.sleep(2)
        result = []
        for trade in ib.openTrades():
            o = trade.order
            c = trade.contract
            if c.secType != "OPT":
                continue
            if not _same_account(o):
                continue        # other sub-account's business — not ours to show
            expiry = c.lastTradeDateOrContractMonth
            if len(expiry) == 8:
                expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
            result.append({
                "order_id":    o.orderId,
                "action":      o.action.capitalize(),
                "qty":         int(o.totalQuantity),
                "ticker":      c.symbol,
                "option_type": "Call" if c.right == "C" else "Put",
                "strike":      c.strike,
                "expiry":      expiry,
                "order_type":  "limit" if o.orderType == "LMT" else "mkt",
                "limit_price": o.lmtPrice if o.orderType == "LMT" else None,
                "status":      trade.orderStatus.status,
                # Manual TWS/web orders report orderId 0 to API clients: they can
                # be shown but never cancelled or modified from here.
                "manual":      o.orderId == 0 or o.clientId == 0,
            })
        return result
    except Exception:
        return []
    finally:
        if ib.isConnected():
            ib.disconnect()


@_serialized
def _cancel_order_sync(order_id: int) -> dict:
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)
        # Direct protocol call — no reqAllOpenOrders (that rebinds orders and
        # causes them to be cancelled when this short-lived session disconnects)
        ib.client.cancelOrder(order_id, "")
        ib.sleep(2)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


@_serialized
def _modify_order_sync(order_id: int, new_price, order_info: dict) -> dict:
    """
    Cancel the existing order then place a replacement with the new price.
    order_info must contain: action, ticker, option_type, strike, expiry, qty
    Avoids reqAllOpenOrders so surviving orders are not rebound to this session.
    """
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)

        # Cancel original order directly by orderId
        ib.client.cancelOrder(order_id, "")
        ib.sleep(1)

        # Build replacement contract
        contract = Option(
            symbol=order_info["ticker"],
            lastTradeDateOrContractMonth=order_info["expiry"].replace("-", ""),
            strike=float(order_info["strike"]),
            right=_RIGHT[order_info["option_type"].upper()],
            exchange="SMART",
            currency="USD",
            multiplier="100",
        )
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return {"success": False, "error": "Contract not found"}

        action = order_info["action"].upper()
        tick   = _min_tick(ib, contract)
        qty    = abs(int(order_info["qty"]))

        if new_price is None:
            smart_type = "bid" if action == "BUY" else "mid"
            try:
                new_price = _resolve_bid_mid(ib, contract, smart_type, tick)
                order = LimitOrder(action=action, totalQuantity=qty, lmtPrice=new_price)
            except ValueError:
                order = MarketOrder(action=action, totalQuantity=qty)
        else:
            order = LimitOrder(action=action, totalQuantity=qty,
                               lmtPrice=_round_to_tick(float(new_price), tick))

        trade = ib.placeOrder(contract, order)
        ib.sleep(4)
        return {"success": True, "new_order_id": trade.order.orderId}
    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


async def get_open_positions() -> list:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _get_open_positions_sync()
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


async def get_pending_orders() -> list:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _get_pending_orders_sync()
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


async def cancel_order(order_id: int) -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _cancel_order_sync(order_id)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


async def modify_order(order_id: int, new_price, order_info: dict) -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _modify_order_sync(order_id, new_price, order_info)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


async def get_account_summary() -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _get_account_summary_sync()
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


def _get_market_data_sync(d: dict) -> dict:
    """
    Fetch market data for an options contract.
    1. Portfolio price — instant, no subscription, works for held positions.
    2. Live reqMktData (type 1) — requires OPRA subscription, snapshot=True.
    3. Delayed reqMktData (type 3) — free 15-20 min delay.
    Uses clientId=_cid()+5 (free slot, read dynamically).
    """
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid() + 5, timeout=10)
        ib.sleep(1)  # let account data arrive

        def _clean(v):
            return round(v, 2) if (v is not None and not math.isnan(v) and v > 0) else None

        # Step 1: check portfolio for last price — instant fallback, no subscription needed
        target_right   = _RIGHT[d["option_type"].upper()]
        target_expiry  = d["expiry"].replace("-", "")
        portfolio_last = None
        for item in ib.portfolio():
            c = item.contract
            if (c.symbol == d["ticker"] and
                    c.right == target_right and
                    abs(c.strike - float(d["strike"])) < 0.01 and
                    c.lastTradeDateOrContractMonth == target_expiry and
                    item.marketPrice > 0 and not math.isnan(item.marketPrice)):
                portfolio_last = round(item.marketPrice, 2)
                break

        # Step 2: qualify contract — SMART first, then CBOE for index options
        contract = Option(
            symbol=d["ticker"],
            lastTradeDateOrContractMonth=target_expiry,
            strike=float(d["strike"]),
            right=target_right,
            exchange="SMART", currency="USD", multiplier="100",
        )
        if not ib.qualifyContracts(contract):
            qualified = False
            for tc in ("SPXW", "SPX", "NDXP", "NDX"):
                alt = Option(
                    symbol=d["ticker"],
                    lastTradeDateOrContractMonth=target_expiry,
                    strike=float(d["strike"]),
                    right=target_right,
                    exchange="CBOE", currency="USD", multiplier="100",
                    tradingClass=tc,
                )
                if ib.qualifyContracts(alt):
                    contract = alt
                    qualified = True
                    break
            if not qualified:
                return {"success": True, "bid": None, "ask": None,
                        "last": portfolio_last, "delayed": False}

        # Index options (SPXW/SPX/NDXP/NDX): SMART routing triggers 10197 on reqMktData.
        # Switch to CBOE exchange for the actual data request (qualification via SMART is fine).
        _INDEX_CLASSES = {"SPXW", "SPX", "NDXP", "NDX"}
        if getattr(contract, "tradingClass", "") in _INDEX_CLASSES:
            contract = Option(
                conId=contract.conId,
                symbol=contract.symbol,
                lastTradeDateOrContractMonth=contract.lastTradeDateOrContractMonth,
                strike=contract.strike,
                right=contract.right,
                exchange="CBOE", currency="USD", multiplier="100",
                tradingClass=contract.tradingClass,
            )

        # Step 3: live data (requires OPRA subscription)
        # snapshot=True auto-cancels server-side after one tick, avoiding stale session locks
        ib.reqMarketDataType(1)
        ticker = ib.reqMktData(contract, "", snapshot=True)
        ib.sleep(6)
        bid  = _clean(ticker.bid)
        ask  = _clean(ticker.ask)
        last = _clean(ticker.last)
        if bid is not None or ask is not None or last is not None:
            return {"success": True, "bid": bid, "ask": ask,
                    "last": last if last is not None else portfolio_last, "delayed": False}

        # Step 4: delayed snapshot (free, 15-20 min delay)
        ib.reqMarketDataType(3)
        ticker = ib.reqMktData(contract, "", snapshot=True)
        ib.sleep(6)
        bid  = _clean(ticker.bid)
        ask  = _clean(ticker.ask)
        last = _clean(ticker.last)
        if bid is not None or ask is not None or last is not None:
            return {"success": True, "bid": bid, "ask": ask,
                    "last": last if last is not None else portfolio_last, "delayed": True}

        return {"success": True, "bid": None, "ask": None,
                "last": portfolio_last, "delayed": False}
    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


async def get_market_data(d: dict) -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _get_market_data_sync(d)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


def _order_budget() -> float:
    """Dollars to spend per automated signal."""
    try:
        return max(1.0, float(os.getenv("ORDER_BUDGET_USD", "1000")))
    except ValueError:
        return 1000.0


def _qualify(ib: IB, d: dict):
    """Qualify an option, falling back to CBOE trading classes for index options."""
    expiry = d["expiry"].replace("-", "")
    right = _RIGHT[d["option_type"].upper()]
    # Standard class first: adjusted chains (2AMZN, ...) also live on SMART and
    # IBKR rejects API orders on them ("IB-cleared orders are not allowed for
    # Flex options"). The equity standard class is named like the symbol.
    std = Option(symbol=d["ticker"], lastTradeDateOrContractMonth=expiry,
                 strike=float(d["strike"]), right=right,
                 exchange="SMART", currency="USD", multiplier="100",
                 tradingClass=d["ticker"])
    if ib.qualifyContracts(std):
        return std
    contract = Option(symbol=d["ticker"], lastTradeDateOrContractMonth=expiry,
                      strike=float(d["strike"]), right=right,
                      exchange="SMART", currency="USD", multiplier="100")
    if ib.qualifyContracts(contract):
        return contract
    for tc in ("SPXW", "SPX", "NDXP", "NDX"):
        alt = Option(symbol=d["ticker"], lastTradeDateOrContractMonth=expiry,
                     strike=float(d["strike"]), right=right,
                     exchange="CBOE", currency="USD", multiplier="100", tradingClass=tc)
        if ib.qualifyContracts(alt):
            return alt
    return None


def _quote_contract(contract):
    """Index options must be quoted over CBOE or IBKR returns 10197."""
    if getattr(contract, "tradingClass", "") in {"SPXW", "SPX", "NDXP", "NDX"}:
        return Option(
            conId=contract.conId, symbol=contract.symbol,
            lastTradeDateOrContractMonth=contract.lastTradeDateOrContractMonth,
            strike=contract.strike, right=contract.right,
            exchange="CBOE", currency="USD", multiplier="100",
            tradingClass=contract.tradingClass)
    return contract


def _live_ask(ib: IB, contract) -> float | None:
    """Current ask — what a market buy actually pays; used for sizing."""
    ib.reqMarketDataType(1)
    ticker = ib.reqMktData(_quote_contract(contract), "", snapshot=True)
    ib.sleep(6)
    ask = ticker.ask
    if ask is None or math.isnan(ask) or ask <= 0:
        return None
    return float(ask)


# Owner spec 2026-08-14: the ladder carries on until FULLY FILLED — no market
# fallback. 120 cycles (~13 min, a ±$6.00 price walk) is the sanity ceiling
# that keeps a stuck ladder from holding the order lock forever; ending there
# surfaces the manual market button instead of trading automatically.
CHASE_ENTRY_CYCLES = 120
CHASE_SELL_CYCLES = 120


def _chase_mid(ib: IB, contract, action: str, qty: int, tick: float,
               max_cycles: int) -> dict:
    """
    The price-ladder chase (owner spec, 2026-08-14, replacing the fresh-mid
    version): independent 5-second cycles.

      BUY : first order at the BID, then +5 cents every cycle until filled.
      SELL: first order at the ASK, then -5 cents every cycle until filled.

    Each cycle places a limit for WHATEVER IS STILL UNFILLED, waits 5 s,
    cancels what still rests, recounts, steps the price, goes again. A cycle's
    outcome — filled, partial, rejected, any error — only feeds the next
    cycle's remainder; an exception inside a cycle ends the ladder cleanly with
    the fills already made. Stops when filled or when max_cycles is spent; the
    caller decides what happens to a remainder (market fallback).

    The step is max(5 cents, one tick) so coarse-tick contracts (SPX) still
    move; a SELL price floors at one tick. Needs a two-sided book ONLY at the
    start (no_mid=True otherwise) — the ladder itself never re-reads quotes,
    so losing market data mid-ladder does not stop it.
    """
    qc = _quote_contract(contract)
    ib.reqMarketDataType(1)
    tkr = ib.reqMktData(qc, "", snapshot=False)
    ib.sleep(2)                      # let the first tick land
    bid, ask = tkr.bid, tkr.ask
    try:
        ib.cancelMktData(qc)
    except Exception:
        pass
    if (bid is None or math.isnan(bid) or bid <= 0
            or ask is None or math.isnan(ask) or ask <= 0 or ask < bid):
        return {"filled": 0, "remaining": qty, "avg_price": None, "cycles": 0,
                "first_mid": None, "last_status": None, "no_mid": True,
                "last_order_id": 0}

    step = max(0.05, tick)
    px = _round_to_tick(bid if action == "BUY" else ask, tick)
    first_px = px
    fills: list[tuple[int, float]] = []
    remaining = qty
    cycles = 0
    last_status = None
    last_order_id = 0
    while remaining > 0 and cycles < max_cycles:
        cycles += 1
        try:
            o = LimitOrder(action, remaining, px)
            o.orderId = ib.client.getReqId()
            last_order_id = o.orderId
            o.tif = "DAY"
            tr = ib.placeOrder(contract, o)
            ib.sleep(5)
            if tr.orderStatus.status not in ("Filled", "Cancelled", "Inactive"):
                ib.cancelOrder(o)
                ib.sleep(1)          # give the cancel (or a racing fill) a beat
            got = int(float(tr.orderStatus.filled or 0))
            if got > 0:
                fills.append((got, float(tr.orderStatus.avgFillPrice or px)))
            last_status = tr.orderStatus.status
            remaining = qty - sum(g for g, _ in fills)
            print(f"[chase] {action} cycle {cycles}: limit {px} -> "
                  f"{got} filled this cycle, {remaining} left "
                  f"({last_status})", flush=True)
        except Exception as e:
            # Connection drop or anything else mid-cycle: keep what filled,
            # hand the remainder back — the caller's market fallback (and the
            # listener's transient retry above it) take over.
            print(f"[chase] {action} cycle {cycles} error: {e!r} — "
                  f"ending ladder with {remaining} left", flush=True)
            break
        nxt = px + step if action == "BUY" else px - step
        px = max(_round_to_tick(nxt, tick), tick)

    filled = qty - remaining
    avg = (sum(g * p for g, p in fills) / filled) if filled else None
    return {"filled": filled, "remaining": remaining, "avg_price": avg,
            "cycles": cycles, "first_mid": first_px,
            "last_status": last_status, "no_mid": False,
            "last_order_id": last_order_id}


def _market_remainder(ib: IB, contract, action: str, qty: int,
                      poll: int = 15) -> dict:
    """The chase's terminal fallback: send what is left at MARKET and wait."""
    o = MarketOrder(action, qty)
    o.orderId = ib.client.getReqId()
    o.tif = "DAY"
    tr = ib.placeOrder(contract, o)
    for _ in range(poll):
        ib.sleep(1)
        if tr.orderStatus.status in ("Filled", "Cancelled", "Inactive"):
            break
    return {"order_id": tr.order.orderId, "status": tr.orderStatus.status,
            "filled": float(tr.orderStatus.filled or 0),
            "avg_price": tr.orderStatus.avgFillPrice or None}


def _live_mid(ib: IB, contract) -> float | None:
    """
    Current bid/ask midpoint. Needs BOTH sides of the book — a one-sided quote has
    no mid. Kept for the mid-limit order mode (the plan when the account goes live).
    """
    ib.reqMarketDataType(1)
    ticker = ib.reqMktData(_quote_contract(contract), "", snapshot=True)
    ib.sleep(6)
    bid, ask = ticker.bid, ticker.ask
    if (bid is None or math.isnan(bid) or bid <= 0
            or ask is None or math.isnan(ask) or ask <= 0 or ask < bid):
        return None
    return (float(bid) + float(ask)) / 2


@_serialized
def _place_bracket_sync(d: dict) -> dict:
    """
    The automated entry, priced by the market-data reality: LIMIT at the bid/ask
    midpoint when the bot holds the live data share, MARKET sized off the card
    price when it does not. Either way a sell limit at the signal's first target
    is attached as a child order.

    The child carries parentId and only activates once the buy fills, so we can
    never end up holding a naked sell.

    d needs: ticker, option_type, strike, expiry, first_target
    """
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)

        contract = _qualify(ib, d)
        if contract is None:
            return {"success": False, "error":
                    f"Contract not found: {d['ticker']} {d['option_type']} "
                    f"{d['strike']} exp {d['expiry']}"}

        # A second buy signal for a contract whose take-profit is still resting is a
        # new ROUND on the position, not a parallel entry. IBKR rejects a fresh
        # bracket outright — Error 201 "Cannot have open orders on both sides of the
        # same US Option contract" — so route it through the buy_more flow instead:
        # cancel the TP, buy at market, re-place the TP for the whole position.
        # (Found by the 2026-08-04 archive replay; the provider's numbered rounds
        # (02)/(03) on the same strike hit this in production.)
        ib.reqOpenOrders()
        ib.sleep(2)
        has_tp = any(t.contract.conId == contract.conId
                     and t.order.action.upper() == "SELL"
                     and t.order.orderType == "LMT"
                     and _same_account(t.order)
                     for t in ib.openTrades())
        if has_tp:
            ib.disconnect()          # buy_more reconnects as the same clientId
            result = _buy_more_sync(d)     # RLock makes this re-entrant
            result["routed"] = "buy_more"
            return result

        # Pricing rule (owner, 2026-08-06): the market-data reality decides, per
        # order. Bot holds the live data share -> LIMIT at the bid/ask midpoint.
        # No quotes (client's live session holds them, Error 10197) -> MARKET,
        # sized from the price Claude read off the card — a real quote, just a
        # few seconds stale.
        tick = _min_tick(ib, contract)
        mid = _live_mid(ib, contract)
        if mid is None and not d.get("force_market"):
            # NO live data -> place NOTHING (owner, 2026-08-14). The signal was
            # parsed fine; the user gets a message + the market button and
            # decides himself. force_market (the button) comes back through
            # here and takes the atomic market path below.
            return {"success": False, "no_data": True, "error":
                    "Signal parsed correctly, but there is no live market data "
                    "to price the entry."}
        if mid is not None:
            entry = _round_to_tick(mid, tick)     # the actual buy limit price
            price_src = "live mid"
        else:
            card = d.get("limit_price")
            if not card:
                return {"success": False, "error":
                        "No live quote and no price on the card — cannot size the order."}
            entry = _round_to_tick(float(card), tick)   # sizing estimate; buy is at market
            price_src = "signal card"

        budget = _order_budget()
        if d.get("force_qty"):
            # Remainder retry from the button: buy exactly this many, no re-sizing.
            qty = int(d["force_qty"])
        else:
            qty = int(budget // (entry * 100))    # round DOWN, never exceed the budget
        if qty < 1:
            return {"success": False, "error":
                    f"One contract costs ${entry * 100:,.0f}, over the "
                    f"${budget:,.0f} budget. Signal skipped."}
        # The budget is now the ONLY thing bounding size — no contract cap.
        capped = qty

        target = d.get("first_target")
        target = _round_to_tick(float(target), tick) if target else None
        if target is not None and target <= entry:
            return {"success": False, "error":
                    f"Target {target} is not above the entry {entry} — refusing."}

        IGNORED = {0, 10349, 2100, 2101, 2102, 2103, 2104, 2105,
                   2106, 2107, 2108, 2109, 2110, 2119, 2158}
        errors: list[str] = []
        ib.errorEvent += lambda rid, code, s, c: (
            errors.append(f"Error {code}: {s}") if code not in IGNORED else None)

        if mid is not None and not d.get("force_market"):
            # LADDER entry (owner spec, 2026-08-14): bid, +5c per 5-second
            # cycle, carried until FULLY FILLED — NO market fallback. A
            # remainder after the sanity ceiling (or an unexpected error inside
            # the ladder) goes back to the user as a button, never to market on
            # our own. The take-profit is placed ONCE at the end, for the TOTAL
            # bought — it cannot ride each 5-second order.
            chase = _chase_mid(ib, contract, "BUY", capped, tick,
                               CHASE_ENTRY_CYCLES)
            bought = chase["filled"]

            if bought <= 0:
                return {"success": False, "error":
                        f"Price ladder ended after {chase['cycles']} cycles "
                        f"with nothing bought."}

            child_trade = None
            if target is not None and bought > 0:
                sell = LimitOrder("SELL", bought, target)
                sell.orderId = ib.client.getReqId()
                sell.tif = "DAY"     # dies at the close; morning sweep re-arms
                sell.transmit = True
                child_trade = ib.placeOrder(contract, sell)
                ib.sleep(2)
                _tp_registry_set(d, target, bought)

            entry_est = chase["first_mid"] or entry
            avg = chase["avg_price"]
            return {
                "success":      True,
                "order_id":     chase["last_order_id"],
                "status":       ("Filled" if bought >= capped
                                 else chase["last_status"] or "Cancelled"),
                "filled":       float(bought),
                "avg_price":    avg,
                "qty":          capped,
                "remaining":    chase["remaining"],
                "entry":        entry_est,
                "entry_type":   f"limit (ladder, {chase['cycles']} cycles)",
                "price_src":    price_src,
                "target":       target,
                "cost":         round(bought * (avg or entry_est) * 100, 2),
                "exit_id":      child_trade.order.orderId if child_trade else None,
                "exit_status":  child_trade.orderStatus.status if child_trade else None,
                "reason":       " | ".join(errors),
            }

        # No live data (or the user's force-market button): the original ATOMIC
        # market bracket — parent + take-profit transmitted together so no leg
        # can reach the exchange alone. Safe in and out of market hours.
        parent = MarketOrder("BUY", capped)
        entry_type = "market"
        parent.orderId = ib.client.getReqId()
        parent.tif = "DAY"
        parent.transmit = target is None          # no target -> nothing to attach

        orders = [parent]
        if target is not None:
            child = LimitOrder("SELL", capped, target)
            child.orderId = ib.client.getReqId()
            child.parentId = parent.orderId
            # DAY (owner, 2026-08-12): the TP dies at the close and the morning
            # sweep re-arms it 2min10s after the next open — selling at market
            # if the price gapped above the target overnight.
            child.tif = "DAY"
            child.transmit = True                 # transmits the whole bracket
            orders.append(child)
            _tp_registry_set(d, target, capped)

        trades = [ib.placeOrder(contract, o) for o in orders]
        parent_trade = trades[0]
        child_trade = trades[1] if len(trades) > 1 else None

        # A market order usually fills in seconds, but not always. Wait a little
        # so the notification reports what actually happened rather than a snapshot
        # taken before the exchange replied.
        DONE = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
        for _ in range(10):                       # up to ~20s
            ib.sleep(2)
            if parent_trade.orderStatus.status in DONE:
                break

        ib.sleep(1)                               # let the child's status settle
        return {
            "success":      True,
            "order_id":     parent_trade.order.orderId,
            "status":       parent_trade.orderStatus.status,
            "filled":       parent_trade.orderStatus.filled,
            "avg_price":    parent_trade.orderStatus.avgFillPrice or None,
            "qty":          capped,
            "entry":        entry,
            "entry_type":   entry_type,
            "price_src":    price_src,
            "target":       target,
            "cost":         round(capped * entry * 100, 2),
            "exit_id":      child_trade.order.orderId if child_trade else None,
            "exit_status":  child_trade.orderStatus.status if child_trade else None,
            "reason":       " | ".join(errors),
        }

    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


@_serialized
def _emergency_exit_sync(d: dict) -> dict:
    """
    Emergency exit — a خفف card arrived with no mention of الهدف.

    Two checks, and only two:
      1. the reader already applied the الهدف rule, so anything reaching here is an
         exit rather than a target announcement
      2. we must actually hold the contract

    If we hold it: cancel the resting sell limit and sell the lot — limit at the
    bid/ask mid when the bot holds the live data share, market when it does not.
    If we do not: do nothing. A SELL with no position writes a naked option.

    There is deliberately NO price comparison. Holding the contract already proves
    the take-profit did not fill, and a price test would refuse to exit in exactly
    the case you most want out of — still holding while the price ran past the limit.

    d needs: ticker, option_type, strike, expiry
    """
    ib = _AccountIB()
    try:
        # Base clientId on purpose: reqOpenOrders() must see the GTC child we placed,
        # and a cancel has to come from the client that owns the order (see rule #1).
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)
        ib.sleep(1)

        contract = _qualify(ib, d)
        if contract is None:
            return {"success": False, "error":
                    f"Contract not found: {d['ticker']} {d['option_type']} "
                    f"{d['strike']} exp {d['expiry']}"}

        held = 0
        for pos in ib.positions(_account()):
            if pos.contract.conId == contract.conId:
                held = int(pos.position)
                break

        # Check 2. Nothing held means nothing to exit — never sell into a short.
        if held <= 0:
            return {"success": True, "acted": False, "held": held,
                    "skip_reason": "we hold none of this contract"}

        # Our resting take-profit for this contract, if it is still working.
        ib.reqOpenOrders()
        ib.sleep(2)
        resting = None
        for trade in ib.openTrades():
            if (trade.contract.conId == contract.conId
                    and trade.order.action.upper() == "SELL"
                    and trade.order.orderType == "LMT"
                    and _same_account(trade.order)):
                resting = trade
                break

        limit_price = resting.order.lmtPrice if resting is not None else None

        # NO live data -> touch NOTHING (owner, 2026-08-14): the take-profit
        # stays resting, the position stays protected, and the user gets the
        # parsed signal + the market button. Checked BEFORE the cancel on
        # purpose — the old order cancelled the TP first and a pricing failure
        # then left the position naked (the MU lesson).
        if _live_mid(ib, contract) is None:
            return {"success": True, "acted": False, "no_data": True,
                    "held": held,
                    "limit_price": (float(limit_price)
                                    if limit_price is not None else None),
                    "tp_order_id": resting.order.orderId if resting else 0,
                    "skip_reason": "no live market data"}

        IGNORED = {0, 10349, 2100, 2101, 2102, 2103, 2104, 2105,
                   2106, 2107, 2108, 2109, 2110, 2119, 2158}
        errors: list[str] = []
        ib.errorEvent += lambda rid, code, s, c: (
            errors.append(f"Error {code}: {s}") if code not in IGNORED else None)

        # Cancel first. Selling while the limit is still working would try to sell
        # the same contracts twice and the second leg would be a short.
        cancelled_id = None
        if resting is not None:
            cancelled_id = resting.order.orderId
            ib.cancelOrder(resting.order)
            ib.sleep(2)

        # LADDER exit (owner spec, 2026-08-14): ask, -5c per 5-second cycle,
        # carried until FULLY FILLED — NO automatic market, ever. A remainder
        # (ceiling, error, or the book dying in the race after the TP cancel)
        # goes back to the user as the button, which sells what is actually
        # held at market on HIS press.
        tick = _min_tick(ib, contract)
        chase = _chase_mid(ib, contract, "SELL", held, tick, CHASE_SELL_CYCLES)
        total = chase["filled"]

        _tp_registry_remove(d)       # position is being closed — sweep must forget it
        return {
            "success":      True,
            "acted":        True,
            "held":         held,
            "limit_price":  float(limit_price) if limit_price is not None else None,
            "cancelled_id": cancelled_id,
            "order_id":     chase["last_order_id"],
            "status":       ("Filled" if total >= held
                             else (chase["last_status"] or "Cancelled")),
            "filled":       float(total),
            "avg_price":    chase["avg_price"],
            "exit_px":      chase["first_mid"],
            "exit_type":    f"limit (ladder, {chase['cycles']} cycles)",
            "reason":       " | ".join(errors),
        }

    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


async def emergency_exit(d: dict) -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _emergency_exit_sync(d)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


@_serialized
def _buy_more_sync(d: dict) -> dict:
    """
    buy_more — a المتوسط (averaging-down) card arrived for a contract we hold.

    Sequence, in the client's specified order:
      1. we must actually hold the contract — otherwise skip (averaging into a
         position we never opened would be a fresh entry, not an average)
      2. cancel the resting take-profit
      3. buy more — limit at the mid with the data share, market without it —
         sized by ORDER_BUDGET_USD like any other entry (an unfilled remainder is
         cancelled before step 4 — a resting BUY would make the take-profit
         re-place bounce on IBKR's both-sides rule)
      4. re-place the take-profit as a GTC sell for the WHOLE position

    The new target comes from THIS message's الاهداف line (averaging messages carry
    revised targets); if it has none we reuse the cancelled order's price.

    The sequence is not atomic, so it is ordered to self-heal: the re-place in step 4
    sells whatever we actually hold after step 3 — if the buy was rejected or did not
    fill, that is the original quantity and the original take-profit is restored.

    d needs: ticker, option_type, strike, expiry; optionally first_target, limit_price
    """
    ib = _AccountIB()
    try:
        # Base clientId: reqOpenOrders() must see the GTC sell we placed, and the
        # cancel has to come from the client that owns the order (rule #1).
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)
        ib.sleep(1)

        contract = _qualify(ib, d)
        if contract is None:
            return {"success": False, "error":
                    f"Contract not found: {d['ticker']} {d['option_type']} "
                    f"{d['strike']} exp {d['expiry']}"}

        held = 0
        for pos in ib.positions(_account()):
            if pos.contract.conId == contract.conId:
                held = int(pos.position)
                break

        if held <= 0:
            return {"success": True, "acted": False, "held": held,
                    "skip_reason": "we hold none of this contract"}

        # The resting take-profit, if it is still working.
        ib.reqOpenOrders()
        ib.sleep(2)
        resting = None
        for trade in ib.openTrades():
            if (trade.contract.conId == contract.conId
                    and trade.order.action.upper() == "SELL"
                    and trade.order.orderType == "LMT"
                    and _same_account(trade.order)):
                resting = trade
                break

        old_target = resting.order.lmtPrice if resting is not None else None

        # Revised target from this message, else keep the old one.
        tick = _min_tick(ib, contract)
        target = d.get("first_target") or old_target
        target = _round_to_tick(float(target), tick) if target else None

        # Pricing rule (owner, 2026-08-06): LIMIT at the mid with the data share,
        # MARKET sized off the card price without it — same rule as the entry.
        mid = _live_mid(ib, contract)
        if mid is not None:
            buy_px = _round_to_tick(mid, tick)
            price_src = "live mid"
        else:
            card = d.get("limit_price")
            if not card:
                return {"success": False, "error":
                        "No live quote and no price on the card — cannot size the order."}
            buy_px = _round_to_tick(float(card), tick)
            price_src = "signal card"

        budget = _order_budget()
        if d.get("force_qty"):
            qty = int(d["force_qty"])             # remainder retry: exact size
        else:
            qty = int(budget // (buy_px * 100))   # round DOWN, never exceed the budget
        if qty < 1:
            return {"success": False, "error":
                    f"One contract costs ${buy_px * 100:,.0f}, over the "
                    f"${budget:,.0f} budget. Signal skipped."}

        IGNORED = {0, 10349, 2100, 2101, 2102, 2103, 2104, 2105,
                   2106, 2107, 2108, 2109, 2110, 2119, 2158}
        errors: list[str] = []
        ib.errorEvent += lambda rid, code, s, c: (
            errors.append(f"Error {code}: {s}") if code not in IGNORED else None)

        # 2. Cancel the take-profit first (the client's specified order). While it
        # rests, part of the position is already spoken for.
        cancelled_id = None
        if resting is not None:
            cancelled_id = resting.order.orderId
            ib.cancelOrder(resting.order)
            ib.sleep(2)

        # 3. Buy more — mid limit with the data share, market without it (or on
        # the user's explicit force-market button).
        if mid is not None and not d.get("force_market"):
            buy = LimitOrder("BUY", qty, buy_px)
        else:
            buy = MarketOrder("BUY", qty)
        buy.orderId = ib.client.getReqId()
        buy.tif = "DAY"              # never leave TIF unset — see the GTC bug in §4
        buy_trade = ib.placeOrder(contract, buy)
        for _ in range(10):                      # up to ~20s
            ib.sleep(2)
            if buy_trade.orderStatus.status in ("Filled", "Cancelled", "Inactive"):
                break

        # Still working after the wait? For a mid limit: AUTO-FALLBACK — cancel the
        # limit and re-send the remainder at MARKET (owner, 2026-08-06). Anything
        # that STILL rests after that (closed market) is cancelled, because a
        # resting BUY plus a fresh SELL on the same contract is IBKR's both-sides
        # rejection (Error 201) — the TP re-place would bounce and the position
        # would be left with no exit at all. Whatever DID fill is captured below.
        fallback_qty = 0
        fb_trade = None
        if buy_trade.orderStatus.status not in ("Filled", "Cancelled", "Inactive"):
            remaining = int(buy_trade.orderStatus.remaining or 0) \
                or qty - int(buy_trade.orderStatus.filled or 0)
            ib.cancelOrder(buy_trade.order)
            ib.sleep(2)
            if mid is not None and remaining > 0:
                fallback_qty = remaining
                fb = MarketOrder("BUY", remaining)
                fb.orderId = ib.client.getReqId()
                fb.tif = "DAY"
                fb_trade = ib.placeOrder(contract, fb)
                for _ in range(10):                  # up to ~20s
                    ib.sleep(2)
                    if fb_trade.orderStatus.status in ("Filled", "Cancelled", "Inactive"):
                        break
                if fb_trade.orderStatus.status not in ("Filled", "Cancelled", "Inactive"):
                    ib.cancelOrder(fb_trade.order)
                    ib.sleep(2)

        # 4. Re-place the take-profit for whatever we ACTUALLY hold now. If the buy
        # failed, new_held == held and this restores the original protection.
        ib.sleep(1)
        new_held = 0
        for pos in ib.positions(_account()):
            if pos.contract.conId == contract.conId:
                new_held = int(pos.position)
                break

        sell_trade = None
        if new_held > 0 and target is not None:
            sell = LimitOrder("SELL", new_held, target)
            sell.orderId = ib.client.getReqId()
            sell.tif = "DAY"         # dies at the close; morning sweep re-arms
            sell.transmit = True
            sell_trade = ib.placeOrder(contract, sell)
            ib.sleep(2)
            _tp_registry_set(d, target, new_held)

        fb_filled = float(fb_trade.orderStatus.filled or 0) if fb_trade else 0.0
        return {
            "success":      True,
            "acted":        True,
            "held_before":  held,
            "held_after":   new_held,
            "bought":       float(buy_trade.orderStatus.filled or 0) + fb_filled,
            "avg_price":    (fb_trade.orderStatus.avgFillPrice if fb_filled
                             else buy_trade.orderStatus.avgFillPrice) or None,
            "buy_status":   buy_trade.orderStatus.status,
            "fallback_qty": fallback_qty,
            "fallback_status": fb_trade.orderStatus.status if fb_trade else None,
            "buy_id":       buy_trade.order.orderId,
            "entry_est":    buy_px,
            "price_src":    price_src,
            "cancelled_id": cancelled_id,
            "old_target":   float(old_target) if old_target is not None else None,
            "target":       target,
            "sell_id":      sell_trade.order.orderId if sell_trade else None,
            "sell_status":  sell_trade.orderStatus.status if sell_trade else None,
            "sell_qty":     new_held if sell_trade else 0,
            "reason":       " | ".join(errors),
        }

    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


async def buy_more(d: dict) -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _buy_more_sync(d)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


@_serialized
def _place_tp_sync(d: dict) -> dict:
    """
    Place a MISSING take-profit for a position we already hold.

    Exists for the channel's edit habit (seen live 2026-08-05): the author posts
    the contract card and edits the الاهداف line in seconds later, so a buy can
    fill before any target exists. When the edit arrives, this adds the GTC sell.
    It never moves or replaces a sell that is already resting.

    d needs: ticker, option_type, strike, expiry, first_target
    """
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)
        ib.sleep(1)
        contract = _qualify(ib, d)
        if contract is None:
            return {"success": False, "error":
                    f"Contract not found: {d['ticker']} {d['option_type']} "
                    f"{d['strike']} exp {d['expiry']}"}
        held = next((int(p.position) for p in ib.positions(_account())
                     if p.contract.conId == contract.conId), 0)
        if held <= 0:
            return {"success": True, "acted": False, "held": held,
                    "skip_reason": "we hold none of this contract"}
        ib.reqOpenOrders()
        ib.sleep(2)
        for t in ib.openTrades():
            if (t.contract.conId == contract.conId
                    and t.order.action.upper() == "SELL"
                    and _same_account(t.order)):
                return {"success": True, "acted": False, "held": held,
                        "skip_reason":
                        f"a sell is already resting (order {t.order.orderId})"}
        tick = _min_tick(ib, contract)
        target = _round_to_tick(float(d["first_target"]), tick)
        sell = LimitOrder("SELL", held, target)
        sell.orderId = ib.client.getReqId()
        sell.tif = "DAY"             # dies at the close; morning sweep re-arms
        sell.transmit = True
        trade = ib.placeOrder(contract, sell)
        ib.sleep(3)
        _tp_registry_set(d, target, held)
        return {"success": True, "acted": True, "held": held, "target": target,
                "order_id": trade.order.orderId,
                "status": trade.orderStatus.status}
    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


async def place_take_profit(d: dict) -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _place_tp_sync(d)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


@_serialized
def _switch_to_market_sync(info: dict) -> dict:
    """
    Convert a resting automated limit order to MARKET — the "Switch to MARKET"
    button on an unfilled notification.

    The order is re-checked here, not trusted from the button: it may have filled
    while the user was reading. Remaining quantity comes from the live order book.

    For an ENTRY the take-profit child dies with the cancelled parent, so the
    market replacement goes out as a fresh bracket (market parent + GTC limit
    child covering the signal's full quantity) — protection is never dropped.
    For an EXIT the replacement is a plain market sell of what is left.

    info needs: order_id, kind ('entry'|'exit'), ticker, option_type, strike,
    expiry; optionally target (entry only).
    """
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)
        ib.reqOpenOrders()          # this client's orders only — never reqAllOpenOrders
        ib.sleep(2)

        old = next((t for t in ib.openTrades()
                    if t.order.orderId == info["order_id"]), None)
        if old is None:
            # POSITION TRUTH for exits (owner-approved 2026-08-12, the MU
            # incident): the original sell being gone can mean filled, cancelled
            # — or REJECTED at birth. The position decides which: still held ->
            # sell it at market right now; zero held -> it genuinely completed.
            if info.get("kind") == "exit":
                contract = _qualify(ib, info)
                if contract is None:
                    return {"success": False, "error": "Contract not found."}
                held = next((int(p.position) for p in ib.positions(_account())
                             if p.contract.conId == contract.conId), 0)
                if held <= 0:
                    return {"success": False, "error":
                            "Nothing left to sell — the exit already completed. "
                            "Check `open positions` to confirm."}
                o = MarketOrder("SELL", held)
                o.orderId = ib.client.getReqId()
                o.tif = "DAY"
                tr = ib.placeOrder(contract, o)
                for _ in range(10):
                    ib.sleep(2)
                    if tr.orderStatus.status in ("Filled", "Cancelled", "Inactive"):
                        break
                return {"success": True, "action": "SELL", "qty": held,
                        "order_id": tr.order.orderId,
                        "status": tr.orderStatus.status,
                        "filled": float(tr.orderStatus.filled or 0),
                        "avg_price": tr.orderStatus.avgFillPrice or None,
                        "exit_id": None, "target": None, "reason": ""}
            return {"success": False, "error":
                    "That order is no longer working — it filled or was cancelled. "
                    "Check `pending orders` and `open positions`."}
        remaining = int(old.orderStatus.remaining or old.order.totalQuantity)
        already = int(old.order.totalQuantity) - remaining
        if remaining < 1:
            return {"success": False, "error": "Nothing left unfilled on that order."}

        contract = _qualify(ib, info)
        if contract is None:
            return {"success": False, "error": "Contract not found."}

        IGNORED = {0, 10349, 2100, 2101, 2102, 2103, 2104, 2105,
                   2106, 2107, 2108, 2109, 2110, 2119, 2158}
        errors: list[str] = []
        ib.errorEvent += lambda rid, code, s, c: (
            errors.append(f"Error {code}: {s}") if code not in IGNORED else None)

        ib.cancelOrder(old.order)   # an entry's TP child is cancelled with its parent
        ib.sleep(2)

        is_entry = info.get("kind") == "entry"
        action = "BUY" if is_entry else "SELL"
        target = info.get("target") if is_entry else None

        parent = MarketOrder(action, remaining)
        parent.orderId = ib.client.getReqId()
        parent.tif = "DAY"
        parent.transmit = target is None
        orders = [parent]
        if target is not None:
            # The TP must cover the WHOLE position this signal built — the part that
            # already filled on the limit lost its child when the parent was cancelled.
            tick = _min_tick(ib, contract)
            child = LimitOrder("SELL", remaining + already,
                               _round_to_tick(float(target), tick))
            child.orderId = ib.client.getReqId()
            child.parentId = parent.orderId
            child.tif = "DAY"       # dies at the close; morning sweep re-arms
            child.transmit = True
            orders.append(child)
            _tp_registry_set(info, float(target), remaining + already)

        trades = [ib.placeOrder(contract, o) for o in orders]
        ptrade = trades[0]
        for _ in range(10):                      # up to ~20s
            ib.sleep(2)
            if ptrade.orderStatus.status in ("Filled", "Cancelled", "Inactive"):
                break

        return {
            "success":   True,
            "action":    action,
            "qty":       remaining,
            "order_id":  ptrade.order.orderId,
            "status":    ptrade.orderStatus.status,
            "filled":    float(ptrade.orderStatus.filled or 0),
            "avg_price": ptrade.orderStatus.avgFillPrice or None,
            "exit_id":   trades[1].order.orderId if len(trades) > 1 else None,
            "target":    target,
            "reason":    " | ".join(errors),
        }
    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


async def switch_to_market(info: dict) -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _switch_to_market_sync(info)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


# Liquid names for the order-book spot check — one is picked at random per check.
_BOOK_SAMPLE_TICKERS = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA")


@_serialized
def _morning_tp_sweep_sync() -> list:
    """
    The 09:32:10 ET sweep (owner, 2026-08-12): TPs are DAY orders now, so
    overnight positions wake up unprotected. For every BOT-managed position in
    the TP registry (manual positions are not in it and are never touched):

      - no longer held           -> drop from the registry, nothing to do
      - a sell already resting   -> skip (protected already)
      - price ABOVE yesterday's target -> the gap beat the target: SELL AT MARKET
      - otherwise                -> re-place yesterday's limit sell, DAY again

    No live quote for a contract counts as "not above": re-arming the limit is
    the safe default when the price cannot be compared.
    """
    results = []
    reg = _tp_registry_all()
    if not reg:
        return results
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid(), timeout=15)
        ib.sleep(1)
        # ALL open orders, not just the bot's own: a sell the client placed BY
        # HAND (clientId 0, invisible to reqOpenOrders) must also count as
        # protection — re-arming on top of it would oversell the position.
        # Listing via reqAllOpenOrders is read-only safe (verified 2026-08-05).
        ib.reqAllOpenOrders()
        ib.sleep(2)
        resting_sells = {t.contract.conId for t in ib.openTrades()
                         if t.order.action.upper() == "SELL"
                         and _same_account(t.order)}

        for key, entry in list(reg.items()):
            r = {"key": key, "ticker": entry["ticker"], "strike": entry["strike"],
                 "option_type": entry["option_type"], "expiry": entry["expiry"],
                 "target": entry["target"]}
            try:
                contract = _qualify(ib, entry)
                if contract is None:
                    r["action"] = "error"
                    r["detail"] = "contract no longer resolves (expired?)"
                    reg.pop(key, None)
                    results.append(r)
                    continue
                held = next((int(p.position) for p in ib.positions(_account())
                             if p.contract.conId == contract.conId), 0)
                if held <= 0:
                    reg.pop(key, None)        # closed sometime yesterday
                    continue
                r["held"] = held
                if contract.conId in resting_sells:
                    r["action"] = "skip"
                    r["detail"] = "a sell is already resting"
                    results.append(r)
                    continue

                mid = _live_mid(ib, contract)
                r["mid"] = mid
                if mid is not None and mid > entry["target"]:
                    # Gapped above the target overnight — get out via the
                    # LADDER (owner spec, 2026-08-14): ask, -5c per cycle,
                    # until filled. NO market fallback — a remainder goes back
                    # to the user with the button.
                    tick = _min_tick(ib, contract)
                    chase = _chase_mid(ib, contract, "SELL", held, tick,
                                       CHASE_SELL_CYCLES)
                    r["action"] = "market_sell"
                    r["how"] = f"ladder, {chase['cycles']} cycles"
                    r["status"] = ("Filled" if chase["filled"] >= held
                                   else chase["last_status"] or "Cancelled")
                    r["filled"] = float(chase["filled"])
                    r["avg_price"] = chase["avg_price"]
                    r["last_order_id"] = chase["last_order_id"]
                    if chase["remaining"] > 0:
                        # keep it registered — the position is NOT closed yet
                        entry["qty"] = chase["remaining"]
                    else:
                        reg.pop(key, None)    # position is being closed
                else:
                    tick = _min_tick(ib, contract)
                    px = _round_to_tick(float(entry["target"]), tick)
                    o = LimitOrder("SELL", held, px)
                    o.orderId = ib.client.getReqId()
                    o.tif = "DAY"
                    trade = ib.placeOrder(contract, o)
                    ib.sleep(2)
                    r["action"] = "rearmed"
                    r["status"] = trade.orderStatus.status
                    r["order_id"] = trade.order.orderId
                    entry["qty"] = held       # keep the registry honest
                    results.append(r)
                    continue
            except Exception as e:
                r["action"] = "error"
                r["detail"] = str(e) or repr(e)
            results.append(r)

        _tp_registry_save(reg)
        return results
    except Exception as e:
        return [{"action": "error", "key": "sweep",
                 "detail": str(e) or repr(e)}]
    finally:
        if ib.isConnected():
            ib.disconnect()


async def morning_tp_sweep() -> list:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _morning_tp_sweep_sync()
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


def _orderbook_check_sync() -> dict:
    """
    Live order-book spot check for the wake-up / details message: quote ONE random
    liquid STOCK with live (type 1) data and report the best bid/ask. A stock book
    avoids the option-chain lookups (slow) and expiry/strike pitfalls; a two-sided
    live quote proves the session holds a live data feed. Error 10197 means a
    competing session took the data share; 354/10089 means not subscribed.
    """
    import random
    from ib_insync import Stock
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid() + 6, timeout=15)
        ib.sleep(1)
        codes: list[int] = []
        ib.errorEvent += lambda rid, code, s, c: codes.append(code)

        sym = random.choice(_BOOK_SAMPLE_TICKERS)
        stk = Stock(sym, "SMART", "USD")
        if not ib.qualifyContracts(stk):
            return {"success": False, "error": f"could not qualify {sym}"}

        ib.reqMarketDataType(1)               # live — the feed we are verifying
        t = ib.reqMktData(stk, "", snapshot=True)
        ib.sleep(8)

        def _clean(v):
            return float(v) if (v is not None and not math.isnan(v) and v > 0) else None

        return {
            "success":   True,
            "desc":      f"{sym} (stock)",
            "bid":       _clean(t.bid),
            "ask":       _clean(t.ask),
            "competing": 10197 in codes,
            # 354/10089 on the snapshot = no live data share for this account —
            # the definitive "not subscribed" signal.
            "no_sub":    any(c in (354, 10089) for c in codes),
        }
    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


async def orderbook_check() -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _orderbook_check_sync()
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


@_serialized
def _bracket_status_sync(parent_id: int, child_id) -> dict:
    """
    Re-check a bracket from a fresh connection, for the follow-up notification.

    A filled parent leaves the open-orders book, so its absence is the signal. The
    child moving PreSubmitted -> Submitted is the confirmation that the take-profit
    is now live at the exchange.
    """
    ib = _AccountIB()
    try:
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)
        ib.reqOpenOrders()          # this client's orders only — never reqAllOpenOrders
        ib.sleep(2)

        open_by_id = {t.order.orderId: t for t in ib.openTrades()}
        parent_open = parent_id in open_by_id
        child_trade = open_by_id.get(child_id) if child_id else None

        # Executions are the ONLY proof of a fill. An order also leaves the open-orders
        # book when it is cancelled or expires at the close, so "not open" on its own
        # must never be read as "filled".
        filled_qty = avg_price = None
        try:
            for f in ib.reqExecutions():
                if f.execution.orderId == parent_id:
                    filled_qty = f.execution.cumQty      # cumulative across partials
                    avg_price = f.execution.avgPrice
        except Exception:
            pass

        status = open_by_id[parent_id].orderStatus.status if parent_open else None
        filled = bool(filled_qty) or status == "Filled"

        return {
            "success":       True,
            "parent_filled": filled,
            # Left the book with no execution => cancelled / expired / rejected.
            "parent_gone":   (not parent_open) and not filled,
            "parent_status": status or ("Filled" if filled else "NotWorking"),
            "filled_qty":    filled_qty,
            "avg_price":     avg_price,
            "exit_status":   child_trade.orderStatus.status if child_trade else None,
        }
    except Exception as e:
        return {"success": False, "error": str(e) or repr(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


async def get_bracket_status(parent_id: int, child_id=None) -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _bracket_status_sync(parent_id, child_id)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


async def place_bracket_order(d: dict) -> dict:
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _place_bracket_sync(d)
        finally:
            loop.close()
    return await asyncio.to_thread(_run)


async def place_order(d: dict) -> dict:
    """
    Async entry point called from the Telegram handler.
    Spawns a thread with its own event loop so ib_insync's blocking
    calls don't interfere with python-telegram-bot's event loop.
    """
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return _place_order_sync(d)
        finally:
            loop.close()

    return await asyncio.to_thread(_run)
