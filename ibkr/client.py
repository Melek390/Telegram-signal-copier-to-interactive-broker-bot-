import os
import math
import asyncio

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"), override=True)

from ib_insync import IB, Option, MarketOrder, LimitOrder

_RIGHT = {"CALL": "C", "PUT": "P"}


def _host() -> str:
    return os.getenv("IBKR_HOST", "127.0.0.1")

def _port() -> int:
    return int(os.getenv("IBKR_PORT", "4002"))

def _cid() -> int:
    return int(os.getenv("IBKR_CLIENT_ID", "1"))


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


def _place_order_sync(d: dict) -> dict:
    """
    Blocking IBKR call. Runs in its own thread + event loop so it never
    blocks the Telegram bot's async event loop.
    """
    ib = IB()
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
        return {"success": False, "error": str(e)}

    finally:
        if ib.isConnected():
            ib.disconnect()


def _get_position_sync(d: dict) -> int:
    """Returns current position size for the contract (0 if not held)."""
    ib = IB()
    try:
        ib.connect(_host(), _port(), clientId=_cid() + 1, timeout=10)
        ib.sleep(1)
        target_right  = _RIGHT[d["option_type"].upper()]
        target_expiry = d["expiry"].replace("-", "")
        for pos in ib.positions():
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


def _get_account_summary_sync() -> dict:
    ib = IB()
    try:
        ib.connect(_host(), _port(), clientId=_cid() + 2, timeout=10)
        ib.sleep(1)

        vals = {v.tag: v.value for v in ib.accountValues() if v.currency in ("USD", "")}
        positions = ib.positions()

        return {
            "success":      True,
            "account":      vals.get("AccountCode", "—"),
            "net_liq":      float(vals.get("NetLiquidation", 0)),
            "avail_funds":  float(vals.get("AvailableFunds", 0)),
            "cash":         float(vals.get("TotalCashValue", 0)),
            "open_pos":     len(positions),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


def _get_open_positions_sync() -> list:
    ib = IB()
    try:
        ib.connect(_host(), _port(), clientId=_cid() + 3, timeout=10)
        ib.sleep(1)
        result = []
        for pos in ib.positions():
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


def _get_pending_orders_sync() -> list:
    ib = IB()
    try:
        # Connect as the SAME clientId that placed the orders and use reqOpenOrders()
        # (this client's orders only). reqAllOpenOrders() adopts orders owned by other
        # clients into this short-lived session — they get cancelled on disconnect.
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)
        ib.reqOpenOrders()
        ib.sleep(2)
        result = []
        for trade in ib.openTrades():
            o = trade.order
            c = trade.contract
            if c.secType != "OPT":
                continue
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
            })
        return result
    except Exception:
        return []
    finally:
        if ib.isConnected():
            ib.disconnect()


def _cancel_order_sync(order_id: int) -> dict:
    ib = IB()
    try:
        ib.connect(_host(), _port(), clientId=_cid(), timeout=10)
        # Direct protocol call — no reqAllOpenOrders (that rebinds orders and
        # causes them to be cancelled when this short-lived session disconnects)
        ib.client.cancelOrder(order_id, "")
        ib.sleep(2)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if ib.isConnected():
            ib.disconnect()


def _modify_order_sync(order_id: int, new_price, order_info: dict) -> dict:
    """
    Cancel the existing order then place a replacement with the new price.
    order_info must contain: action, ticker, option_type, strike, expiry, qty
    Avoids reqAllOpenOrders so surviving orders are not rebound to this session.
    """
    ib = IB()
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
        return {"success": False, "error": str(e)}
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
    ib = IB()
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
        return {"success": False, "error": str(e)}
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
