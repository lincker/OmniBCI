"""Stable public contracts for OmniBCI plugins.

New plugins accept keyword constructor inputs ``parameters: PluginParameters``
and ``context: PluginContext``. The host validates and copies the JSON-compatible
parameter mapping before construction. Plugins should keep runtime state separate
from those inputs. Existing port constructors remain supported by legacy hosts.

The context contains host-provided metadata only; devices, renderers and services
are supplied through their existing lifecycle contracts, never serialized here.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

from omnibci.domain.ports.acquisition import SignalSource, TriggerEmitter
from omnibci.domain.ports.algorithm import (
    Decoder,
    EpochDetector,
    Filter,
    FilterContext,
    Prediction,
)
from omnibci.domain.ports.effector import Effector
from omnibci.domain.ports.paradigm import Paradigm
from omnibci.domain.ports.renderer import Color, Rect, Renderer, TextureHandle
from omnibci.domain.signal import SignalChunk
from omnibci.domain.signal_info import SignalInfo


PluginParameters = Mapping[str, Any]

# Preserve the public import used by plugins built before the SignalInfo rename.
AcquisitionDescriptor = SignalInfo


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Immutable metadata that can cross a multiprocessing spawn boundary.

    ``sample_rate`` describes the acquired signal and can be absent before a
    source connects. ``config_revision`` identifies the host's applied snapshot.
    ``resource_root`` is the trusted asset directory of the selected package,
    supplied as an absolute string path by the host; no filesystem access occurs
    while creating this context.

    ``acquired_signal`` describes normalized acquisition rows. ``input_signal``
    describes the channel IDs, names, types and units supplied to this processing
    plugin after channel selection and ordering. Their ``channels`` exclude the
    event/trigger row. Both are absent in the legacy path.

    ``channel_names`` is deprecated and retained for existing plugins. Prepared
    contexts populate it from ``acquired_signal.channels`` for compatibility;
    it never describes mapped input rows. New plugins should read channel
    records from ``acquired_signal`` or ``input_signal`` as appropriate.
    """

    sample_rate: float | None = None
    channel_names: tuple[str, ...] = ()
    config_revision: str | None = None
    resource_root: str | None = None
    acquired_signal: SignalInfo | None = None
    input_signal: SignalInfo | None = None

    def __post_init__(self) -> None:
        for field in ("acquired_signal", "input_signal"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, SignalInfo):
                raise ValueError(f"{field} must be SignalInfo or None")
        if self.sample_rate is not None and (
            isinstance(self.sample_rate, bool)
            or not isinstance(self.sample_rate, (int, float))
            or not isfinite(self.sample_rate)
            or self.sample_rate <= 0
        ):
            raise ValueError("sample_rate must be a positive finite number")
        if isinstance(self.channel_names, (str, bytes)):
            raise ValueError("channel_names must be a sequence of strings")
        try:
            names = tuple(self.channel_names)
        except TypeError as exc:
            raise ValueError("channel_names must be a sequence of strings") from exc
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("channel_names must contain non-empty strings")
        object.__setattr__(self, "channel_names", names)
        for field in ("config_revision", "resource_root"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field} must be a non-empty string or None")


__all__ = [
    "Color", "Decoder", "Effector", "EpochDetector", "Filter", "FilterContext",
    "Paradigm", "PluginContext", "PluginParameters", "Prediction", "Rect",
    "Renderer", "SignalChunk", "SignalSource", "SignalInfo", "AcquisitionDescriptor", "TextureHandle", "TriggerEmitter",
]
