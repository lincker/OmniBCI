"""Resolve only selected capabilities from a process's pinned configuration."""
from copy import deepcopy

from omnibci.infrastructure.plugins.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugins import preparation
from omnibci.domain.signal_info import SignalInfo


def resolve_configured_plugin(snapshot: dict, slot: str, kind: str):
    """Consume the prepared local artifact without resolving its selection again."""
    bindings = preparation.configured_bindings(snapshot, slot=slot)
    binding = next((binding for binding in bindings if binding.slot == slot), None)
    if binding is None or binding.kind != kind:
        raise PluginLoadError(f"No {kind} selected for {slot}")
    artifacts = snapshot.get('PLUGINS.artifacts')
    if not isinstance(artifacts, dict) or slot not in artifacts:
        raise PluginLoadError(f'{slot}: runtime configuration requires a prepared artifact')
    descriptor = preparation.read_prepared_plugin(binding, artifacts[slot])
    return descriptor, deepcopy(binding.parameters)


def load_configured_plugin(snapshot: dict, slot: str, kind: str, expected_base,
                           *, input_signal: SignalInfo | None = None,
                           loader=PluginLoader):
    """Use exact accepted selections; never substitute aliases or versions."""
    if kind in {"filter", "decoder", "epoch_detector"} and not isinstance(input_signal, SignalInfo):
        raise PluginLoadError(f"{slot}: processing requires prepared input_signal")
    descriptor, parameters = resolve_configured_plugin(snapshot, slot, kind)
    if descriptor.channel_requirements is not None:
        try:
            descriptor.channel_requirements.validate_channels(input_signal.channels)
        except ValueError as exc:
            raise PluginLoadError(f"{slot}: {exc}") from exc
    return loader.instantiate(
        descriptor, expected_base, parameters=parameters,
        **({"input_signal": input_signal} if kind in {"filter", "decoder", "epoch_detector"} else {}),
    )
