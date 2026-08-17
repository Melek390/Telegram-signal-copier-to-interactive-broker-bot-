# Telegram → IBKR Signal Trade Copier

> **Copies trade signals from a Telegram channel straight into Interactive Brokers as live options orders — fully automatically.**

A trade-copier bot that bridges a **Telegram signal channel** and an **Interactive Brokers** account. It watches the channel in real time, reads each signal — Arabic text plus a broker screenshot — and places the order itself: no confirmation step, no manual input. Telegram is used for **monitoring and recovery**, not for trading by hand.

**Highlights**

- 📡 **Live signal listening** — reacts to every new channel message within seconds.
- 🧠 **Two-layer reading** — trading rules are literal keyword checks in *code*; an AI model (Claude Haiku) only reads the contract card image. Deterministic decisions, no prompt drift.
- 🤖 **Fully automated execution** — entry, average-in and emergency exit are placed without human input.
- 🪜 **Price-ladder fills** — buys start at the bid and step **+5¢ every 5 s**; sells start at the ask and step **−5¢** — until filled. No blind market orders.
- 🛡️ **Take-profit discipline** — every entry gets a sell limit at the signal's first target, re-armed automatically each morning after the open.
- 🚨 **Self-monitoring** — alerts for sleep mode, lost gateway and lost market data, each snoozable; daily pre-market status check.
- 🔐 **Secure web login** — switch paper/live and enter broker credentials (and the target sub-account) through a one-time private web form, never through Telegram.
- ♻️ **Self-healing deployment** — watchdogs restart the broker gateway and re-apply the read-only-API fix automatically.

---

## Screenshot

**Account status on demand** — `wake up` starts the gateway and reports real account state.

<img src="assets/account-connected.png" width="430" alt="Account summary — gateway connected, net liquidation and funds">

---

## Table of Contents

1. [How a Signal Becomes an Order](#how-a-signal-becomes-an-order)
2. [Order Execution Rules](#order-execution-rules)
3. [Bot Command Reference](#bot-command-reference)
4. [Buttons the Bot Offers](#buttons-the-bot-offers)
5. [Automatic Monitoring](#automatic-monitoring)
6. [Architecture](#architecture)
7. [Requirements](#requirements)
8. [Configuration (`.env`)](#configuration-env)
9. [Setup](#setup)
10. [Web-Based Login](#web-based-login)
11. [Market Data Behaviour](#market-data-behaviour)
12. [Deployment & Auto-Recovery](#deployment--auto-recovery)
13. [IBKR Gotchas Worth Knowing](#ibkr-gotchas-worth-knowing)
14. [Project Structure](#project-structure)

---

## How a Signal Becomes an Order

```
channel message
   │
   ├─ 1. prefilter (code)      no image → dropped, free, no API call
   ├─ 2. classify (code)       literal Arabic keyword rules → buy / buy_more / exit / ignore
   ├─ 3. read card (Claude)    contract card vs chart + ticker, strike, expiry, price, target
   └─ 4. execute (IBKR)        price ladder + take-profit, then a Telegram report
```

**Classification lives in code, not in the prompt.** The rules are literal substring checks on the message text:

| Text contains | Action |
|---|---|
| `المتوسط` | **average-in** — cancel the take-profit, buy more, re-place one take-profit for the whole position |
| `بسم الله` + `كول`/`بوت` | **buy** — new entry |
| `خفف` **without** `الهدف`/`الاهداف` | **emergency exit** — sell the whole position |
| anything else | ignore |

The model's only job is perception: is the screenshot a **contract card** or a **price chart** (charts are ignored), and what values are printed on it. This split removed a ~7% decision-error rate measured over a 1,525-message archive replay; the final architecture ran 1,140 consecutive messages with **zero** classification errors.

**Late targets:** the channel often edits the targets line in *seconds after* posting. If an entry fills before a target exists, the bot remembers the message and places the take-profit automatically the moment the edit arrives.

---

## Order Execution Rules

**Entry / emergency exit / morning clean-up sell all use the same price ladder:**

- **Buy** — first limit at the **bid**, then **+5¢ every 5 seconds**.
- **Sell** — first limit at the **ask**, then **−5¢ every 5 seconds**.
- Each 5-second cycle is independent: it re-prices, re-sizes to whatever is still unfilled, and cancels the previous rung. A rejection or error in one cycle simply feeds the next.
- The step widens to one tick on coarse-tick contracts (e.g. SPX); sell prices never go below one tick.
- **No automatic market orders.** The ladder runs to a sanity ceiling (~13 min) and then hands the user a button. Market orders only ever exist because a human pressed one.

**Sizing** — `ORDER_BUDGET_USD ÷ (price × 100)`, rounded down. The budget is the only bound on order size.

**Take-profits** — a sell limit at the signal's **first target**, `DAY`, placed once for the total quantity bought.

**Morning re-arm sweep** — take-profits are DAY orders, so they expire at the close. Each weekday at **09:30 ET + `delay`** (adjustable, default 2 min 10 s) the bot walks its own positions and:

| Situation | Action |
|---|---|
| Price **above** yesterday's target | sell the position via the ladder |
| Price below target | re-place yesterday's limit for today |
| A sell already resting (incl. a manual one) | skip |
| Position no longer held | forget it |

Only positions the **bot itself** opened are touched — they are tracked in a local registry. Positions the account holder opened by hand are never swept.

**No live market data** — the bot places **nothing**. It reports that the signal was read correctly, says it cannot price the order, and offers a button. Nothing is cancelled either: on an exit signal without data, the existing take-profit is deliberately left resting so the position stays protected.

**Transient failures** — a connection-class failure (timeout, dropped socket) is retried once after 30 seconds. Deliberate refusals (contract not found, target below entry, over budget) are never retried.

---

## Bot Command Reference

Commands are plain text — no slash needed except `/help` and `/cancel`. Typing any command while a prompt is waiting for input leaves that prompt cleanly.

### Status & monitoring
| Command | Description |
|---|---|
| `status` | Instant state card — mode (live/paper), armed or halted, gateway, account, **size** and **delay** settings |
| `details` | Account summary from the broker (net liq, available funds, cash, position count) **+ live order-book check** |
| `open positions` | List of open option positions (display only) |
| `pending orders` | List of working orders, manual ones flagged (display only) |

### Settings
| Command | Description |
|---|---|
| `size` | Set the dollars spent per signal — reply with an amount, e.g. `5000` |
| `delay` | Set how long after the 09:30 ET open the position re-check runs — reply with `130` or `2:10` |

### Session control
| Command | Description |
|---|---|
| `wake up` | Start the gateway, clear the halt, reclaim market data, then show the account card + order-book status + settings |
| `sleep` | 🛑 Kill switch — halt all trading and stop the gateway |
| `login` | Choose paper/live and open a one-time web form for credentials (and optional account ID) |
| `logout` | Graceful IBKR logout (clean session close) |
| `/help` | Command list |
| `/cancel` | Leave the current prompt |

> The gateway **auto-wakes** when a command needs it — no need to wake it manually first.
> There is **no manual order entry**. Trading happens only from channel signals; the bot's own buttons are the only manual actions.

---

## Buttons the Bot Offers

The bot never asks for confirmation before trading, but it does offer recovery actions on its notifications:

| Button | Appears when | Effect |
|---|---|---|
| **Place at MARKET ⚡** | an entry could not be priced or the ladder ended with a remainder | buys exactly what is missing at market, with the take-profit re-attached for the whole position |
| **Switch to MARKET ⚡** | an exit or sweep sell is unfilled or could not run | sells what is actually held at market (position-checked, so it can never double-sell) |
| **Snooze 15 min / 12 h** | any guard alert | pauses the alert; the 1-minute drumbeat resumes afterwards until the cause is resolved |

Buttons are single-use and expire on bot restart rather than acting on stale context.

---

## Automatic Monitoring

**Guard** — checks every minute, weekdays only (weekends are silent), and alerts once per minute until resolved:

| Condition | First alert |
|---|---|
| Bot asleep | 5 minutes into sleep |
| Awake but **no live market data** (competing session or no subscription) | immediately |
| Awake but **gateway down** (crash or failed wake-up) | after a ~3-minute grace, so normal re-logins don't false-alarm |

Each alert carries snooze buttons; when the cause clears, a single ✅ all-clear is sent and the episode resets. Alert state is persisted, so a bot restart neither re-fires nor forgets a snooze.

**Pre-market check** — every weekday at **09:00 ET** (30 minutes before the open): if the bot is up, it sends account status and the live order-book status; if it's asleep, it sends a reminder to `wake up`.

---

## Architecture

```
Telegram commands ─► python-telegram-bot ─► handlers ─► ibkr/client.py ─► IB Gateway
                                                ▲          (ib_insync, thread-isolated)
Signal channel ─► Telethon listener ─► prefilter ─► classify() ─► Claude reader ─┘
                  (daemon thread)        (code)      (code)        (card only)
```

- **PTB** runs the main event loop, all commands, the guard, the pre-market check and the morning sweep.
- **ib_insync** talks to IB Gateway. Every IBKR call runs in its own short-lived thread with its own event loop, so blocking broker calls never stall PTB. All order-owning calls are serialised behind one re-entrant lock and share one clientId.
- **Telethon** runs in a **separate daemon thread** with an isolated event loop — PTB cancelling its tasks can't kill it (an early bug: `CancelledError` is a `BaseException`, so a `try/except Exception` reconnect loop never caught it and signals were silently missed). Cross-thread work is marshalled back with `run_coroutine_threadsafe`.

> **Python event-loop note:** `bot.py` must call `asyncio.set_event_loop(asyncio.new_event_loop())` as its *first* statement, before any import — `eventkit` (an `ib_insync` dependency) calls `get_event_loop()` at import time and newer Python versions no longer auto-create one.

---

## Requirements

- **Python 3.11+**
- **IB Gateway** (paper or live), reachable on a local socket port
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather)
- **Telegram API credentials** (`api_id` / `api_hash`) from [my.telegram.org](https://my.telegram.org) — for the channel listener
- An **Anthropic API key** — the model reads the signal cards (~$0.003 per signal)
- A **live market-data subscription** on the IBKR user the bot logs in with (US equities + OPRA options). Without it the bot can price nothing and every signal becomes a button.

```bash
pip install -r requirements.txt
```

```
python-telegram-bot==21.11.1
python-dotenv==1.0.1
ib_insync==0.9.86
telethon==1.43.2
anthropic==0.89.0
```

---

## Configuration (`.env`)

Copy `.env.example` to `.env` and fill in your own values. **Never commit `.env`.**

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather |
| `AUTHORIZED_USER_IDS` | yes | Comma-separated Telegram user IDs allowed to use the bot (also the notification recipients) |
| `IBKR_HOST` | yes | Usually `127.0.0.1` |
| `IBKR_PORT` | yes | `4002` = Gateway paper, `4001` = Gateway live |
| `IBKR_CLIENT_ID` | yes | Base client ID (the bot derives others from it) |
| `IBKR_ACCOUNT` | multi-account logins | Sub-account to trade, e.g. `U1234567`. Set from the login page. **Required when a login holds more than one account** — otherwise IBKR routes to its own default account |
| `CLAUDE_API_KEY` | yes | Anthropic API key for the card reader |
| `CLAUDE_MODEL` | optional | Default `claude-haiku-4-5` |
| `TELEGRAM_API_ID` | listener | Telegram `api_id` |
| `API_HASH` | listener | Telegram `api_hash` |
| `SIGNAL_CHANNEL` | listener | Numeric ID of the channel to watch (`-100…`) |
| `AUTOMATED_BOT` | yes | `true` — the Claude signal pipeline |
| `SIGNAL_LISTENER` | yes | `false` — the legacy OCR pipeline (exactly one pipeline may be enabled) |
| `ORDER_BUDGET_USD` | yes | Dollars per signal (also set from Telegram with `size`) |
| `SWEEP_DELAY_SECONDS` | optional | Seconds after the open for the position re-check (also set with `delay`, default `130`) |
| `DATA_PRIORITY` | optional | `true` lets the background watchdog reclaim market data from another session. Default `false` — only an explicit `wake up` reclaims |
| `LOGIN_PORT` | optional | Port for the web login form (default `7823`) |
| `VPS_IP` | optional | Host shown in the login link |

> All IBKR connection variables are read **at call time**, so the login flow can switch paper↔live without restarting the bot.

---

## Setup

1. **Install dependencies** — `pip install -r requirements.txt`.
2. **Create `.env`** — copy `.env.example` and fill in your values.
3. **Configure IB Gateway:** enable *ActiveX and Socket Clients*, **uncheck Read-Only API**, set the socket port to match `IBKR_PORT`.
4. **First-run listener authentication** (one time): Telethon prompts for a phone number and login code, then stores `listener.session`. **Do not delete it** or you'll re-authenticate.
5. **Run:** `python bot.py`
6. **Arm it:** send `wake up` in Telegram.

---

## Web-Based Login

Rather than typing IBKR credentials into Telegram, the `login` command:

1. Asks for **Live** or **Paper**.
2. Starts a **temporary local HTTP server** (default `7823`) protected by a one-time token and sends a link.
3. You submit username, password and — for logins holding several accounts — the **Account ID**, directly to the server.
4. The server updates the gateway config, switches the port, restarts the gateway and replies with the account summary.

Trading stays **halted for the whole switch** and is only re-armed once the new gateway is verified up; a failed login leaves the bot safely halted. A pinned account that doesn't exist under the login is reported immediately, listing the accounts that do.

> IB Gateway / IBC store the password in plain text in their own config regardless of how it's entered — securing host access matters more than the input channel.

---

## Market Data Behaviour

- **One live market-data session per IBKR username.** If the same username is logged in elsewhere (mobile/desktop), the API gets no quotes — error 10197. Running the bot on a **dedicated second username** avoids fighting the account holder's own session.
- **`wake up` always reclaims** the data share (the other session loses quotes). The background watchdog never does, unless `DATA_PRIORITY=true`.
- **Index options (SPX/NDX) must be quoted over `CBOE`.** Routing them through `SMART` triggers competing-session errors even when nothing else is connected; qualification via `SMART` is fine.
- **Both sides of the book are required** before the ladder starts. A one-sided or empty book counts as no data (see the no-data rule above).
- `details` and `wake up` sample a random liquid stock and report whether live data is genuinely flowing, so subscription problems surface before the open, not during a signal.

---

## Deployment & Auto-Recovery

Runs unattended on a Linux VPS alongside IB Gateway (driven headlessly by **IBC**), each component under `tmux`:

| Session | Job |
|---|---|
| `bot` | the bot itself |
| `gatewaywatchdog` | restarts IB Gateway when its port goes down — capped at 5 consecutive failures, then alerts instead of retrying (repeated failed logins can lock an IBKR account) |
| `fixwatchdog` | re-applies the read-only-API fix to every new gateway process, on either port |

**IBC config essentials:** `AcceptNonBrokerageAccountWarning=yes`, `OverrideReadOnlyApi=yes`; credentials and `TradingMode` are rewritten by the `login` flow.

**Live accounts require 2FA.** IBKR re-authenticates weekly (typically Sunday): the gateway login pauses on a challenge until it is approved in the IBKR Mobile app. Push approval works headlessly; SMS codes do not.

**Runtime state files** (git-ignored, all survive restarts): `.trading_halted` (kill switch), `.tp_registry.json` (bot-managed take-profits), `.guard_state.json` (alert episodes), `listener.session` (Telethon).

**clientId allocation** (base `IBKR_CLIENT_ID`, shown for base `1`):

| clientId | Purpose |
|---|---|
| 1 | all order placement / cancel / modify (must own the orders it touches) |
| +1 … +3 | position and account reads |
| +5 | market-data requests |
| +6 | order-book health check |

---

## IBKR Gotchas Worth Knowing

Learned the hard way — worth keeping in mind when extending the bot:

- **Never call `reqAllOpenOrders()` on a short-lived connection that places orders.** It *rebinds* other clients' orders to that connection, and IBKR cancels them when it drops. It is safe for read-only listing (that's how manually placed orders become visible).
- **Cancel/modify must come from the clientId that placed the order** — cross-clientId operations are silently ignored. Manual TWS orders report `orderId 0` and can be *seen* but never cancelled through the API.
- **Rejection reasons arrive via `ib.errorEvent`, not the trade log.** Market-data-farm notices and the TIF=DAY notice (10349) are filtered as noise; error 202 is also IBKR's receipt for the bot's *own* deliberate cancels.
- **IBKR rejects limit prices too far from the market** ("more aggressive than …"). A stale or one-sided quote can produce exactly that — which is why the ladder re-prices every cycle and no order is ever priced from a single snapshot.
- **A quote can be silently stale.** A snapshot may return prices from many minutes earlier with no error at all — never assume one quote is the market.
- **You cannot have orders on both sides of the same US option contract.** A repeat buy on a contract with a resting take-profit is rejected (error 201) — the bot detects this and routes the signal through the average-in flow instead.
- **Adjusted option chains** (e.g. `2AMZN` after a corporate action) also list on `SMART` and reject API orders as "Flex options" — always qualify with `tradingClass` equal to the symbol first.
- **TIF is never left unset.** Take-profits are `DAY` and re-armed each morning; entries and exits are `DAY` so a stale order dies at the close instead of firing into a gap.

---

## Project Structure

```
bot.py                       entry point — handlers, background loops, listener startup
tg/
  handlers.py                commands, login flow, guard, pre-market check, morning sweep
  automated_listener.py      Telethon daemon-thread listener → signal execution
  messages.py                all message strings + formatters
  keyboards.py               inline keyboard builders
  callbacks.py               callback_data constants
  signal_listener.py         legacy OCR listener (disabled)
ibkr/
  client.py                  ib_insync integration — ladder engine, entry/exit/average-in,
                             morning sweep, take-profit registry, account pinning
automated_bot/
  signal_reader.py           classify() rules in code + Claude card reader
  prefilter.py               stage-1 gate (image required)
  config.py                  model settings
  pipeline.py, cost_report.py, compare_models.py, check_classification.py   evaluation tools
data_probe.py                market-data health probe used by the watchdog
run_simulation.py            full-archive replay harness against a paper account
requirements.txt
.env.example                 template — copy to .env and fill in
```

---

## License

Private project. Not for redistribution.
