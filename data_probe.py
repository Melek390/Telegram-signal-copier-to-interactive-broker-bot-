#!/usr/bin/env python3
"""
Market-data health probe. Deployed to /root/data_probe.py on the VPS.

Exit codes:
    0  healthy — session alive and market data flowing (or benignly absent)
    1  UNHEALTHY — a competing session holds our market data (10197), or the
       gateway is a zombie (API port open but no response from IBKR's servers)

The paper account borrows market data from the client's live username. When the
client logs in on phone/web, IBKR hands the data share to that session and every
quote here dies with error 10197. Re-logging the gateway in seizes the share back
and kicks the other session's data — the bot has priority while it is awake.

Deliberately conservative: a nan quote WITHOUT 10197 (closed market, missing
subscription) is healthy. Only the definite competing-session signal, a dead
server link, or an unresponsive API triggers a restart.
"""

import re
import sys

from ib_insync import IB, Option, Stock


def main() -> int:
    port = 4002
    try:
        m = re.search(r"^IBKR_PORT=(\d+)", open("/root/bot/.env").read(), re.M)
        if m:
            port = int(m.group(1))
    except Exception:
        pass

    codes = []
    ib = IB()
    ib.errorEvent += lambda reqId, code, msg, c: codes.append(code)

    try:
        ib.connect("127.0.0.1", port, clientId=93, timeout=15)
    except Exception as e:
        # Port open but the handshake never completes = gateway lost its IBKR
        # session (the JVM keeps the socket open while logged out).
        print(f"probe: connect failed ({e})")
        return 1

    try:
        try:
            ib.reqCurrentTime()          # round-trip proves the session is alive
        except Exception:
            print("probe: no server response")
            return 1

        # Probe an OPTION, not the stock: the paper account borrows OPRA options
        # data from the live user but has no stock subscription, so a stock quote
        # returns 10089 (benign) even while a competing session holds the options
        # data. Only an option request surfaces the 10197 we are looking for.
        spy = Stock("SPY", "SMART", "USD")
        if not ib.qualifyContracts(spy):
            if 10197 in codes or 1100 in codes:
                print("probe: competing/disconnected during qualify")
                return 1
            print("probe: qualify failed (benign)")
            return 0

        try:
            params = ib.reqSecDefOptParams("SPY", "", "STK", spy.conId)
            p = next((x for x in params if x.exchange == "SMART"), params[0])
            expiry = sorted(p.expirations)[0]
            # The params strike list is the union across expiries; ask for the
            # real chain of THIS expiry and take its middle strike.
            cds = ib.reqContractDetails(
                Option("SPY", expiry, 0, "C", exchange="SMART", currency="USD"))
            strikes = sorted(c.contract.strike for c in cds)
            if not strikes:
                raise ValueError("empty chain")
            opt = cds[0].contract
            mid = strikes[len(strikes) // 2]
            opt = next(c.contract for c in cds if c.contract.strike == mid)
        except Exception:
            if 10197 in codes or 1100 in codes:
                print("probe: competing/disconnected during chain lookup")
                return 1
            print("probe: chain lookup failed (benign)")
            return 0

        ib.reqMarketDataType(1)          # live — the share we are protecting
        ib.reqMktData(opt, "", snapshot=True)
        ib.sleep(8)

        if 10197 in codes:
            print("probe: 10197 — competing live session holds market data")
            return 1
        if 1100 in codes and 1102 not in codes:   # lost and not restored
            print("probe: 1100 — connectivity lost")
            return 1
        print("probe: healthy")
        return 0
    finally:
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    sys.exit(main())
