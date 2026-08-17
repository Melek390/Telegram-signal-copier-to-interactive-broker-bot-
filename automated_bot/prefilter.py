"""
Stage 1 — a hardcoded gate that runs before anything else.

Only messages with an attached image can reach Claude. Text-only messages are dropped
here: no API call, no cost, no chance of producing an order.

This is deliberately plain code rather than a prompt instruction. The cheapest and most
predictable place to say "no" is before the network, and a rule this load-bearing should
be readable in five seconds and testable without an API key.
"""

from pathlib import Path

NO_IMAGE = "no_image"


def allows(has_image: bool) -> bool:
    """
    The gate for a message we have not downloaded yet.
    Pass `bool(message.photo)` from the Telegram listener.
    """
    return bool(has_image)


def allows_image_file(image_path) -> bool:
    """The same gate for a message already saved to disk. Empty files do not count."""
    if not image_path:
        return False
    path = Path(image_path)
    return path.is_file() and path.stat().st_size > 0
