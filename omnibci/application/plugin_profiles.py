"""Portable plugin profiles and pinned activation snapshots, without I/O."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

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


def _legacy_fields(value, kind: str, processing_input) -> dict:
    data = _object_data(value, "legacy fields")
    allowed = {"acquisition_montage", "manual_montage"} if kind == "acquisition" else {"processing_input"} if kind == "decoder" else set()
    if set(data) - allowed or processing_input is not None and "processing_input" in data:
        raise PluginProfileValidationError("Invalid legacy profile fields")
    return data


def _parse_profile_fields(data: dict) -> dict:
    result = dict(data)
    legacy = {key: result.pop(key) for key in ("acquisition_montage", "manual_montage") if key in result}
    if "processing_input" in result:
        value = result.pop("processing_input")
        if not isinstance(value, dict):
            raise PluginProfileValidationError("processing_input must be an object")
        if type(value.get("schema_version", 1)) is not int:
            raise PluginProfileValidationError("processing_input schema_version must be an integer")
        if value.get("schema_version", 1) == 1:
            legacy["processing_input"] = value
        else:
            try:
                result["processing_input"] = ProcessingInput.from_dict(value)
            except (ValueError, TypeError) as exc:
                raise PluginProfileValidationError(f"Invalid processing_input: {exc}") from exc
    result["legacy"] = legacy
    return result


@dataclass(frozen=True, slots=True)
class PluginSelection:
    """Exact capability identity; None selects an unversioned legacy package."""

    package_id: str
    contribution_id: str
    version: str | None

    def __post_init__(self) -> None:
        for name in ("package_id", "contribution_id", "version"):
            value = getattr(self, name)
            if name == "version" and value is None:
                continue
            if not isinstance(value, str) or not value or value != value.strip():
                raise PluginProfileValidationError(f"{name} must be a non-empty, trimmed string")

    def to_dict(self) -> dict:
        data = {"package_id": self.package_id, "contribution_id": self.contribution_id}
        if self.version is not None:
            data["version"] = self.version
        return data

    @classmethod
    def from_dict(cls, data: dict) -> PluginSelection:
        _fields(data, {"package_id", "contribution_id"}, {"version"})
        return cls(data["package_id"], data["contribution_id"], data.get("version"))


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
    # Inert raw fields preserve old files and revision hashes until explicit replacement.
    legacy: dict = field(default_factory=dict, repr=False)

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
        object.__setattr__(self, "legacy", _legacy_fields(self.legacy, self.kind, self.processing_input))

    @property
    def processing_input_requires_review(self) -> bool:
        return "processing_input" in self.legacy

    def to_dict(self) -> dict:
        data = {
            "format_version": self.format_version, "profile_id": self.profile_id,
            "name": self.name, "kind": self.kind, "selection": self.selection.to_dict(),
            "parameters": _object_data(self.parameters, "parameters"),
            "configuration_version": self.configuration_version, "revision": self.revision,
        }
        data.update(_object_data(self.legacy, "legacy fields"))
        if self.processing_input is not None:
            data["processing_input"] = self.processing_input.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> PluginProfile:
        _fields(data, {"format_version", "profile_id", "name", "kind", "selection",
                       "parameters", "configuration_version", "revision"},
                {"acquisition_montage", "manual_montage", "processing_input"})
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
    legacy: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.slot, str):
            raise PluginProfileValidationError("slot must be a string")
        expected = _SLOTS.get(self.slot)
        if self.slot.startswith("processing.filter."):
            validate_profile_id(self.slot.removeprefix("processing.filter."))
            expected = "filter"
        if expected is None or self.kind != expected:
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
        object.__setattr__(self, "legacy", _legacy_fields(self.legacy, self.kind, self.processing_input))

    @property
    def processing_input_requires_review(self) -> bool:
        return "processing_input" in self.legacy

    def to_dict(self) -> dict:
        data = {
            "slot": self.slot, "kind": self.kind, "selection": self.selection.to_dict(),
            "parameters": _object_data(self.parameters, "parameters"),
            "configuration_version": self.configuration_version,
            "profile_revision": self.profile_revision,
        }
        if self.profile_id is not None:
            data["profile_id"] = self.profile_id
        data.update(_object_data(self.legacy, "legacy fields"))
        if self.processing_input is not None:
            data["processing_input"] = self.processing_input.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ProfileBinding:
        _fields(data, {"slot", "kind", "selection", "parameters", "configuration_version",
                       "profile_revision"}, {"profile_id", "manual_montage", "processing_input"})
        return cls(**{**_parse_profile_fields(data), "profile_id": data.get("profile_id"),
                      "selection": PluginSelection.from_dict(data["selection"])})


@dataclass(frozen=True, slots=True, init=False)
class ActivationSnapshot:
    """Immutable accepted content; collection getters return independent copies."""

    _settings_json: str = field(repr=False)
    _bindings_json: str = field(repr=False)
    _revision: str
    format_version: int = 2

    def __init__(self, settings: dict, bindings: tuple[ProfileBinding, ...],
                 format_version: int = 2) -> None:
        if type(format_version) is not int or format_version != 2:
            raise PluginProfileValidationError("Unsupported activation format_version")
        if not isinstance(bindings, (tuple, list)) or any(
            not isinstance(binding, ProfileBinding) for binding in bindings
        ):
            raise PluginProfileValidationError("bindings must be an ordered list of ProfileBinding")
        if len({binding.slot for binding in bindings}) != len(bindings):
            raise PluginProfileValidationError("Duplicate activation slot")
        settings = _object_data(settings, "settings")
        binding_data = [binding.to_dict() for binding in bindings]
        canonical = lambda data: json.dumps(data, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=False, allow_nan=False)
        object.__setattr__(self, "format_version", format_version)
        object.__setattr__(self, "_settings_json", canonical(settings))
        object.__setattr__(self, "_bindings_json", canonical(binding_data))
        semantic_bindings = [{key: value for key, value in data.items()
                              if key not in {"profile_id", "profile_revision"}}
                             for data in binding_data]
        content = canonical({"settings": settings, "bindings": semantic_bindings})
        object.__setattr__(self, "_revision", hashlib.sha256(content.encode("utf-8")).hexdigest())

    @property
    def settings(self) -> dict:
        return json.loads(self._settings_json)

    @property
    def bindings(self) -> tuple[ProfileBinding, ...]:
        return tuple(ProfileBinding.from_dict(data) for data in json.loads(self._bindings_json))

    @property
    def revision(self) -> str:
        return self._revision

    def to_dict(self) -> dict:
        return {"format_version": self.format_version, "revision": self.revision,
                "settings": self.settings, "bindings": json.loads(self._bindings_json)}

    @classmethod
    def from_dict(cls, data: dict) -> ActivationSnapshot:
        _fields(data, {"format_version", "revision", "settings", "bindings"})
        if not isinstance(data["bindings"], list):
            raise PluginProfileValidationError("bindings must be a list")
        snapshot = cls(data["settings"], tuple(ProfileBinding.from_dict(item)
                                             for item in data["bindings"]), data["format_version"])
        if data["revision"] != snapshot.revision:
            raise PluginProfileValidationError("Activation revision does not match its content")
        return snapshot


class PluginProfileRepository(Protocol):
    def list_profiles(self, kind: str | None = None) -> list[PluginProfile]: ...
    def get_profile(self, profile_id: str) -> PluginProfile: ...
    def save_profile(self, profile: PluginProfile, expected_revision: int | None) -> PluginProfile: ...
    def delete_profile(self, profile_id: str, expected_revision: int) -> None: ...
    def get_activation(self) -> ActivationSnapshot | None: ...
    def save_activation(self, snapshot: ActivationSnapshot,
                        expected_revision: str | None) -> ActivationSnapshot: ...
