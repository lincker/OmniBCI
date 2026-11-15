"""Construct the real processing state machine with its input already attached."""

from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor


def make_processor(*args, **kwargs):
    processor = SignalProcessor(*args, **kwargs)
    processor._input_attached = True
    return processor
