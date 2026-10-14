"""Compatibility exports for source plugins using the previous signal type name."""
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo


AcquisitionDescriptor = SignalInfo

__all__ = ["AcquisitionDescriptor", "ChannelDescriptor"]
