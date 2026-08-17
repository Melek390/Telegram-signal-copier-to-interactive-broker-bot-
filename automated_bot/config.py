"""Settings for the automated signal pipeline. All read from .env at call time."""

import os


def api_key() -> str:
    # The SDK reads ANTHROPIC_API_KEY on its own; this project's .env uses CLAUDE_API_KEY.
    return os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")


def model() -> str:
    # Haiku 4.5 scored 11/11 on the labelled edge-case set (compare_models.py) at ~2.4s,
    # matching Sonnet 5 and Opus 5 on accuracy while being the fastest and cheapest.
    # Re-run the comparison before changing this.
    return os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")


def effort() -> str:
    """low | medium | high | xhigh | max. Worth sweeping — low/medium are strong on Opus 5."""
    return os.getenv("CLAUDE_EFFORT", "high")


def max_tokens() -> int:
    # Thinking is on by default on Opus 5 and shares this budget with the JSON output,
    # so leave headroom or the response truncates mid-answer.
    return int(os.getenv("CLAUDE_MAX_TOKENS", "8000"))
