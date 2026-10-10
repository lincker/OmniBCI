"""Project authoritative runtime inputs and validate only a node's capabilities."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

from omnibci.application.activation_settings import RUNTIME_HOST_SETTING_KEYS
from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.application.plugin_schema import PluginSchemaError, validate_parameters
from omnibci.application.ports.plugin_configuration_catalog import PluginConfigurationCatalog


_ROLE_KINDS = {
    "engine": frozenset({"acquisition", "decoder", "epoch_detector", "filter"}),
    "stimulation": frozenset({"paradigm", "trigger"}),
    "actuation": frozenset({"effector"}),
}
_REQUIRED_SLOTS = {
    "engine": frozenset({"acquisition.source", "processing.decoder", "processing.epoch_detector"}),
    "stimulation": frozenset({"stimulation.paradigm", "stimulation.trigger"}),
    "actuation": frozenset({"actuation.effector"}),
}
_CONTEXT_KEYS = frozenset({
    "ACQUISITION.sample_rate", "ACQUISITION.channel_number", "ACQUISITION.channel_names",
})
_ROLE_SETTING_KEYS = {
    "engine": RUNTIME_HOST_SETTING_KEYS | _CONTEXT_KEYS,
    "stimulation": _CONTEXT_KEYS | {
        "debug", "NEUROENGINE.message_server_ip", "NEUROENGINE.message_server_port",
        "STIMULATION.renderer", "STIMULATION.auto_spawn",
    },
    "actuation": _CONTEXT_KEYS | {
        "debug", "ACTUATION.enabled", "ACTUATION.auto_spawn", "ACTUATION.server_ip",
        "ACTUATION.server_port", "ACTUATION.watchdog_timeout_s", "ACTUATION.max_command_age_s",
    },
}
_ALIASES = {
    "acquisition.source": "ACQUISITION.signal_source",
    "processing.decoder": "ALGORITHM.decoder",
    "processing.epoch_detector": "ALGORITHM.epoch_detector",
    "stimulation.paradigm": "STIMULATION.paradigm",
    "stimulation.trigger": "ACQUISITION.trigger",
    "actuation.effector": "ACTUATION.effector",
}


def _canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise PluginSchemaError("Node configuration must contain finite JSON values") from exc


def _validate_role(role: str) -> None:
    if not isinstance(role, str) or role not in _ROLE_KINDS:
        raise PluginSchemaError("Unsupported node role")


def _validate_settings(role: str, settings: dict) -> None:
    if not isinstance(settings, dict) or set(settings) - _ROLE_SETTING_KEYS[role]:
        raise PluginSchemaError("Node settings contain unsupported fields for this role")
    for key in ("ACQUISITION.sample_rate", "ACQUISITION.channel_number"):
        value = settings.get(key)
        if type(value) is not int or value <= 0:
            raise PluginSchemaError(f"{key} must be a positive integer")
    names = settings.get("ACQUISITION.channel_names", [])
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise PluginSchemaError("ACQUISITION.channel_names must be a list of strings")
    for key, value in settings.items():
        if key in _CONTEXT_KEYS:
            continue
        if key == "SIGNAL.setup":
            from omnibci.domain.montage import SignalSetup
            SignalSetup.from_dict(value)
            continue
        if key == "debug" or key.endswith((".enabled", ".auto_spawn")):
            valid = type(value) is bool
        elif key.endswith(("_ip", ".renderer")):
            valid = isinstance(value, str) and bool(value.strip())
        elif key.endswith("_port"):
            valid = type(value) is int and 1 <= value <= 65535
        elif key == "NEUROENGINE.queue_size":
            valid = type(value) is int and value > 0
        else:
            minimum_inclusive = key in {"ALGORITHM.baseline_duration", "ACTUATION.max_command_age_s"}
            valid = type(value) in (int, float) and (value >= 0 if minimum_inclusive else value > 0)
        if not valid:
            raise PluginSchemaError(f"{key} has an invalid node setting value")
    if role == "actuation" and settings.get("ACTUATION.enabled") is not True:
        raise PluginSchemaError("Actuation is disabled in the accepted configuration")


@dataclass(frozen=True, slots=True, init=False)
class NodeConfiguration:
    """Role projection tied to its authoritative parent revision and run identity.

    The parent revision identifies the full configuration, not this projection.
    Its authority is established by the backend transport and connection handshake.
    """

    role: str
    revision: str
    generation: str | None
    _settings_json: str = field(repr=False)
    _bindings_json: str = field(repr=False)
    format_version: int = 1

    def __init__(self, role: str, revision: str, generation: str | None, settings: dict,
                 bindings: tuple[ProfileBinding, ...], format_version: int = 1):
        _validate_role(role)
        if type(format_version) is not int or format_version != 1:
            raise PluginSchemaError("Unsupported node configuration format_version")
        if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{64}", revision) is None:
            raise PluginSchemaError("Node configuration requires the parent activation revision")
        if generation is not None and (
            not isinstance(generation, str) or not generation or generation != generation.strip()
        ):
            raise PluginSchemaError("Node generation must be non-empty text or null")
        _validate_settings(role, settings)
        if not isinstance(bindings, (tuple, list)) or any(
            not isinstance(binding, ProfileBinding) or binding.kind not in _ROLE_KINDS[role]
            for binding in bindings
        ):
            raise PluginSchemaError("Node bindings contain unsupported capabilities for this role")
        slots = {binding.slot for binding in bindings}
        if len(slots) != len(bindings):
            raise PluginSchemaError("Duplicate node configuration slot")
        missing = _REQUIRED_SLOTS[role] - slots
        if missing:
            raise PluginSchemaError("Missing required node selections: " + ", ".join(sorted(missing)))
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "format_version", format_version)
        object.__setattr__(self, "_settings_json", _canonical(settings))
        object.__setattr__(self, "_bindings_json", _canonical([binding.to_dict() for binding in bindings]))

    @property
    def settings(self) -> dict:
        return json.loads(self._settings_json)

    @property
    def bindings(self) -> tuple[ProfileBinding, ...]:
        return tuple(ProfileBinding.from_dict(item) for item in json.loads(self._bindings_json))

    def to_dict(self) -> dict:
        return {"format_version": self.format_version, "role": self.role, "revision": self.revision,
                "generation": self.generation, "settings": self.settings,
                "bindings": json.loads(self._bindings_json)}

    @classmethod
    def from_dict(cls, data: dict) -> NodeConfiguration:
        fields = {"format_version", "role", "revision", "generation", "settings", "bindings"}
        if not isinstance(data, dict) or set(data) != fields or not isinstance(data["bindings"], list):
            raise PluginSchemaError("Node configuration has missing or unknown fields")
        return cls(**{**data, "bindings": tuple(ProfileBinding.from_dict(item) for item in data["bindings"])})


def project_node_configuration(runtime_snapshot: dict, role: str) -> NodeConfiguration:
    """Project a backend-owned snapshot without looking up any installed packages."""
    _validate_role(role)
    if not isinstance(runtime_snapshot, dict) or not isinstance(runtime_snapshot.get("PLUGINS.bindings"), list):
        raise PluginSchemaError("Node projection requires pinned runtime bindings")
    bindings = tuple(ProfileBinding.from_dict(item) for item in runtime_snapshot["PLUGINS.bindings"])
    if len({binding.slot for binding in bindings}) != len(bindings):
        raise PluginSchemaError("Duplicate activation slot")
    source = next((binding for binding in bindings if binding.slot == "acquisition.source"), None)
    if source is None:
        raise PluginSchemaError("Node projection requires acquisition source context")
    settings = {key: value for key, value in runtime_snapshot.items() if key in _ROLE_SETTING_KEYS[role]}
    for name in ("sample_rate", "channel_number"):
        key = f"ACQUISITION.{name}"
        value = source.parameters.get(name)
        if key in settings and _canonical(settings[key]) != _canonical(value):
            raise PluginSchemaError(f"{key} disagrees with the pinned source")
        settings[key] = value
    return NodeConfiguration(
        role=role, revision=runtime_snapshot.get("CONFIG.revision"),
        generation=runtime_snapshot.get("CONFIG.generation"), settings=settings,
        bindings=tuple(binding for binding in bindings if binding.kind in _ROLE_KINDS[role]),
    )


def materialize_node_configuration(configuration: NodeConfiguration,
                                   catalog: PluginConfigurationCatalog) -> dict:
    """Validate exact local capabilities and derive their runtime aliases locally."""
    result = configuration.settings
    filters = []
    for binding in configuration.bindings:
        definition = catalog.get(binding.kind, binding.selection)
        if definition.kind != binding.kind or definition.selection != binding.selection:
            raise PluginSchemaError(f"{binding.slot}: the exact selected plugin is unavailable")
        if not definition.available:
            raise PluginSchemaError(f"{binding.slot}: unavailable on this platform")
        if definition.schema is None:
            raise PluginSchemaError(f"{binding.slot}: plugin has no configuration schema")
        if definition.configuration_version != binding.configuration_version:
            raise PluginSchemaError(f"{binding.slot}: the profile requires a configuration schema migration")
        parameters = validate_parameters(definition.schema, binding.parameters)
        if _canonical(parameters) != _canonical(binding.parameters):
            raise PluginSchemaError(f"{binding.slot}: snapshot must include resolved defaults")
        if binding.slot in _ALIASES:
            result[_ALIASES[binding.slot]] = definition.plugin_type
        if binding.kind == "acquisition":
            for key in ("sample_rate", "channel_number"):
                if _canonical(parameters.get(key)) != _canonical(result[f"ACQUISITION.{key}"]):
                    raise PluginSchemaError(f"ACQUISITION.{key} disagrees with the pinned source")
            result.update({f"ACQUISITION.{key}": value for key, value in parameters.items()})
            if binding.manual_montage is not None:
                if (binding.manual_montage.source_id != definition.plugin_type
                        or binding.manual_montage.sample_rate != parameters["sample_rate"]
                        or binding.manual_montage.output_row_count != parameters["channel_number"]):
                    raise PluginSchemaError("Manual montage disagrees with the pinned source")
                result["ACQUISITION.manual_montage"] = binding.manual_montage.to_dict()
        elif binding.kind == "filter":
            filters.append(definition.plugin_type)
    if configuration.role == "engine":
        result["ALGORITHM.filters"] = filters
    result["PLUGINS.bindings"] = [binding.to_dict() for binding in configuration.bindings]
    result["CONFIG.revision"] = configuration.revision
    if configuration.generation is not None:
        result["CONFIG.generation"] = configuration.generation
    result["CONFIG.node_role"] = configuration.role
    return result
