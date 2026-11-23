"""A signed, static plugin repository with verified downloads and no accounts."""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from omnibci.application.plugin_management.market import MarketContribution, MarketPackage, PreparedMarketInstall
from omnibci.application.plugin_management.identity import (
    PACKAGE_DOMAINS, PLUGIN_KINDS, SUPPORTED_PLATFORMS, PluginSelection,
    is_valid_identifier, is_valid_package_id, is_valid_sha256, is_valid_version,
)
from omnibci.application.plugin_management.packages import MarketVerification
from omnibci.infrastructure.storage import strict_json_loads

_INDEX_FIELDS = frozenset({
    "schema_version", "repository_id", "issued_at", "expires_at", "packages", "signature",
})
_PACKAGE_FIELDS = frozenset({
    "package_id", "version", "display_name", "description", "download", "sha256", "size",
    "contributions", "api_version", "platforms", "host_version", "requires", "domain",
})


class MarketRepositoryError(RuntimeError):
    """A repository response failed transport, signature, or metadata validation."""


@dataclass(frozen=True, slots=True)
class MarketLimits:
    max_index_bytes: int = 1024 * 1024
    max_download_bytes: int = 256 * 1024 * 1024
    max_packages: int = 2000
    timeout_seconds: float = 10.0
    transfer_seconds: float = 120.0

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (
            self.max_index_bytes, self.max_download_bytes, self.max_packages,
        )) or any(
            isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or value <= 0
            for value in (self.timeout_seconds, self.transfer_seconds)
        ):
            raise ValueError("Market limits must be finite positive values")


def canonical_index_bytes(document: Mapping) -> bytes:
    """Publisher/client signing format: compact sorted UTF-8 JSON, no signature."""
    try:
        return json.dumps(
            {key: value for key, value in document.items() if key != "signature"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise MarketRepositoryError("Index is not canonicalizable JSON") from exc


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise MarketRepositoryError("Repository redirects are not allowed")


def _text(value, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise MarketRepositoryError(f"Invalid {field}")
    return value


class StaticMarketRepository:
    """Read one operator-configured repository and download exact package versions.

    Public keys must come from operator configuration, never the repository.
    ``allow_http`` is limited to literal loopback addresses for controlled tests.
    Listing only reads signed metadata. Downloads do not publish or enable a package.
    """

    def __init__(
        self,
        repo_url: str,
        trusted_keys: Mapping[str, bytes | str | Ed25519PublicKey],
        cache_dir: Path,
        *,
        expected_repository_id: str | None = None,
        allow_http: bool = False,
        limits: MarketLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.allow_http = allow_http
        self._validate_url(repo_url)
        self.repo_url = repo_url
        self.cache_dir = Path(cache_dir).resolve()
        self.expected_repository_id = expected_repository_id
        self.limits = limits or MarketLimits()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._opener = build_opener(_RejectRedirects())
        self._keys = {}
        for identifier, key in trusted_keys.items():
            _text(identifier, "trusted key ID", 128)
            try:
                if isinstance(key, str):
                    key = base64.b64decode(key, validate=True)
                self._keys[identifier] = (
                    key if isinstance(key, Ed25519PublicKey)
                    else Ed25519PublicKey.from_public_bytes(key)
                )
            except (TypeError, ValueError) as exc:
                raise MarketRepositoryError(f"Invalid trusted Ed25519 public key: {identifier}") from exc
        if not self._keys:
            raise MarketRepositoryError("At least one operator-configured trusted key is required")

    def list_packages(self) -> tuple[MarketPackage, ...]:
        """Fetch and verify current metadata; never download or execute plugins."""
        return self._parse_index(self._fetch_index())

    def _fetch_index(self) -> bytes:
        buffer = io.BytesIO()
        self._transfer(self.repo_url, buffer, self.limits.max_index_bytes)
        return buffer.getvalue()

    def _parse_index(self, raw: bytes) -> tuple[MarketPackage, ...]:
        if not isinstance(raw, bytes) or len(raw) > self.limits.max_index_bytes:
            raise MarketRepositoryError("Repository index exceeds the size limit")
        document = strict_json_loads(
            raw, error_cls=MarketRepositoryError, reject_non_finite=True,
        )
        if not isinstance(document, dict) or document.keys() != _INDEX_FIELDS:
            raise MarketRepositoryError("Repository index has unsupported or missing fields")
        self._verify_signature(document)
        if type(document["schema_version"]) is not int or document["schema_version"] != 1:
            raise MarketRepositoryError("Unsupported repository schema version")
        repository_id = _text(document["repository_id"], "repository ID", 128)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", repository_id):
            raise MarketRepositoryError("Invalid repository ID")
        if self.expected_repository_id is not None and repository_id != self.expected_repository_id:
            raise MarketRepositoryError("Repository identity differs from operator configuration")
        self._verify_times(document)
        packages = document["packages"]
        if not isinstance(packages, list) or len(packages) > self.limits.max_packages:
            raise MarketRepositoryError("Repository contains an invalid number of packages")
        parsed = tuple(self._package(item, repository_id) for item in packages)
        if len({(item.package_id, item.version) for item in parsed}) != len(parsed):
            raise MarketRepositoryError("Repository contains duplicate package versions")
        return parsed

    def prepare_install(self, package_id: str, version: str) -> PreparedMarketInstall:
        proof = self._fetch_index()
        selected = next((
            item for item in self._parse_index(proof)
            if item.package_id == package_id and item.version == version
        ), None)
        if selected is None:
            raise MarketRepositoryError(f"Unknown exact repository package: {package_id}@{version}")
        archive = self._download(selected)
        return PreparedMarketInstall(selected, archive, proof)

    def verify_prepared(self, prepared: PreparedMarketInstall) -> MarketVerification:
        # Called after archive validation, immediately before local publication.
        # The accepted evidence stays in the host receipt, outside plugin inputs.
        selected = prepared.package
        if selected not in self._parse_index(prepared.proof):
            raise MarketRepositoryError("Prepared package differs from signed repository metadata")
        document = json.loads(prepared.proof)
        key_id = document["signature"]["key_id"]
        return MarketVerification(
            selected.repository_id, key_id,
            hashlib.sha256(self._keys[key_id].public_bytes_raw()).hexdigest(),
            self.clock().astimezone(UTC).isoformat(), prepared.proof,
        )

    def _verify_signature(self, document: dict) -> None:
        signature = document["signature"]
        if not isinstance(signature, dict) or signature.keys() != {"algorithm", "key_id", "value"}:
            raise MarketRepositoryError("Invalid repository signature metadata")
        if signature["algorithm"] != "ed25519":
            raise MarketRepositoryError("Unsupported repository signature algorithm")
        key_id = _text(signature["key_id"], "signature key ID", 128)
        key = self._keys.get(key_id)
        if key is None:
            raise MarketRepositoryError(f"Repository signing key is not trusted: {key_id}")
        try:
            value = signature["value"]
            if not isinstance(value, str):
                raise ValueError("signature must be base64 text")
            decoded = base64.b64decode(value, validate=True)
            if len(decoded) != 64:
                raise ValueError("Ed25519 signatures contain 64 bytes")
            key.verify(decoded, canonical_index_bytes(document))
        except (ValueError, InvalidSignature) as exc:
            raise MarketRepositoryError("Repository signature verification failed") from exc

    def _verify_times(self, document: dict) -> None:
        if any(
            not isinstance(document[field], str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", document[field])
            for field in ("issued_at", "expires_at")
        ):
            raise MarketRepositoryError("Repository timestamps must use UTC YYYY-MM-DDTHH:MM:SSZ")
        try:
            issued = datetime.strptime(document["issued_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            expires = datetime.strptime(document["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except (ValueError, TypeError) as exc:
            raise MarketRepositoryError("Repository timestamps must use UTC YYYY-MM-DDTHH:MM:SSZ") from exc
        now = self.clock()
        if now.tzinfo is None:
            raise MarketRepositoryError("Repository clock must be timezone-aware")
        if issued > now:
            raise MarketRepositoryError("Repository index has a future issue time")
        if expires <= issued or expires <= now:
            raise MarketRepositoryError("Repository index has expired or has an invalid validity period")

    def _package(self, item, repository_id: str) -> MarketPackage:
        if not isinstance(item, dict) or item.keys() - _PACKAGE_FIELDS:
            raise MarketRepositoryError("Unsupported package metadata")
        package_id = _text(item.get("package_id"), "package ID")
        version = _text(item.get("version"), "package version", 128)
        if not is_valid_package_id(package_id):
            raise MarketRepositoryError("Invalid publisher-qualified package ID")
        if not is_valid_version(version):
            raise MarketRepositoryError("Invalid package version")
        display_name = _text(item.get("display_name"), "package display name")
        description = item.get("description", "")
        if not isinstance(description, str) or len(description) > 4000:
            raise MarketRepositoryError("Invalid package description")
        digest = _text(item.get("sha256"), "package SHA-256", 64)
        if not is_valid_sha256(digest):
            raise MarketRepositoryError("Package SHA-256 must use 64 lowercase hexadecimal digits")
        size = item.get("size")
        if type(size) is not int or not 0 < size <= self.limits.max_download_bytes:
            raise MarketRepositoryError("Package size is invalid or exceeds the download limit")
        download = _text(item.get("download"), "relative package download", 2048)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~+/-]*", download) or any(
            part in ("", ".", "..") for part in download.split("/")
        ):
            raise MarketRepositoryError("Package downloads must use plain relative URL paths")
        download_url = urljoin(self.repo_url, download)
        self._validate_url(download_url)
        domain = item.get("domain")
        if not isinstance(domain, str) or domain not in PACKAGE_DOMAINS:
            raise MarketRepositoryError("Missing or invalid package domain")
        contributions = item.get("contributions")
        if not isinstance(contributions, list) or not 0 < len(contributions) <= 128:
            raise MarketRepositoryError("Package requires a bounded non-empty contribution list")
        parsed = []
        identifiers = set()
        for contribution in contributions:
            if not isinstance(contribution, dict) or contribution.keys() != {"id", "kind"}:
                raise MarketRepositoryError("Invalid contribution metadata")
            identifier = _text(contribution["id"], "contribution ID", 128)
            kind = contribution["kind"]
            if not is_valid_identifier(identifier) or identifier in identifiers:
                raise MarketRepositoryError("Invalid or duplicate contribution ID")
            if not isinstance(kind, str) or kind not in PLUGIN_KINDS:
                raise MarketRepositoryError("Unknown contribution kind")
            identifiers.add(identifier)
            parsed.append(MarketContribution(kind, PluginSelection(package_id, identifier, version)))
        platforms = item.get("platforms", [])
        if not isinstance(platforms, list) or any(
            not isinstance(value, str) or value not in SUPPORTED_PLATFORMS
            for value in platforms
        ) or len(platforms) != len(set(platforms)):
            raise MarketRepositoryError("Invalid package platform metadata")
        requires = item.get("requires", {})
        if not isinstance(requires, dict) or any(
            not isinstance(value, str) or not value or not key for key, value in requires.items()
        ):
            raise MarketRepositoryError("Invalid package host requirements")
        return MarketPackage(
            repository_id, package_id, version, display_name, description, download_url,
            digest, size, tuple(parsed), domain,
            _text(item["api_version"], "package API version", 32) if "api_version" in item else None,
            tuple(platforms),
            _text(item["host_version"], "package host version", 128) if "host_version" in item else None,
            tuple(sorted(requires.items())),
        )

    def _validate_url(self, url: str) -> None:
        if not isinstance(url, str) or any(
            char.isspace() or not 32 <= ord(char) < 127 for char in url
        ) or "\\" in url or "?" in url or "#" in url:
            raise MarketRepositoryError("Invalid repository URL")
        try:
            parsed = urlsplit(url)
            _ = parsed.port
        except ValueError as exc:
            raise MarketRepositoryError("Invalid repository URL") from exc
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise MarketRepositoryError("Repository URLs cannot contain credentials, queries, or fragments")
        if parsed.scheme == "https":
            return
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if not (parsed.scheme == "http" and self.allow_http and loopback):
            raise MarketRepositoryError("Repository URLs require HTTPS; test HTTP is limited to literal loopback")

    def _transfer(self, url: str, output: BinaryIO, maximum: int, expected_size: int | None = None) -> str:
        request = Request(url, headers={
            "Accept-Encoding": "identity", "User-Agent": "OmniBCI-Plugin-Market/1",
        })
        deadline = time.monotonic() + self.limits.transfer_seconds
        digest = hashlib.sha256()
        size = 0
        try:
            with self._opener.open(request, timeout=self.limits.timeout_seconds) as response:
                if response.geturl() != url:
                    raise MarketRepositoryError("Repository redirects are not allowed")
                if response.status != 200:
                    raise MarketRepositoryError(f"Repository returned HTTP {response.status}")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise MarketRepositoryError("Encoded repository responses are unsupported")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    if len(declared) > 20 or not re.fullmatch(r"[0-9]+", declared) or int(declared) > maximum:
                        raise MarketRepositoryError("Repository response exceeds the size limit")
                    if expected_size is not None and int(declared) != expected_size:
                        raise MarketRepositoryError("Download size differs from signed metadata")
                while True:
                    if time.monotonic() > deadline:
                        raise MarketRepositoryError("Repository transfer exceeded its time limit")
                    block = response.read1(min(64 * 1024, maximum - size + 1))
                    if not block:
                        break
                    size += len(block)
                    if size > maximum:
                        raise MarketRepositoryError("Repository response exceeds the size limit")
                    output.write(block)
                    digest.update(block)
                if declared is not None and size != int(declared):
                    raise MarketRepositoryError("Repository response is truncated")
                if expected_size is not None and size != expected_size:
                    raise MarketRepositoryError("Download size differs from signed metadata")
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise MarketRepositoryError(f"Repository request failed: {exc}") from exc
        return digest.hexdigest()

    def _download(self, package: MarketPackage) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        destination = self.cache_dir / f"{package.sha256}.zip"
        if destination.is_symlink():
            raise MarketRepositoryError("Package cache must not contain symlinks")
        if destination.is_file() and destination.stat().st_size == package.size:
            with destination.open("rb") as reader:
                if hashlib.file_digest(reader, "sha256").hexdigest() == package.sha256:
                    return destination
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="__download-", suffix=".zip", dir=self.cache_dir, delete=False) as writer:
                temporary = Path(writer.name)
                digest = self._transfer(package.download_url, writer, package.size, package.size)
                if digest != package.sha256:
                    raise MarketRepositoryError("Download SHA-256 differs from signed metadata")
                writer.flush()
                os.fsync(writer.fileno())
            temporary.replace(destination)
            return destination
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


@contextmanager
def _market_errors():
    from omnibci.application.plugin_management.market import PluginMarketError
    from omnibci.application.plugin_management.packages import PluginPackageStorageError

    try:
        yield
    except MarketRepositoryError as exc:
        raise PluginMarketError(str(exc)) from exc
    except OSError as exc:
        raise PluginPackageStorageError(f"Cannot access market cache: {exc}") from exc


class MarketRepositoryAdapter:
    def __init__(self, repository: StaticMarketRepository):
        self.repository = repository

    def list_packages(self):
        with _market_errors():
            return self.repository.list_packages()

    def prepare_install(self, package_id, version):
        with _market_errors():
            return self.repository.prepare_install(package_id, version)

    def verify_prepared(self, prepared):
        with _market_errors():
            return self.repository.verify_prepared(prepared)


def configured_market_service(installation, *, environ=None):
    """No configuration means disabled; partial/invalid trust configuration fails closed."""
    from platformdirs import user_cache_dir
    from omnibci.application.plugin_management.market import PluginMarketError
    from omnibci.application.plugin_management.market_service import PluginMarketService

    environment = os.environ if environ is None else environ
    names = ("OMNIBCI_MARKET_URL", "OMNIBCI_MARKET_REPOSITORY_ID", "OMNIBCI_MARKET_TRUSTED_KEYS")
    values = [environment.get(name) for name in names]
    if all(value is None for value in values):
        return None
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise PluginMarketError("Market configuration requires URL, repository ID and trusted public keys together")
    url, repository_id, encoded_keys = values
    if len(url) > 2048 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", repository_id):
        raise PluginMarketError("Configured market URL or repository ID is invalid")
    if len(encoded_keys) > 64 * 1024:
        raise PluginMarketError("Configured market public-key mapping is too large")
    try:
        keys = strict_json_loads(encoded_keys, error_cls=MarketRepositoryError)
    except (ValueError, MarketRepositoryError) as exc:
        raise PluginMarketError("OMNIBCI_MARKET_TRUSTED_KEYS must be a JSON object of key IDs and Base64 public keys") from exc
    if not isinstance(keys, dict) or not 1 <= len(keys) <= 128 or any(not isinstance(value, str) for value in keys.values()):
        raise PluginMarketError("Configure between 1 and 128 Base64 Ed25519 public keys")
    cache = environment.get("OMNIBCI_MARKET_CACHE_DIR")
    if cache is not None and (not isinstance(cache, str) or not cache.strip() or "\0" in cache):
        raise PluginMarketError("OMNIBCI_MARKET_CACHE_DIR must name a local cache directory")
    cache_dir = Path(cache).expanduser() if cache is not None else Path(user_cache_dir("OmniBCI", "OmniBCI")) / "plugin-market"
    limits = MarketLimits(max_download_bytes=min(MarketLimits().max_download_bytes, installation.max_archive_bytes))
    with _market_errors():
        repository = StaticMarketRepository(
            url, keys, cache_dir, expected_repository_id=repository_id, limits=limits,
        )
    return PluginMarketService(MarketRepositoryAdapter(repository), installation)
