"""Resolve and instantiate selected capabilities from a process's pinned configuration."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Generic, Mapping, Type, TypeVar

from omnibci.domain.signal_info import SignalInfo
from omnibci.infrastructure.plugin_management import preparation
from omnibci.infrastructure.plugin_management.descriptors import PluginDescriptor
from omnibci.infrastructure.plugin_management.loader import PluginLoader, PluginLoadError

T = TypeVar("T")
_PROCESSING_KINDS = frozenset({"filter", "decoder", "epoch_detector"})


@dataclass(frozen=True, slots=True)
class LoadedPlugin(Generic[T]):
    """An instantiated plugin paired with its verified descriptor and payload schemas."""

    instance: T
    descriptor: PluginDescriptor
    payload_schemas: Mapping[str, Mapping[str, Any]]


def resolve_configured_plugin(snapshot: dict, slot: str, kind: str) -> tuple[PluginDescriptor, dict]:
    """Consume the prepared local artifact without resolving its selection again."""
    bindings = preparation.configured_bindings(snapshot, slot=slot)
    binding = next((binding for binding in bindings if binding.slot == slot), None)
    if binding is None or binding.kind != kind:
        raise PluginLoadError(f"No {kind} selected for {slot}")
    artifacts = snapshot.get("PLUGINS.artifacts")
    if not isinstance(artifacts, dict) or slot not in artifacts:
        raise PluginLoadError(f"{slot}: runtime configuration requires a prepared artifact")
    descriptor = preparation.read_prepared_plugin(binding, artifacts[slot])
    return descriptor, deepcopy(binding.parameters)


def load_configured_plugin_bundle(
    snapshot: dict,
    slot: str,
    kind: str,
    expected_base: Type[T],
    *,
    input_signal: SignalInfo | None = None,
    loader=PluginLoader,
) -> LoadedPlugin[T]:
    """Resolve, validate, and instantiate a configured plugin with its payload schemas."""
    if kind in _PROCESSING_KINDS and not isinstance(input_signal, SignalInfo):
        raise PluginLoadError(f"{slot}: processing requires prepared input_signal")
    descriptor, parameters = resolve_configured_plugin(snapshot, slot, kind)
    if kind == "decoder" and descriptor.channel_requirements is not None:
        assert input_signal is not None
        try:
            descriptor.channel_requirements.validate_channels(input_signal.channels)
        except ValueError as exc:
            raise PluginLoadError(f"{slot}: {exc}") from exc
    instance = loader.instantiate(
        descriptor,
        expected_base,
        parameters=parameters,
        **({"input_signal": input_signal} if kind in _PROCESSING_KINDS else {}),
    )
    return LoadedPlugin(
        instance=instance,
        descriptor=descriptor,
        payload_schemas=deepcopy(getattr(descriptor, "payload_schemas", None) or {}),
    )


def load_configured_plugin(
    snapshot: dict,
    slot: str,
    kind: str,
    expected_base: Type[T],
    *,
    input_signal: SignalInfo | None = None,
    loader=PluginLoader,
) -> T:
    """Use exact accepted selections; never substitute aliases or versions."""
    return load_configured_plugin_bundle(
        snapshot,
        slot,
        kind,
        expected_base,
        input_signal=input_signal,
        loader=loader,
    ).instance
