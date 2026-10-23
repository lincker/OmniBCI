from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator
from zipfile import BadZipFile, ZipFile

import numpy as np


_SCHEMA = "omnibci.frame-pack/v2"
_LEGACY_SCHEMA = "omnibci.frame-pack/v1"
_WIDTH = 1920
_HEIGHT = 990
_ORIENTATION = "bottom-up"
_GROUPS = {
    "idle": (1, "L8", 1),
    "stimulation": (240, "L8", 1),
    "cue": (40, "RGB8", 3),
    "result": (40, "RGB8", 3),
}
_ENTRY_PATTERNS = {
    "idle": "frames/idle/{index:03d}.l8",
    "stimulation": "frames/stimulation/{index:03d}.l8",
    "cue": "frames/cue/{index:03d}.rgb8",
    "result": "frames/result/{index:03d}.rgb8",
}


class FramePackError(RuntimeError):
    pass


class FramePackFormatError(FramePackError):
    pass


class FramePackIntegrityError(FramePackError):
    pass


@dataclass(frozen=True)
class _FrameDescriptor:
    group: str
    index: int
    entry: str
    pixel_format: str
    shape: tuple[int, int, int]
    size: int
    sha256: str
    source_sha256: str


class FramePack:
    def __init__(self, path: Path | str, *, allow_legacy: bool = False) -> None:
        """Read v2 assets; only the offline builder opts into legacy migration."""
        self._path = Path(path)
        self._allow_legacy = allow_legacy
        try:
            self._archive = ZipFile(self._path, "r")
        except (OSError, BadZipFile) as exc:
            raise FramePackFormatError(
                f"Unable to open frame pack: {self._path}"
            ) from exc

        try:
            self._frames = self._load_manifest()
        except Exception:
            self._archive.close()
            raise

    def __enter__(self) -> FramePack:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._archive.close()

    @property
    def idle_frame(self) -> np.ndarray:
        return self._read_frame(self._frames["idle"][0])

    def iter_stimulation_frames(self) -> Iterator[np.ndarray]:
        for descriptor in self._frames["stimulation"]:
            yield self._read_frame(descriptor)

    def iter_cue_frames(self) -> Iterator[np.ndarray]:
        for descriptor in self._frames["cue"]:
            yield self._read_frame(descriptor)

    def iter_result_frames(self) -> Iterator[np.ndarray]:
        # Legacy cue frames are solid red and become v2 result frames.
        group = "result" if "result" in self._frames else "cue"
        for descriptor in self._frames[group]:
            yield self._read_frame(descriptor)

    def _load_manifest(self) -> dict[str, tuple[_FrameDescriptor, ...]]:
        archive_names = self._archive.namelist()
        if len(archive_names) != len(set(archive_names)):
            raise FramePackFormatError("Frame pack contains duplicate ZIP entries")
        for entry in archive_names:
            self._validate_entry_name(entry)

        try:
            manifest_bytes = self._archive.read("manifest.json")
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except KeyError as exc:
            raise FramePackFormatError("Frame pack is missing manifest.json") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FramePackFormatError("Frame pack manifest is invalid") from exc

        if not isinstance(manifest, dict):
            raise FramePackFormatError("Frame pack manifest must be an object")
        schema = manifest.get("schema")
        legacy = self._allow_legacy and schema == _LEGACY_SCHEMA
        if schema != _SCHEMA and not legacy:
            raise FramePackFormatError("Unsupported frame pack schema")
        groups = {name: spec for name, spec in _GROUPS.items() if not legacy or name != "result"}
        if manifest.get("width") != _WIDTH or manifest.get("height") != _HEIGHT:
            raise FramePackFormatError(
                f"Frame pack dimensions must be {_WIDTH}x{_HEIGHT}"
            )
        if manifest.get("orientation") != _ORIENTATION:
            raise FramePackFormatError(
                f"Frame pack orientation must be {_ORIENTATION}"
            )

        raw_frames = manifest.get("frames")
        if not isinstance(raw_frames, list):
            raise FramePackFormatError("Frame pack manifest frames must be a list")

        grouped: dict[str, list[_FrameDescriptor]] = {
            group: [] for group in groups
        }
        for raw_descriptor in raw_frames:
            descriptor = self._parse_descriptor(raw_descriptor)
            if descriptor.group not in grouped:
                raise FramePackFormatError(
                    f"Unknown frame group: {descriptor.group}"
                )
            grouped[descriptor.group].append(descriptor)

        expected_entries = {"manifest.json"}
        validated: dict[str, tuple[_FrameDescriptor, ...]] = {}
        for group, (count, pixel_format, channels) in groups.items():
            descriptors = sorted(grouped[group], key=lambda item: item.index)
            if [item.index for item in descriptors] != list(range(count)):
                raise FramePackFormatError(
                    f"Frame group {group} must contain indices 0 through {count - 1}"
                )
            for descriptor in descriptors:
                expected_entry = _ENTRY_PATTERNS[group].format(
                    index=descriptor.index
                )
                if descriptor.entry != expected_entry:
                    raise FramePackFormatError(
                        f"Frame entry must be {expected_entry}"
                    )
                if descriptor.pixel_format != pixel_format:
                    raise FramePackFormatError(
                        f"Frame {descriptor.entry} must use {pixel_format}"
                    )
                if descriptor.shape != (_HEIGHT, _WIDTH, channels):
                    raise FramePackFormatError(
                        f"Frame {descriptor.entry} has an invalid shape"
                    )
                if descriptor.size != _HEIGHT * _WIDTH * channels:
                    raise FramePackFormatError(
                        f"Frame {descriptor.entry} has an invalid byte size"
                    )
                if descriptor.entry not in archive_names:
                    raise FramePackFormatError(
                        f"Frame pack is missing {descriptor.entry}"
                    )
                if self._archive.getinfo(descriptor.entry).file_size != descriptor.size:
                    raise FramePackIntegrityError(
                        f"Frame {descriptor.entry} ZIP size does not match the manifest"
                    )
                expected_entries.add(descriptor.entry)
            validated[group] = tuple(descriptors)

        if set(archive_names) != expected_entries:
            raise FramePackFormatError(
                "Frame pack entries do not match the manifest"
            )
        return validated

    def _parse_descriptor(self, value: object) -> _FrameDescriptor:
        if not isinstance(value, dict):
            raise FramePackFormatError("Frame descriptor must be an object")
        try:
            group = value["group"]
            index = value["index"]
            entry = value["entry"]
            pixel_format = value["pixel_format"]
            shape = value["shape"]
            size = value["size"]
            sha256 = value["sha256"]
            source_sha256 = value["source_sha256"]
        except KeyError as exc:
            raise FramePackFormatError(
                f"Frame descriptor is missing {exc.args[0]}"
            ) from exc

        if not isinstance(group, str):
            raise FramePackFormatError("Frame group must be a string")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise FramePackFormatError("Frame index must be a non-negative integer")
        if not isinstance(entry, str):
            raise FramePackFormatError("Frame entry must be a string")
        self._validate_entry_name(entry)
        if not isinstance(pixel_format, str):
            raise FramePackFormatError("Pixel format must be a string")
        if (
            not isinstance(shape, list)
            or len(shape) != 3
            or any(
                not isinstance(item, int) or isinstance(item, bool)
                for item in shape
            )
        ):
            raise FramePackFormatError("Frame shape must contain three integers")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise FramePackFormatError("Frame byte size must be a non-negative integer")
        self._validate_digest(sha256, "canonical")
        self._validate_digest(source_sha256, "source")

        return _FrameDescriptor(
            group=group,
            index=index,
            entry=entry,
            pixel_format=pixel_format,
            shape=tuple(shape),
            size=size,
            sha256=sha256,
            source_sha256=source_sha256,
        )

    def _read_frame(self, descriptor: _FrameDescriptor) -> np.ndarray:
        try:
            raw = self._archive.read(descriptor.entry)
        except (KeyError, BadZipFile, OSError) as exc:
            raise FramePackIntegrityError(
                f"Unable to read frame {descriptor.entry}"
            ) from exc
        if len(raw) != descriptor.size:
            raise FramePackIntegrityError(
                f"Frame {descriptor.entry} has an unexpected byte size"
            )
        if hashlib.sha256(raw).hexdigest() != descriptor.sha256:
            raise FramePackIntegrityError(
                f"Frame {descriptor.entry} failed SHA-256 verification"
            )
        return np.frombuffer(raw, dtype=np.uint8).reshape(descriptor.shape)

    @staticmethod
    def _validate_entry_name(entry: str) -> None:
        path = PurePosixPath(entry)
        if (
            not entry
            or "\\" in entry
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or entry != "/".join(path.parts)
        ):
            raise FramePackFormatError(f"Invalid ZIP entry path: {entry!r}")

    @staticmethod
    def _validate_digest(value: object, name: str) -> None:
        if not isinstance(value, str) or len(value) != 64:
            raise FramePackFormatError(f"Invalid {name} SHA-256 digest")
        try:
            int(value, 16)
        except ValueError as exc:
            raise FramePackFormatError(f"Invalid {name} SHA-256 digest") from exc
