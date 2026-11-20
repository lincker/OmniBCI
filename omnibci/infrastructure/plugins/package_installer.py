"""Install trusted plugin ZIPs without importing code or activating plugins.

Archives contain a v2 ``manifest.toml`` at their root. Optional
``package.host_version`` and ``package.requires`` declare exact host and
host-library versions. Dependencies are checked against the host allowlist;
installation never invokes pip or adds a package to ``sys.path``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, replace
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from omnibci.application.plugin_packages import MarketVerification, PluginPackageError
from omnibci.infrastructure.plugins.descriptors import PluginDescriptor
from omnibci.infrastructure.plugins.artifact_inventory import artifact_inventory, is_bytecode_path
from omnibci.infrastructure.plugins.package_requirements import (
    check_dependencies, installed_host_libraries, installed_host_version, normalize_dependency,
)
from omnibci.infrastructure.plugins.plugin_catalog import (
    PluginCatalog,
)

from omnibci.infrastructure.plugins.manifest import (
    PluginManifestError, parse_manifest, read_manifest, _unique_json_object,
)

_RECEIPT = ".omnibci-install.json"
_CHUNK_SIZE = 1024 * 1024
_WINDOWS_DEVICES = frozenset({
    "con", "prn", "aux", "nul", "conin$", "conout$",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    *(f"{prefix}{number}" for prefix in ("com", "lpt") for number in "¹²³"),
})


class PackageInstallError(RuntimeError):
    """The archive cannot be installed without changing existing versions."""


@dataclass(frozen=True, slots=True)
class InstallationLimits:
    max_archive_bytes: int = 256 * 1024 * 1024
    max_expanded_bytes: int = 1024 * 1024 * 1024
    max_file_bytes: int = 256 * 1024 * 1024
    max_manifest_bytes: int = 1024 * 1024
    max_entries: int = 10_000
    max_compression_ratio: int = 200

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (
            self.max_archive_bytes, self.max_expanded_bytes, self.max_file_bytes,
            self.max_manifest_bytes, self.max_entries, self.max_compression_ratio,
        )):
            raise ValueError("Installation limits must be positive integers")


@dataclass(frozen=True, slots=True)
class InstalledPackage:
    package_id: str
    version: str
    location: Path
    sha256: str
    already_installed: bool
    contributions: tuple[PluginDescriptor, ...]


def _portable_path(name: str, *, directory: bool = False) -> tuple[str, ...]:
    """Use one portable archive spelling, including on case-insensitive disks."""
    raw = name[:-1] if directory and name.endswith("/") else name
    parts = raw.split("/")
    if not raw or len(raw) > 4096 or "\\" in raw or any(
        not part or part in {".", ".."} or len(part.encode("utf-8")) > 255
        or part.endswith((".", " "))
        or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        or part.split(".")[0].rstrip(" .").casefold() in _WINDOWS_DEVICES
        for part in parts
    ):
        raise PackageInstallError(f"Unsafe or non-portable archive path: {name!r}")
    return tuple(parts)


def _path_key(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)


class PluginPackageInstaller:
    """Publish immutable versions at ``root/publisher.id/version``.

    The caller must authorize the local file or verify its repository trust
    before calling ``install``. A matching SHA-256 verifies bytes, not trust.
    Catalog integration chooses when installed versions become selectable.
    """

    def __init__(
        self,
        root: Path,
        *,
        host_version: str | None = None,
        host_dependencies: Mapping[str, str] | None = None,
        platform_name: str | None = None,
        limits: InstallationLimits | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.host_version = host_version or installed_host_version()
        if host_dependencies is None:
            host_dependencies = installed_host_libraries()
        self.host_dependencies = {
            normalize_dependency(name): version
            for name, version in host_dependencies.items()
        }
        self.platform_name = platform_name or sys.platform
        self.limits = limits or InstallationLimits()
        self.catalog = PluginCatalog(plugin_dir=self.root.parent, host_version=self.host_version)

    def install(
        self, archive: Path, *, revalidate: Callable[[], MarketVerification] | None = None,
        development: bool = False,
        expected_sha256: str | None = None,
        expected_package_id: str | None = None, expected_version: str | None = None,
        expected_contributions: Mapping[str, str] | None = None, expected_domain: str | None = None,
    ) -> InstalledPackage:
        """Validate, extract, and atomically publish; leave activation unchanged."""
        if type(development) is not bool or development and revalidate is not None:
            raise PackageInstallError("Development snapshots cannot carry repository admission")
        if expected_sha256 is not None and not re.fullmatch(
            r"[0-9a-fA-F]{64}", expected_sha256,
        ):
            raise PackageInstallError("Expected SHA-256 must contain 64 hexadecimal digits")
        if revalidate is not None and any(value is None for value in (
            expected_sha256, expected_package_id, expected_version, expected_domain, expected_contributions,
        )):
            raise PackageInstallError("Repository verification requires exact expected artifact metadata")
        self.root.mkdir(parents=True, exist_ok=True)
        # Catalog traversal ignores __ directories, so incomplete installations
        # cannot become visible even when the installation root is being scanned.
        with tempfile.TemporaryDirectory(prefix="__install-", dir=self.root) as temporary:
            staging = Path(temporary)
            copied_archive = staging / "archive.zip"
            try:
                digest = self._copy_archive(Path(archive), copied_archive)
                if expected_sha256 is not None and digest != expected_sha256.lower():
                    raise PackageInstallError("Archive SHA-256 does not match the expected digest")
                location = staging / "package"
                location.mkdir()
                self._check_directory_size(copied_archive)
                with zipfile.ZipFile(copied_archive) as package_zip:
                    members = self._validate_members(package_zip)
                    files = self._extract(package_zip, members, location)
                descriptors = self._validate_package(location)
                package = descriptors[0].package
                if expected_package_id is not None and package.identifier != expected_package_id:
                    raise PackageInstallError("Archive package identity does not match the expected identity")
                if expected_version is not None and package.version != expected_version:
                    raise PackageInstallError("Archive package version does not match the expected version")
                if expected_contributions is not None and {
                    descriptor.contribution_id: descriptor.category for descriptor in descriptors
                } != dict(expected_contributions):
                    raise PackageInstallError("Archive contributions do not match the expected contributions")
                if expected_domain is not None and package.domain != expected_domain:
                    raise PackageInstallError("Archive domain does not match signed repository metadata")
                _portable_path(package.identifier)
                _portable_path(package.version)
                if not re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,127}", package.version):
                    raise PackageInstallError("Package version must use a portable numeric version")
                destination_parent = self.root / package.identifier
                destination = destination_parent / package.version
                self._check_destination_names(package.identifier, package.version)
                receipt = {
                    "receipt_version": 1, "package_id": package.identifier,
                    "version": package.version, "sha256": digest, "files": files,
                    "origin": "development" if development else "market" if revalidate is not None else "local",
                    "market_verification": None,
                }
                with self._package_lock(package.identifier):
                    if revalidate is not None:
                        verification = revalidate()
                        if not isinstance(verification, MarketVerification):
                            raise PackageInstallError("Repository verifier must return accepted evidence")
                        receipt["market_verification"] = verification.to_dict()
                    self._write_receipt(location, receipt)
                    destination_parent.mkdir(exist_ok=True)
                    already_installed = self._publish(location, destination, receipt)
                published = tuple(replace(
                    descriptor, package=replace(descriptor.package, location=destination),
                ) for descriptor in descriptors)
                return InstalledPackage(
                    package.identifier, package.version, destination, digest,
                    already_installed, published,
                )
            except (PackageInstallError, PluginPackageError):
                raise
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                raise PackageInstallError(f"Cannot install plugin archive: {exc}") from exc

    def _copy_archive(self, source: Path, destination: Path) -> str:
        digest = hashlib.sha256()
        total = 0
        with source.open("rb") as reader, destination.open("xb") as writer:
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                raise PackageInstallError("Plugin archive must be a regular file")
            while block := reader.read(_CHUNK_SIZE):
                total += len(block)
                if total > self.limits.max_archive_bytes:
                    raise PackageInstallError("Archive exceeds the compressed size limit")
                writer.write(block)
                digest.update(block)
        return digest.hexdigest()

    def _check_directory_size(self, archive: Path) -> None:
        """Bound directory parsing before ZipFile allocates one object per entry.

        ZIP64 and split archives are unnecessary under the default size limits
        and are intentionally unsupported. Count the directory records instead
        of trusting the count declared by an untrusted end-of-directory record.
        """
        size = archive.stat().st_size
        with archive.open("rb") as reader:
            reader.seek(max(0, size - 65557))
            tail = reader.read()
            position = tail.rfind(b"PK\x05\x06")
            if position < 0 or len(tail) - position < 22:
                raise PackageInstallError("Archive has no valid ZIP directory")
            _, disk, directory_disk, disk_entries, entries, length, offset, comment_length = struct.unpack(
                "<4s4H2LH", tail[position:position + 22],
            )
            end = size - len(tail) + position
            if (
                disk or directory_disk or disk_entries != entries
                or entries == 0xFFFF or length == 0xFFFFFFFF or offset == 0xFFFFFFFF
                or offset + length != end or position + 22 + comment_length != len(tail)
            ):
                raise PackageInstallError("Split, ZIP64, or inconsistent ZIP directories are unsupported")
            if not entries or entries > self.limits.max_entries:
                raise PackageInstallError("Archive has an invalid number of entries")
            reader.seek(offset)
            counted = 0
            while reader.tell() < end:
                header = reader.read(46)
                if len(header) != 46 or header[:4] != b"PK\x01\x02":
                    raise PackageInstallError("Archive has a malformed ZIP directory")
                counted += 1
                if counted > self.limits.max_entries:
                    raise PackageInstallError("Archive has an invalid number of entries")
                reader.seek(sum(struct.unpack("<3H", header[28:34])), os.SEEK_CUR)
            if reader.tell() != end or counted != entries:
                raise PackageInstallError("Archive ZIP directory count or length is inconsistent")

    def _validate_members(
        self, archive: zipfile.ZipFile,
    ) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
        members = archive.infolist()
        if not members or len(members) > self.limits.max_entries:
            raise PackageInstallError("Archive has an invalid number of entries")
        paths: dict[tuple[str, ...], tuple[tuple[str, ...], bool]] = {}
        explicit: set[tuple[str, ...]] = set()
        checked = []
        expanded = 0
        for member in members:
            if member.orig_filename != member.filename:
                raise PackageInstallError("Archive member contains a null byte")
            directory = member.is_dir()
            parts = _portable_path(member.filename, directory=directory)
            if _path_key(parts)[0] in {"omnibci", "omnibci.py"}:
                raise PackageInstallError("Archive must not vendor the omnibci host namespace")
            if len(parts) == 1 and _path_key(parts) == _path_key((_RECEIPT,)):
                raise PackageInstallError("Archive contains reserved installation metadata")
            if is_bytecode_path(Path(*parts)):
                raise PackageInstallError("Archive must not contain Python bytecode or cache directories")
            mode = stat.S_IFMT(member.external_attr >> 16)
            if mode not in (0, stat.S_IFDIR if directory else stat.S_IFREG):
                raise PackageInstallError(f"Archive member is not a regular file: {member.filename}")
            if member.flag_bits & 1 or member.compress_type not in (
                zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
            ):
                raise PackageInstallError("Encrypted or unsupported compressed entries are forbidden")
            expanded += member.file_size
            if member.file_size > self.limits.max_file_bytes or expanded > self.limits.max_expanded_bytes:
                raise PackageInstallError("Archive exceeds the expanded size limit")
            if parts == ("manifest.toml",) and member.file_size > self.limits.max_manifest_bytes:
                raise PackageInstallError("Manifest exceeds the size limit")
            if directory and member.file_size:
                raise PackageInstallError("Directory entries must not contain data")
            if member.file_size > max(1, member.compress_size) * self.limits.max_compression_ratio:
                raise PackageInstallError("Archive exceeds the compression ratio limit")
            key = _path_key(parts)
            if key in explicit:
                raise PackageInstallError(f"Duplicate or case-colliding archive path: {member.filename}")
            explicit.add(key)
            for length in range(1, len(parts) + 1):
                prefix = parts[:length]
                prefix_key = _path_key(prefix)
                is_directory = length < len(parts) or directory
                previous = paths.get(prefix_key)
                if previous is not None and previous != (prefix, is_directory):
                    raise PackageInstallError(f"Conflicting archive path: {member.filename}")
                paths[prefix_key] = (prefix, is_directory)
            checked.append((member, parts))
        if (("manifest.toml",), False) != paths.get(("manifest.toml",)):
            raise PackageInstallError("Archive requires manifest.toml at its root")
        return checked

    def _extract(
        self, archive: zipfile.ZipFile,
        members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]], location: Path,
    ) -> dict[str, str]:
        files: dict[str, str] = {}
        for member, parts in members:
            target = location.joinpath(*parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(member) as reader, target.open("xb") as writer:
                while block := reader.read(min(_CHUNK_SIZE, member.file_size - size + 1)):
                    size += len(block)
                    if size > member.file_size:
                        raise PackageInstallError("Expanded member size differs from ZIP metadata")
                    writer.write(block)
                    digest.update(block)
                if size != member.file_size:
                    raise PackageInstallError("Expanded member size differs from ZIP metadata")
                writer.flush()
                os.fsync(writer.fileno())
            files[str(PurePosixPath(*parts))] = digest.hexdigest()
        return files

    def _validate_package(self, location: Path) -> tuple[PluginDescriptor, ...]:
        try:
            document = read_manifest(location)
            descriptors = parse_manifest(document, location, source="user", precedence=0,
                                         host_version=self.host_version)
        except PluginManifestError as exc:
            raise PackageInstallError(f"Cannot install invalid package manifest: {exc}") from exc
        self._check_requirements(document["package"])
        for descriptor in descriptors:
            self._check_entrypoint(descriptor)
        if not any(item.supports_platform(self.platform_name) for item in descriptors):
            raise PackageInstallError(f"Package has no contributions compatible with {self.platform_name}")
        return tuple(descriptors)

    def _check_requirements(self, package: dict) -> None:
        try:
            check_dependencies(package.get("requires", {}), self.host_dependencies)
        except ValueError as exc:
            raise PackageInstallError(str(exc)) from exc

    @staticmethod
    def _check_entrypoint(descriptor: PluginDescriptor) -> None:
        assert descriptor.entrypoint is not None
        parts = descriptor.entrypoint.module.split(".")
        parent = descriptor.location
        for part in parts[:-1]:
            if (parent / f"{part}.py").exists() or not (parent / part).is_dir():
                raise PackageInstallError(f"Entrypoint prefix is not an unambiguous package: {part}")
            parent /= part
        module_file = parent / f"{parts[-1]}.py"
        package_dir = parent / parts[-1]
        if module_file.exists() and package_dir.exists() or not (
            module_file.is_file() or (package_dir / "__init__.py").is_file()
        ):
            raise PackageInstallError(f"Missing or ambiguous entrypoint module: {descriptor.entrypoint}")

    def _check_destination_names(self, package_id: str, version: str) -> None:
        for parent, name in ((self.root, package_id), (self.root / package_id, version)):
            if parent.is_symlink() or parent.exists() and not parent.is_dir():
                raise PackageInstallError("Installation parent must be a real directory")
            if not parent.exists():
                continue
            for child in parent.iterdir():
                if _path_key((child.name,)) == _path_key((name,)) and child.name != name:
                    raise PackageInstallError("Package identity or version collides across platforms")
            if (parent / name).is_symlink():
                raise PackageInstallError("Installation destination must not be a symlink")

    @contextmanager
    def _package_lock(self, package_id: str):
        directory = self.root / "__locks"
        if directory.is_symlink():
            raise PackageInstallError("Package lock directory must not be a symlink")
        directory.mkdir(exist_ok=True)
        path = directory / f"{package_id}.lock"
        if path.is_symlink():
            raise PackageInstallError("Package lock must not be a symlink")
        with path.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"0"); stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _write_receipt(location: Path, receipt: dict) -> None:
        with (location / _RECEIPT).open("x", encoding="utf-8") as writer:
            json.dump(receipt, writer, sort_keys=True)
            writer.flush()
            os.fsync(writer.fileno())

    def read_receipt(self, location: Path, package_id: str, version: str) -> dict:
        """Read bounded host records without refreshing repository trust."""
        path = location / _RECEIPT
        # Reserve space for one bounded signed proof as well as escaped paths.
        limit = self.limits.max_entries * (6 * 4096 + 128) + 2 * 1024 * 1024
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise PackageInstallError("Installation receipt must be a bounded regular file")
        receipt = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
        if (not isinstance(receipt, dict)
                or receipt.keys() != {"receipt_version", "package_id", "version", "sha256", "files",
                                      "origin", "market_verification"}
                or type(receipt["receipt_version"]) is not int or receipt["receipt_version"] != 1
                or receipt["package_id"] != package_id or receipt["version"] != version):
            raise PackageInstallError("Installation receipt identity or format is invalid")
        files = receipt["files"]
        if not isinstance(files, dict) or not files or len(files) > self.limits.max_entries:
            raise PackageInstallError("Installation receipt file list is invalid")
        for value in [receipt["sha256"], *files.values()]:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise PackageInstallError("Installation receipt digest is invalid")
        for name in files:
            _portable_path(name)
        if "manifest.toml" not in files:
            raise PackageInstallError("Installation receipt does not include the manifest")
        if receipt["origin"] not in ("local", "market", "development"):
            raise PackageInstallError("Installation receipt origin is invalid")
        verification = receipt["market_verification"]
        if receipt["origin"] == "development" and verification is not None:
            raise PackageInstallError("Development snapshots cannot carry repository admission")
        if verification is not None:
            MarketVerification.from_dict(verification)
        elif receipt["origin"] == "market":
            raise PackageInstallError("Market origin requires accepted repository evidence")
        return receipt

    def _publish(self, source: Path, destination: Path, receipt: dict) -> bool:
        if not destination.exists():
            try:
                source.rename(destination)
                return False
            except OSError:
                if not destination.exists():
                    raise
        actual = self._verify_existing(destination, receipt)
        if "development" in (actual["origin"], receipt["origin"]) and actual["origin"] != receipt["origin"]:
            raise PackageInstallError("Development snapshots and installed releases require separate stores")
        incoming = receipt["market_verification"]
        retained = actual["market_verification"]
        # Identical proof retries retain their first acceptance timestamp.
        if incoming is not None and (retained is None or any(
            incoming[key] != retained[key] for key in incoming if key != "accepted_at"
        )):
            actual["market_verification"] = incoming
            self._replace_receipt(destination, actual)
        return True

    @staticmethod
    def _replace_receipt(destination: Path, receipt: dict) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="__receipt-",
                                             dir=destination.parent, delete=False) as writer:
                temporary = Path(writer.name)
                json.dump(receipt, writer, sort_keys=True)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, destination / _RECEIPT)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _verify_existing(self, destination: Path, expected: dict) -> dict:
        try:
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError("destination is not a real directory")
            actual = self.read_receipt(destination, expected["package_id"], expected["version"])
            if any(actual[key] != expected[key] for key in ("package_id", "version", "sha256", "files")):
                raise ValueError("different archive or file inventory")
            if artifact_inventory(destination) != expected["files"]:
                raise ValueError("installed file inventory or content changed")
            return actual
        except (OSError, ValueError, TypeError, PackageInstallError) as exc:
            raise PackageInstallError(
                f"Package version already exists with conflicting content: {destination} ({exc})"
            ) from exc
