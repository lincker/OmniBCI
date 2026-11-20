"""Construct the real processing state machine with its input already attached."""

from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor
import numpy as np

from omnibci.domain.ports.algorithm import EpochBoundary
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.sdk import PluginContext


def marker_boundaries(raw_data):
    """Test protocol: positive codes below 255 start; 255 ends."""
    events = raw_data[-1]
    return tuple(EpochBoundary(offset=int(index), kind="end" if events[index] == 255 else "start",
                               event_id=None if events[index] == 255 else int(events[index]))
                 for index in np.flatnonzero((events > 0) & (events <= 255)))


def prepared_context(sample_rate):
    return PluginContext(input_signal=SignalInfo(sample_rate=sample_rate, channels=(
        ChannelDescriptor(0, "C3", channel_id="c3", signal_type="eeg"),
        ChannelDescriptor(1, "C4", channel_id="c4", signal_type="eeg"),
    )))


def make_processor(*args, **kwargs):
    processor = SignalProcessor(*args, **kwargs)
    processor._input_attached = True
    return processor
