"""Portable plugin profiles and pinned activation snapshots, without I/O."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import UUID

from omnibci.application.activation_settings import MODULES, SECTION_SETTING_KEYS
from omnibci.domain.channel_mapping import ProcessingInput


class PluginProfileValidationError(ValueError):
    """A profile, binding or stored activation is invalid."""


class PluginProfileNotFoundError(KeyError):
    """The requested profile does not exist."""


class ProfileRevisionConflict(RuntimeError):
    """The stored document changed after the caller read it."""


class PluginProfileStorageError(OSError):
    """Local plugin configuration could not be read or written."""


PLUGIN_KINDS = frozenset({
    "acquisition", "decoder", "epoch_detector", "filter", "paradigm", "trigger", "effector",
})
MODULE_KINDS = {
    "acquisition": frozenset({"acquisition"}),
    "processing": frozenset({"decoder", "epoch_detector", "filter"}),
    "stimulation": frozenset({"paradigm", "trigger"}),
    "actuation": frozenset({"effector"}),
}
_SLOTS = {
    "acquisition.source": "acquisition",
    "processing.decoder": "decoder",
    "processing.epoch_detector": "epoch_detector",
    "stimulation.paradigm": "paradigm",
    "stimulation.trigger": "trigger",
    "actuation.effector": "effector",
}


def validate_profile_id(value: str) -> None:
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise PluginProfileValidationError("profile_id must be a canonical UUID")


def plugin_kind_for_slot(slot: str) -> str:
    if not isinstance(slot, str):
        raise PluginProfileValidationError("slot must be a string")
    if slot.startswith("processing.filter."):
        validate_profile_id(slot.removeprefix("processing.filter."))
        return "filter"
    if slot not in _SLOTS:
        raise PluginProfileValidationError("Unsupported configuration slot")
    return _SLOTS[slot]


def _positive_integer(value: int, name: str, *, minimum: int = 1) -> None:
    if type(value) is not int or value < minimum:
        raise PluginProfileValidationError(f"{name} must be an integer >= {minimum}")


def _copy_data(value: Any, path: str, depth: int = 0) -> Any:
    if depth > 64:
        raise PluginProfileValidationError(f"{path}: data nesting exceeds depth 64")
    if type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_copy_data(item, f"{path}[{index}]", depth + 1)
                for index, item in enumerate(value)]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _copy_data(item, f"{path}.{key}", depth + 1) for key, item in value.items()}
    raise PluginProfileValidationError(f"{path}: use finite JSON values without null for TOML storage")


def _object_data(value: dict, name: str) -> dict:
    if not isinstance(value, dict):
        raise PluginProfileValidationError(f"{name} must be an object")
    return _copy_data(value, name)


def _fields(data: dict, required: set[str], optional: set[str] = frozenset()) -> None:
    if not isinstance(data, dict) or set(data) - required - optional or required - set(data):
        raise PluginProfileValidationError("Document has missing or unknown fields")


def _processing_input(value, kind: str) -> ProcessingInput | None:
    if value is not None and (kind != "decoder" or not isinstance(value, ProcessingInput)):
        raise PluginProfileValidationError("processing_input requires a decoder profile and ProcessingInput")
    return value


def module_for_slot(slot: str) -> str:
    plugin_kind_for_slot(slot)
    return slot.split(".", 1)[0]


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _parse_profile_fields(data: dict) -> dict:
    result = dict(data)
    if "processing_input" in result:
        try:
            result["processing_input"] = ProcessingInput.from_dict(result["processing_input"])
        except (ValueError, TypeError) as exc:
            raise PluginProfileValidationError(f"Invalid processing_input: {exc}") from exc
    return result


@dataclass(frozen=True, slots=True)
class PluginSelection:
    """Exact package, contribution and installed version identity."""

    package_id: str
    contribution_id: str
    version: str

    def __post_init__(self) -> None:
        for name in ("package_id", "contribution_id", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise PluginProfileValidationError(f"{name} must be a non-empty, trimmed string")

        if not re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,127}", self.version) or self.version.endswith("."):
            raise PluginProfileValidationError("version must be an exact package version")

    def to_dict(self) -> dict:
        return {"package_id": self.package_id, "contribution_id": self.contribution_id, "version": self.version}

    @classmethod
    def from_dict(cls, data: dict) -> PluginSelection:
        _fields(data, {"package_id", "contribution_id", "version"})
        return cls(data["package_id"], data["contribution_id"], data["version"])


@dataclass(frozen=True, slots=True)
class PluginProfile:
    profile_id: str
    name: str
    kind: str
    selection: PluginSelection
    parameters: dict
    configuration_version: int = 1
    revision: int = 0
    format_version: int = 2
    processing_input: ProcessingInput | None = None

    def __post_init__(self) -> None:
        validate_profile_id(self.profile_id)
        if (not isinstance(self.name, str) or not 1 <= len(self.name.strip()) <= 64
                or any(ord(character) < 32 for character in self.name)):
            raise PluginProfileValidationError("name must be single-line text of 1 to 64 characters")
        if not isinstance(self.kind, str) or self.kind not in PLUGIN_KINDS:
            raise PluginProfileValidationError("Unsupported plugin kind")
        if not isinstance(self.selection, PluginSelection):
            raise PluginProfileValidationError("selection must be PluginSelection")
        if type(self.format_version) is not int or self.format_version != 2:
            raise PluginProfileValidationError("Unsupported profile format_version")
        _positive_integer(self.configuration_version, "configuration_version")
        _positive_integer(self.revision, "revision", minimum=0)
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "parameters", _object_data(self.parameters, "parameters"))
        object.__setattr__(self, "processing_input", _processing_input(self.processing_input, self.kind))

    def to_dict(self) -> dict:
        data = {
            "format_version": self.format_version, "profile_id": self.profile_id,
            "name": self.name, "kind": self.kind, "selection": self.selection.to_dict(),
            "parameters": _object_data(self.parameters, "parameters"),
            "configuration_version": self.configuration_version, "revision": self.revision,
        }
        if self.processing_input is not None:
            data["processing_input"] = self.processing_input.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> PluginProfile:
        _fields(data, {"format_version", "profile_id", "name", "kind", "selection",
                       "parameters", "configuration_version", "revision"},
                {"processing_input"})
        return cls(**{**_parse_profile_fields(data),
                      "selection": PluginSelection.from_dict(data["selection"])})


@dataclass(frozen=True, slots=True)
class ProfileBinding:
    """A slot pins parameter values independently of later profile edits."""

    slot: str
    kind: str
    profile_id: str | None
    selection: PluginSelection
    parameters: dict
    configuration_version: int = 1
    profile_revision: int = 0
    processing_input: ProcessingInput | None = None

    def __post_init__(self) -> None:
        if self.kind != plugin_kind_for_slot(self.slot):
            raise PluginProfileValidationError("slot and plugin kind must match")
        if self.profile_id is not None:
            validate_profile_id(self.profile_id)
        if not isinstance(self.selection, PluginSelection):
            raise PluginProfileValidationError("selection must be PluginSelection")
        _positive_integer(self.configuration_version, "configuration_version")
        _positive_integer(self.profile_revision, "profile_revision",
                          minimum=0 if self.profile_id is None else 1)
        object.__setattr__(self, "parameters", _object_data(self.parameters, "parameters"))
        object.__setattr__(self, "processing_input", _processing_input(self.processing_input, self.kind))

    def to_dict(self) -> dict:
        data = {
            "slot": self.slot, "kind": self.kind, "selection": self.selection.to_dict(),
            "parameters": _object_data(self.parameters, "parameters"),
            "configuration_version": self.configuration_version,
            "profile_revision": self.profile_revision,
        }
        if self.profile_id is not None:
            data["profile_id"] = self.profile_id
        if self.processing_input is not None:
            data["processing_input"] = self.processing_input.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ProfileBinding:
        _fields(data, {"slot", "kind", "selection", "parameters", "configuration_version",
                       "profile_revision"}, {"profile_id", "processing_input"})
        return cls(**{**_parse_profile_fields(data), "profile_id": data.get("profile_id"),
                      "selection": PluginSelection.from_dict(data["selection"])})


@dataclass(frozen=True, slots=True, init=False)
class ModuleActivation:
    """Immutable module content; getters return independent collection copies."""

    module: str
    _settings_json: str = field(repr=False)
    _bindings_json: str = field(repr=False)
    revision: str

    def __init__(self, module: str, settings: dict, bindings: tuple[ProfileBinding, ...]) -> None:
        if not isinstance(module, str) or module not in MODULES:
            raise PluginProfileValidationError("Unsupported configuration module")
        if not isinstance(bindings, (tuple, list)) or any(not isinstance(item, ProfileBinding) for item in bindings):
            raise PluginProfileValidationError("bindings must be an ordered list of ProfileBinding")
        if len({item.slot for item in bindings}) != len(bindings):
            raise PluginProfileValidationError("Duplicate activation slot")
        if any(module_for_slot(item.slot) != module or item.kind not in MODULE_KINDS[module] for item in bindings):
            raise PluginProfileValidationError("Bindings must belong to the configuration module")
        settings = _object_data(settings, "settings")
        if settings.keys() - SECTION_SETTING_KEYS[module]:
            raise PluginProfileValidationError("Settings must belong to the configuration module")
        for key, value in settings.items():
            if key.endswith("auto_spawn"):
                valid = type(value) is bool
            elif key == "STIMULATION.renderer":
                valid = isinstance(value, str) and value in ("pyopengl", "moderngl")
            elif key.endswith(".server_ip"):
                valid = isinstance(value, str) and bool(value.strip())
            elif key.endswith(".server_port"):
                valid = type(value) is int and 1 <= value <= 65535
            else:
                try:
                    valid = (type(value) in (int, float) and math.isfinite(value)
                             and (value > 0 if key == "ACTUATION.watchdog_timeout_s" else value >= 0))
                except OverflowError:
                    valid = False
            if not valid:
                raise PluginProfileValidationError(f"Invalid module setting: {key}")
        values = [item.to_dict() for item in bindings]
        semantic = [{key: value for key, value in item.items() if key not in {"profile_id", "profile_revision"}}
                    for item in values]
        object.__setattr__(self, "module", module)
        object.__setattr__(self, "_settings_json", _canonical(settings))
        object.__setattr__(self, "_bindings_json", _canonical(values))
        content = _canonical({"module": module, "settings": settings, "bindings": semantic})
        object.__setattr__(self, "revision", hashlib.sha256(content.encode("utf-8")).hexdigest())

    @property
    def settings(self) -> dict:
        return json.loads(self._settings_json)

    @property
    def bindings(self) -> tuple[ProfileBinding, ...]:
        return tuple(ProfileBinding.from_dict(item) for item in json.loads(self._bindings_json))

    def to_dict(self) -> dict:
        return {"module": self.module, "revision": self.revision, "settings": self.settings,
                "bindings": json.loads(self._bindings_json)}

    @classmethod
    def from_dict(cls, data: dict) -> ModuleActivation:
        _fields(data, {"module", "revision", "settings", "bindings"})
        if not isinstance(data["bindings"], list):
            raise PluginProfileValidationError("bindings must be a list")
        result = cls(data["module"], data["settings"], tuple(ProfileBinding.from_dict(item) for item in data["bindings"]))
        if data["revision"] != result.revision:
            raise PluginProfileValidationError("Activation revision does not match its content")
        return result


@dataclass(frozen=True, slots=True, init=False)
class EngineSettings:
    _settings_json: str = field(repr=False)
    revision: str

    def __init__(self, settings: dict) -> None:
        settings = _object_data(settings, "settings")
        if settings.keys() - SECTION_SETTING_KEYS["engine"]:
            raise PluginProfileValidationError("Settings must belong to the engine section")
        for key, value in settings.items():
            if key == "debug":
                valid = type(value) is bool
            elif key == "NEUROENGINE.queue_size":
                valid = type(value) is int and value > 0
            else:
                try:
                    valid = type(value) in (int, float) and math.isfinite(value) and value > 0
                except OverflowError:
                    valid = False
            if not valid:
                raise PluginProfileValidationError(f"Invalid Engine setting: {key}")
        object.__setattr__(self, "_settings_json", _canonical(settings))
        content = _canonical({"engine": settings})
        object.__setattr__(self, "revision", hashlib.sha256(content.encode("utf-8")).hexdigest())

    @property
    def settings(self) -> dict:
        return json.loads(self._settings_json)

    def to_dict(self) -> dict:
        return {"revision": self.revision, "settings": self.settings}

    @classmethod
    def from_dict(cls, data: dict) -> EngineSettings:
        _fields(data, {"revision", "settings"})
        result = cls(data["settings"])
        if data["revision"] != result.revision:
            raise PluginProfileValidationError("Engine revision does not match its content")
        return result


@dataclass(frozen=True, slots=True)
class ActivationSet:
    modules: Mapping[str, ModuleActivation | None]
    engine: EngineSettings | None

    def __post_init__(self):
        if not isinstance(self.modules, Mapping) or self.modules.keys() - set(MODULES):
            raise PluginProfileValidationError("Unsupported activation sections")
        values = {module: self.modules.get(module) for module in MODULES}
        if any(item is not None and (not isinstance(item, ModuleActivation) or item.module != module)
               for module, item in values.items()):
            raise PluginProfileValidationError("Activation module does not match its section")
        if self.engine is not None and not isinstance(self.engine, EngineSettings):
            raise PluginProfileValidationError("engine must be EngineSettings or None")
        object.__setattr__(self, "modules", MappingProxyType(values))

    def revisions(self) -> dict[str, str | None]:
        return {**{module: item.revision if item else None for module, item in self.modules.items()},
                "engine": self.engine.revision if self.engine else None}

    def bindings(self) -> tuple[ProfileBinding, ...]:
        return tuple(binding for item in self.modules.values() if item for binding in item.bindings)

    def settings(self) -> dict:
        values = {key: value for item in self.modules.values() if item for key, value in item.settings.items()}
        if self.engine is not None:
            values.update(self.engine.settings)
        return values


def _saved_at(value: str) -> None:
    try:
        valid = isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        valid = False
    if not valid:
        raise PluginProfileValidationError("saved_at must be an ISO-8601 timestamp with a timezone")


@dataclass(frozen=True, slots=True, init=False)
class ProfileRevision:
    profile_id: str
    revision: int
    saved_at: str
    _profile_json: str = field(repr=False)

    def __init__(self, profile_id: str, revision: int, saved_at: str, profile: PluginProfile):
        if not isinstance(profile, PluginProfile) or profile.profile_id != profile_id or profile.revision != revision:
            raise PluginProfileValidationError("History must match the saved profile identity and revision")
        _positive_integer(revision, "revision")
        _saved_at(saved_at)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "saved_at", saved_at)
        object.__setattr__(self, "_profile_json", _canonical(profile.to_dict()))

    @property
    def profile(self) -> PluginProfile:
        return PluginProfile.from_dict(json.loads(self._profile_json))

    def to_dict(self) -> dict:
        return {"profile_id": self.profile_id, "revision": self.revision, "saved_at": self.saved_at,
                "profile": json.loads(self._profile_json)}

    @classmethod
    def from_dict(cls, data: dict) -> ProfileRevision:
        _fields(data, {"profile_id", "revision", "saved_at", "profile"})
        return cls(data["profile_id"], data["revision"], data["saved_at"], PluginProfile.from_dict(data["profile"]))


@dataclass(frozen=True, slots=True)
class ModuleActivationRevision:
    module: str
    entry_id: str
    saved_at: str
    activation: ModuleActivation
    restored_from: str | None = None

    def __post_init__(self):
        validate_profile_id(self.entry_id)
        if self.restored_from is not None:
            validate_profile_id(self.restored_from)
        _saved_at(self.saved_at)
        if not isinstance(self.activation, ModuleActivation) or self.activation.module != self.module:
            raise PluginProfileValidationError("History must match its module")

    @property
    def revision(self) -> str:
        return self.activation.revision

    def to_dict(self) -> dict:
        data = {"module": self.module, "entry_id": self.entry_id, "saved_at": self.saved_at,
                "activation": self.activation.to_dict()}
        if self.restored_from is not None:
            data["restored_from"] = self.restored_from
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ModuleActivationRevision:
        _fields(data, {"module", "entry_id", "saved_at", "activation"}, {"restored_from"})
        return cls(data["module"], data["entry_id"], data["saved_at"],
                   ModuleActivation.from_dict(data["activation"]), data.get("restored_from"))


class PluginProfileRepository(Protocol):
    def list_profiles(self, kind: str | None = None) -> list[PluginProfile]: ...
    def get_profile(self, profile_id: str) -> PluginProfile: ...
    def save_profile(self, profile: PluginProfile, expected_revision: int | None) -> PluginProfile: ...
    def delete_profile(self, profile_id: str, expected_revision: int) -> None: ...
    def list_profile_history(self, profile_id: str) -> list[ProfileRevision]: ...
    def get_profile_revision(self, profile_id: str, revision: int) -> ProfileRevision | None: ...
    def get_module(self, module: str) -> ModuleActivation | None: ...
    def save_module(self, activation: ModuleActivation, expected_revision: str | None) -> ModuleActivation: ...
    def remove_module(self, module: str, *, expected_revision: str) -> None: ...
    def list_module_history(self, module: str) -> list[ModuleActivationRevision]: ...
    def get_module_history_entry(self, module: str, entry_id: str) -> ModuleActivationRevision | None: ...
    def record_module_history(self, activation: ModuleActivation, entry_id: str,
                              restored_from: str | None = None) -> ModuleActivationRevision: ...
    def get_engine(self) -> EngineSettings | None: ...
    def save_engine(self, settings: EngineSettings, expected_revision: str | None) -> EngineSettings: ...
    def remove_engine(self, *, expected_revision: str) -> None: ...
    def get_activation_set(self) -> ActivationSet: ...
