"""Canonical public contracts for OmniBCI Plugin API v1.

Constructors receive keyword-only parameters and their own resource_root.
Processing roles also require the actual mapped input_signal. Runtime capabilities
arrive through role lifecycle methods; host identities and services stay private.
"""
from omnibci.domain.ports.acquisition import (
    SignalSource, SourceCheckpoint, SourceObservation, TriggerEmitter, TriggerOutput,
)
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter, EpochBoundary, Prediction
from omnibci.domain.ports.effector import Effector
from omnibci.domain.ports.paradigm import Paradigm
from omnibci.domain.ports.renderer import Color, FrameTiming, Rect, RenderSurface, TextureHandle
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo

__all__ = [
    "Color", "Decoder", "Effector", "EpochDetector", "Filter", "EpochBoundary", "FrameTiming",
    "Paradigm", "Prediction", "Rect", "RenderSurface", "SignalSource", "SignalInfo",
    "ChannelDescriptor", "SourceObservation", "SourceCheckpoint", "TextureHandle",
    "TriggerEmitter", "TriggerOutput",
]
