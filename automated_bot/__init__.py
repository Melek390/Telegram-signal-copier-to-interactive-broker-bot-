"""Claude-powered signal reading for the automated trading pipeline."""

from .signal_reader import SignalRead, classify, read_signal

__all__ = ["SignalRead", "classify", "read_signal"]
