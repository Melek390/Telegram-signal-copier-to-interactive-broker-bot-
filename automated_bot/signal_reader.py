"""
Reads one pre-filtered signal message (screenshot + Arabic text) and returns a
structured result.

Pre-filtering happens before this module is called: only messages that HAVE an image
reach here. Everything else is dropped by the listener.

Design rule — the model perceives, the code decides. Classification (buy / buy_more /
exit / ignore) is a set of literal substring checks on the message text, so it runs
HERE, in `classify()`, deterministically. Claude is only asked what a model is needed
for: is the screenshot a contract card or a chart, and what values are on it. A
message the text rules classify as "ignore" never reaches the API at all.

This replaced prompt-side classification on 2026-08-04 after the archive simulation
showed ~8% semantic drift (false exits on winners, missed تخفف/نخفف exits, buys on
دخول-style entries outside the spec).
"""

import base64
import json
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, ConfigDict, Field

from . import config, prefilter

Action = Literal["buy", "buy_more", "exit", "ignore"]
# "no_image" and "api_error" are set locally; "not_a_signal" and "target_reached"
# come from the text rules; "chart_image" and "unreadable" from the card reader.
Reason = Literal["chart_image", "not_a_signal", "unreadable",
                 "target_reached", "api_error", "no_image"]


def classify(text: str) -> tuple[Action, Optional[Reason]]:
    """
    The client's rules, as literal substring checks. Order matters:

    1. المتوسط        -> buy_more. Averaging messages almost always ALSO contain the
                         بسم الله + كول buy format, so this must be checked first.
    2. بسم الله + كول/بوت -> buy. Entries phrased دخول/دخلت (the channel's high-risk
                         "hit and run" format) do NOT count — owner's decision
                         2026-08-04: keep the strict rule.
    3. خفف            -> exit, UNLESS الهدف/الاهداف also appears (then it is a target
                         announcement our resting sell limit already handled). Substring
                         match on purpose: نخفف and تخفف are the same instruction.
                         خفف with no الهدف is the emergency exit — client's rule, final.
    4. anything else  -> ignore.
    """
    t = text or ""
    if "المتوسط" in t:
        return "buy_more", None
    if "بسم الله" in t and ("كول" in t or "بوت" in t):
        return "buy", None
    if "خفف" in t:
        if "الهدف" in t or "الاهداف" in t:
            return "ignore", "target_reached"
        return "exit", None
    return "ignore", "not_a_signal"


class SignalRead(BaseModel):
    """The combined result: code's classification + Claude's extraction."""

    # extra="forbid" makes Pydantic emit additionalProperties:false, which structured
    # outputs requires. No field has a default, so all land in the schema's `required`.
    model_config = ConfigDict(extra="forbid")

    action: Action
    reason: Optional[Reason]
    ticker: Optional[str]
    right: Optional[Literal["Call", "Put"]]
    strike: Optional[float]
    expiry: Optional[str]
    price_bid: Optional[float]
    price_ask: Optional[float]
    price_last: Optional[float]
    first_target: Optional[float]
    confidence: Literal["high", "low"]
    note: Optional[str]

    @property
    def is_actionable(self) -> bool:
        return self.action in ("buy", "buy_more", "exit") and self.confidence == "high"


class CardRead(BaseModel):
    """What Claude extracts from one screenshot. Every field always present; unknown = None."""

    model_config = ConfigDict(extra="forbid")

    screenshot: Literal["contract_card", "price_chart"]
    readable: bool
    ticker: Optional[str]
    right: Optional[Literal["Call", "Put"]]
    strike: Optional[float]
    # The description rides along in the JSON schema, so the model sees the format too.
    expiry: Optional[str] = Field(
        description="Expiry as YYYY-MM-DD, converted from the card (AUG 10 '26 -> 2026-08-10)"
    )
    price_bid: Optional[float]
    price_ask: Optional[float]
    price_last: Optional[float]
    first_target: Optional[float]
    confidence: Literal["high", "low"]
    note: Optional[str]


SYSTEM_PROMPT = """\
You read screenshots from an Arabic Telegram options channel. Each input is ONE
message: a screenshot plus the message text. You return JSON only.

You do NOT classify the message or decide what to do about it — code does that from
the text separately. Your only job is to say what the screenshot is and extract the
values from it.

The screenshot is either:
  (a) a CONTRACT CARD — ticker, price, then expiry + strike + Call/Put, e.g.
        TSLA        2.43
        AUG 10 '26 330 Call
  (b) a PRICE CHART — axes, a plotted line, no strike and no Call/Put.

Set screenshot to "contract_card" or "price_chart". For a chart: every extracted
field is null, readable is true, confidence "high" — extraction does not apply, and
recognising a chart confidently is a complete answer.

For a card, extract:

CONTRACT DETAILS COME FROM THE SCREENSHOT ONLY — ticker, strike, expiry, Call/Put.
The text sometimes states a different strike. The screenshot is always correct. Never
take contract details from the text and never comment on the difference.

EXPIRY MUST BE CONVERTED to YYYY-MM-DD. The card writes it as "AUG 10 '26" — return
"2026-08-10". Never return the card's own wording. "'26" means the year 2026.

FIRST TARGET COMES FROM THE TEXT ONLY. It is on the line starting الاهداف or الهدف —
take the FIRST number in that list. It never appears in the screenshot. No target
line in the text means first_target is null (that is not illegibility).

READING THE CARD — the renderer is imperfect. Correct these:
  - a decimal point rendered as a colon: "2:30" is 2.30
  - a missing space before the year: "JUL 31'26" is JUL 31 '26
  - a comma decimal: "1,96" is 1.96
  - stray single letters beside the price ("e", "i", "LAS") are artifacts — ignore them
  - TWO prices ("0.87 1.00") are bid and ask: price_bid first, price_ask second,
    price_last null. ONE price goes in price_last, bid and ask null.

readable is false when any card value — ticker, strike, expiry, Call/Put, price — is
present but not clearly legible. NEVER GUESS: return null for an illegible value and
set readable false. A missed trade is acceptable; a wrong trade is not.

confidence is "high" only when every value you return is clearly legible. Otherwise
"low". Return JSON only, no prose."""


def _ignored(reason: Reason, note: str,
             confidence: Literal["high", "low"] = "low") -> SignalRead:
    """A no-op result. Anything not read confidently, or ruled out, becomes this."""
    return SignalRead(
        action="ignore", reason=reason, ticker=None, right=None, strike=None,
        expiry=None, price_bid=None, price_ask=None, price_last=None,
        first_target=None, confidence=confidence, note=note,
    )


def _media_type(path: Path) -> str:
    return {"png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(
        path.suffix.lower().lstrip("."), "image/jpeg"
    )


def _supports_effort(model_id: str) -> bool:
    """Haiku 4.5 rejects output_config.effort; the Opus and Sonnet lines accept it."""
    return "haiku" not in model_id.lower()


def read_signal(
    image_path: str | Path,
    message_text: str,
    today: str = "",
    model_id: str = "",
    on_usage=None,
) -> SignalRead:
    """
    Classify (code) then extract (Claude). Never raises — on any failure returns
    action="ignore" so a bad read can only cost us a trade, never cause one.

    `today` (YYYY-MM-DD) anchors the year on expiries written as "AUG 10 '26".
    `model_id` overrides the configured model (used by the model comparison script).
    `on_usage` is called with the response's token usage, for cost measurement.
    """
    # Stage 1 gate — no image, no signal. Nothing below this line runs without one.
    if not prefilter.allows_image_file(image_path):
        return _ignored(prefilter.NO_IMAGE, f"no usable image: {image_path}")
    image_path = Path(image_path)

    # Stage 2 — deterministic classification from the text. An ignore here never
    # reaches the API: no cost, no model in the loop, same answer every time.
    action, why = classify(message_text)
    if action == "ignore":
        return _ignored(why, "text rule", confidence="high")

    # Stage 3 — Claude reads the screenshot: card-vs-chart gate + extraction.
    key = config.api_key()
    if not key:
        return _ignored("api_error", "CLAUDE_API_KEY is not set")

    try:
        image_b64 = base64.standard_b64encode(image_path.read_bytes()).decode()
    except OSError as e:
        return _ignored("unreadable", f"could not read image: {e}")

    client = anthropic.Anthropic(api_key=key)
    model_id = model_id or config.model()

    output_config = {
        "format": {"type": "json_schema", "schema": CardRead.model_json_schema()},
    }
    if _supports_effort(model_id):
        output_config["effort"] = config.effort()

    try:
        response = client.messages.create(
            model=model_id,
            max_tokens=config.max_tokens(),
            # Cached: signals sometimes arrive a minute apart (seen in the real channel),
            # and a hit costs 10% of a fresh read. The write premium is ~1.25x of <1k tokens.
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            output_config=output_config,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": _media_type(image_path),
                        "data": image_b64,
                    }},
                    {"type": "text", "text":
                        f"Message text:\n<<<\n{message_text}\n>>>"
                        + (f"\nToday's date: {today}" if today else "")},
                ],
            }],
        )
    except anthropic.RateLimitError:
        return _ignored("api_error", "rate limited")
    except anthropic.APIConnectionError:
        return _ignored("api_error", "could not reach the Claude API")
    except anthropic.APIStatusError as e:
        return _ignored("api_error", f"API error {e.status_code}: {e.message}")

    if on_usage:
        on_usage(response.usage)   # report tokens even if the parse below fails

    if response.stop_reason == "refusal":
        return _ignored("api_error", "model declined the request")

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        return _ignored("api_error", f"empty response (stop_reason={response.stop_reason})")

    try:
        card = CardRead.model_validate_json(text)
    except (ValueError, json.JSONDecodeError) as e:
        return _ignored("api_error", f"response did not match the schema: {e}")

    # Rule 2 preserved: a buy/exit-worded message whose picture is a chart is ignored.
    if card.screenshot == "price_chart":
        return _ignored("chart_image", card.note or "screenshot is a chart")
    if not card.readable:
        return _ignored("unreadable", card.note or "card value not legible")

    return SignalRead(
        action=action, reason=None, ticker=card.ticker, right=card.right,
        strike=card.strike, expiry=card.expiry, price_bid=card.price_bid,
        price_ask=card.price_ask, price_last=card.price_last,
        first_target=card.first_target, confidence=card.confidence, note=card.note,
    )
