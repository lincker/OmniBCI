"""Real signature verification with controlled repository transports and packages."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import threading
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

import pytest
import tomlkit
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omnibci.infrastructure.plugins.market_repository import (
    MarketLimits,
    MarketRepositoryError,
    StaticMarketRepository,
    canonical_index_bytes,
)
from omnibci.infrastructure.plugins.package_installer import PackageInstallError, PluginPackageInstaller

_URL = "https://plugins.example.test/repository/index.json"
_DOWNLOAD = "https://plugins.example.test/repository/packages/device-1.0.0.zip"
_NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def _package_bytes(*, publisher="acme", version="1.0.0"):
    manifest = {
        "manifest_version": 2,
        "package": {"id": "device", "publisher": publisher, "version": version, "api_version": "2"},
        "contributions": [{"id": "reader", "kind": "acquisition", "entrypoint": "driver:Reader"}],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.toml", tomlkit.dumps(manifest))
        archive.writestr("driver.py", "raise AssertionError('Installation must never execute code')\n")
    return buffer.getvalue()


def _index(archive):
    return {
        "schema_version": 1,
        "repository_id": "example.plugins",
        "issued_at": "2026-09-23T00:00:00Z",
        "expires_at": "2026-09-24T00:00:00Z",
        "packages": [{
            "package_id": "acme.device", "version": "1.0.0", "display_name": "Example Reader",
            "description": "Controlled test package.", "download": "packages/device-1.0.0.zip",
            "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive),
            "contributions": [{"id": "reader", "kind": "acquisition"}],
            "api_version": "2", "platforms": ["linux", "darwin", "win32"],
            "host_version": "1.0.0", "requires": {},
        }],
    }


def _signed(index, key, *, key_id="test-key"):
    document = copy.deepcopy(index)
    document["signature"] = {
        "algorithm": "ed25519", "key_id": key_id,
        "value": base64.b64encode(key.sign(canonical_index_bytes(document))).decode("ascii"),
    }
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


class _Response(io.BytesIO):
    def __init__(self, url, data, *, status=200, headers=None, final_url=None):
        super().__init__(data)
        self.url = final_url or url
        self.status = status
        self.headers = {"Content-Length": str(len(data))} if headers is None else headers

    def geturl(self):
        return self.url


class _Opener:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request.full_url, timeout, request.headers))
        response = self.routes[request.full_url]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(request.full_url)
        return _Response(request.full_url, response)


@pytest.fixture
def repository(tmp_path):
    key = Ed25519PrivateKey.generate()
    archive = _package_bytes()
    index = _index(archive)
    installer = PluginPackageInstaller(tmp_path / "installed", host_version="1.0.0", host_dependencies={})
    client = StaticMarketRepository(
        _URL, {"test-key": key.public_key()}, installer, tmp_path / "cache",
        expected_repository_id="example.plugins", clock=lambda: _NOW,
    )
    opener = _Opener({_URL: _signed(index, key), _DOWNLOAD: archive})
    client._opener = opener
    return client, opener, key, archive, index


def test_listing_verifies_real_ed25519_signature_without_downloading_or_installing(repository):
    client, opener, _, _, _ = repository

    packages = client.list_packages()

    assert len(packages) == 1
    package = packages[0]
    assert package.package_id == "acme.device"
    assert package.repository_id == "example.plugins"
    assert package.version == "1.0.0"
    assert package.download_url == _DOWNLOAD
    assert package.contributions[0].selection.package_id == "acme.device"
    assert package.contributions[0].selection.contribution_id == "reader"
    assert package.contributions[0].kind == "acquisition"
    assert package.api_version == "2"
    assert package.host_version == "1.0.0"
    assert [request[0] for request in opener.requests] == [_URL]
    assert not client.cache_dir.exists()
    assert not client.installer.root.exists()


def test_install_downloads_verified_archive_without_executing_or_enabling(repository):
    client, opener, _, archive, _ = repository

    installed = client.install("acme.device", "1.0.0")

    assert installed.package_id == "acme.device"
    assert not installed.already_installed
    assert (installed.location / "manifest.toml").exists()
    assert (client.cache_dir / f"{installed.sha256}.zip").read_bytes() == archive
    assert not list(installed.location.rglob("__pycache__"))
    assert [request[0] for request in opener.requests] == [_URL, _DOWNLOAD]
    assert not list(client.cache_dir.glob("__download-*"))


def test_repeated_install_reverifies_index_and_reuses_only_validated_cache(repository):
    client, opener, _, _, _ = repository
    first = client.install("acme.device", "1.0.0")

    second = client.install("acme.device", "1.0.0")

    assert second.already_installed
    assert second.location == first.location
    assert [request[0] for request in opener.requests] == [_URL, _DOWNLOAD, _URL]


def test_corrupt_cache_is_replaced_only_after_complete_verified_download(repository):
    client, opener, _, archive, index = repository
    client.cache_dir.mkdir()
    cached = client.cache_dir / f"{index['packages'][0]['sha256']}.zip"
    cached.write_bytes(b"0" * len(archive))

    client.install("acme.device", "1.0.0")

    assert cached.read_bytes() == archive
    assert [request[0] for request in opener.requests] == [_URL, _DOWNLOAD]


def test_cache_symlink_is_not_followed(repository, tmp_path):
    client, _, _, _, index = repository
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    client.cache_dir.mkdir()
    (client.cache_dir / f"{index['packages'][0]['sha256']}.zip").symlink_to(outside)

    with pytest.raises(MarketRepositoryError, match="symlinks"):
        client.install("acme.device", "1.0.0")

    assert outside.read_text() == "preserve"
    assert not client.installer.root.exists()


@pytest.mark.parametrize("change", ["payload", "signature", "wrong_key", "unknown_key", "embedded_key", "algorithm"])
def test_untrusted_or_invalid_signatures_are_rejected_before_download(repository, change):
    client, opener, key, _, index = repository
    document = json.loads(_signed(index, key))
    if change == "payload":
        document["packages"][0]["display_name"] = "Tampered"
    elif change == "signature":
        document["signature"]["value"] = base64.b64encode(b"x" * 64).decode()
    elif change == "wrong_key":
        document = json.loads(_signed(index, Ed25519PrivateKey.generate()))
    elif change == "unknown_key":
        document["signature"]["key_id"] = "untrusted"
    elif change == "embedded_key":
        document["signature"]["public_key"] = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    else:
        document["signature"]["algorithm"] = "none"
    opener.routes[_URL] = json.dumps(document).encode()

    with pytest.raises(MarketRepositoryError, match="signature|signing key"):
        client.install("acme.device", "1.0.0")

    assert [request[0] for request in opener.requests] == [_URL]
    assert not client.installer.root.exists()


@pytest.mark.parametrize("field,value", [
    ("issued_at", "2026-09-25T00:00:00Z"),
    ("expires_at", "2026-09-23T12:00:00Z"),
    ("expires_at", "2026-09-22T12:00:00Z"),
    ("issued_at", "2026-09-23"), ("expires_at", 123),
    ("issued_at", "2026-9-23T00:00:00Z"),
    ("repository_id", "other.repository"), ("schema_version", 2), ("schema_version", True),
])
def test_stale_future_wrong_repository_and_unknown_schema_are_rejected(repository, field, value):
    client, opener, key, _, index = repository
    index[field] = value
    opener.routes[_URL] = _signed(index, key)

    with pytest.raises(MarketRepositoryError):
        client.install("acme.device", "1.0.0")

    assert [request[0] for request in opener.requests] == [_URL]
    assert not client.cache_dir.exists()


def test_expired_index_blocks_reinstall_even_when_archive_is_cached(repository):
    client, opener, key, _, index = repository
    client.install("acme.device", "1.0.0")
    index["expires_at"] = "2026-09-23T01:00:00Z"
    opener.routes[_URL] = _signed(index, key)

    with pytest.raises(MarketRepositoryError, match="expired"):
        client.install("acme.device", "1.0.0")

    assert [request[0] for request in opener.requests] == [_URL, _DOWNLOAD, _URL]


@pytest.mark.parametrize("url", [
    "file:///tmp/index.json", "ftp://server/index.json", "http://example.com/index.json",
    "http://127.0.0.1/index.json", "https://user:pass@example.com/index.json",
    "https://example.com/index.json?token=abc", "https://example.com/index.json#fragment",
    "https://example.com/with space", "https://example.com:bad/index.json", "https:///index.json",
    "https://example.com/index.json?", "https://example.com/index.json#",
    "https://example.com\\evil/index.json",
])
def test_default_transport_rejects_non_https_and_unsafe_repository_urls(tmp_path, url):
    with pytest.raises(MarketRepositoryError):
        StaticMarketRepository(
            url, {"key": Ed25519PrivateKey.generate().public_key()},
            PluginPackageInstaller(tmp_path / "installed"), tmp_path / "cache",
        )


@pytest.mark.parametrize("host", ["example.com", "localhost", "192.168.1.1", "0.0.0.0"])
def test_http_exception_never_allows_nonliteral_loopback(tmp_path, host):
    with pytest.raises(MarketRepositoryError, match="literal loopback"):
        StaticMarketRepository(
            f"http://{host}/index.json", {"key": Ed25519PrivateKey.generate().public_key()},
            PluginPackageInstaller(tmp_path / "installed"), tmp_path / "cache", allow_http=True,
        )


@pytest.mark.parametrize("download", [
    "https://evil.example/package.zip", "//evil.example/package.zip", "/package.zip",
    "../package.zip", "packages/../package.zip", "packages//package.zip", "./package.zip",
    "packages/%2e%2e/package.zip", "packages/%252e%252e/package.zip", "packages\\bad.zip",
    "package.zip?redirect=evil", "package.zip#fragment", "file:///tmp/package.zip",
])
def test_package_urls_cannot_escape_repository_directory_or_origin(repository, download):
    client, opener, key, _, index = repository
    index["packages"][0]["download"] = download
    opener.routes[_URL] = _signed(index, key)

    with pytest.raises(MarketRepositoryError, match="relative URL"):
        client.list_packages()


@pytest.mark.parametrize("mutation", ["sha256", "size_small", "size_large", "missing_header"])
def test_download_hash_or_length_mismatch_never_reaches_installer(repository, mutation):
    client, opener, key, archive, index = repository
    if mutation == "sha256":
        index["packages"][0]["sha256"] = "0" * 64
    elif mutation == "size_small":
        index["packages"][0]["size"] -= 1
    elif mutation == "size_large":
        index["packages"][0]["size"] += 1
    else:
        opener.routes[_DOWNLOAD] = lambda url: _Response(url, archive[:-1], headers={})
    opener.routes[_URL] = _signed(index, key)

    with pytest.raises(MarketRepositoryError, match="size|SHA-256"):
        client.install("acme.device", "1.0.0")

    assert not client.installer.root.exists()
    assert not list(client.cache_dir.glob("*.zip"))
    assert not list(client.cache_dir.glob("__download-*"))


@pytest.mark.parametrize("mismatch", ["package_id", "version", "contributions"])
def test_signed_catalog_archive_identity_mismatch_is_rejected_before_publication(repository, mismatch):
    client, opener, key, archive, index = repository
    if mismatch == "package_id":
        index["packages"][0]["package_id"] = "other.device"
    elif mismatch == "version":
        index["packages"][0]["version"] = "2.0.0"
    else:
        index["packages"][0]["contributions"] = [{"id": "reader", "kind": "filter"}]
    opener.routes[_URL] = _signed(index, key)
    package = index["packages"][0]

    with pytest.raises(PackageInstallError, match="does not match|do not match"):
        client.install(package["package_id"], package["version"])

    assert not list(client.installer.root.rglob("manifest.toml"))
    assert not list(client.installer.root.glob("__install-*"))


def test_package_selection_is_exact_and_never_falls_back_to_latest(repository):
    client, opener, _, _, _ = repository

    with pytest.raises(MarketRepositoryError, match="Unknown exact"):
        client.install("acme.device", "2.0.0")

    assert [request[0] for request in opener.requests] == [_URL]


@pytest.mark.parametrize("field,value", [
    ("size", True), ("size", 0), ("size", 10**20), ("sha256", "wrong"),
    ("package_id", "../../escape"), ("version", "../1.0.0"), ("version", "latest"),
    ("display_name", ""), ("description", ["bad"]), ("api_version", 2),
    ("contributions", []), ("contributions", [{"id": "reader", "kind": "unknown"}]),
    ("platforms", ["linux", "linux"]), ("requires", {"numpy": 2}),
])
def test_invalid_signed_package_metadata_is_rejected(repository, field, value):
    client, opener, key, _, index = repository
    index["packages"][0][field] = value
    opener.routes[_URL] = _signed(index, key)

    with pytest.raises(MarketRepositoryError):
        client.list_packages()


def test_duplicate_versions_and_contribution_ids_are_rejected(repository):
    client, opener, key, _, index = repository
    index["packages"].append(copy.deepcopy(index["packages"][0]))
    opener.routes[_URL] = _signed(index, key)
    with pytest.raises(MarketRepositoryError, match="duplicate package"):
        client.list_packages()
    index["packages"].pop()
    index["packages"][0]["contributions"].append({"id": "reader", "kind": "trigger"})
    opener.routes[_URL] = _signed(index, key)
    with pytest.raises(MarketRepositoryError, match="duplicate contribution"):
        client.list_packages()


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b'{"a": 1, "a": 2}', b'{"value": NaN}'])
def test_invalid_ambiguous_or_nonfinite_json_is_rejected(repository, body):
    client, opener, _, _, _ = repository
    opener.routes[_URL] = body
    with pytest.raises(MarketRepositoryError):
        client.list_packages()


def test_unicode_and_different_json_whitespace_use_same_signature(repository):
    client, opener, key, _, index = repository
    index["packages"][0]["display_name"] = "算法 Reader"
    document = json.loads(_signed(index, key))
    opener.routes[_URL] = json.dumps(document, indent=4, ensure_ascii=True).encode()

    assert client.list_packages()[0].display_name == "算法 Reader"


@pytest.mark.parametrize("fault", ["redirect", "status", "encoding", "oversized_header", "truncated", "network"])
def test_transport_failures_are_actionable_and_do_not_install(repository, fault):
    client, opener, _, _, _ = repository
    body = opener.routes[_URL]
    if fault == "redirect":
        response = lambda url: _Response(url, body, final_url="https://evil.example/index.json")
    elif fault == "status":
        response = lambda url: _Response(url, body, status=206)
    elif fault == "encoding":
        response = lambda url: _Response(url, body, headers={"Content-Encoding": "gzip"})
    elif fault == "oversized_header":
        response = lambda url: _Response(url, body, headers={"Content-Length": str(10**10)})
    elif fault == "truncated":
        response = lambda url: _Response(url, body, headers={"Content-Length": str(len(body) + 1)})
    else:
        response = URLError("controlled network failure")
    opener.routes[_URL] = response

    with pytest.raises(MarketRepositoryError):
        client.list_packages()
    assert not client.installer.root.exists()


def test_index_read_limit_is_enforced_without_content_length(repository):
    client, opener, _, _, _ = repository
    body = opener.routes[_URL]
    client.limits = replace(client.limits, max_index_bytes=32)
    opener.routes[_URL] = lambda url: _Response(url, body, headers={})

    with pytest.raises(MarketRepositoryError, match="size limit"):
        client.list_packages()


def test_pathological_content_length_is_rejected_without_integer_conversion(repository):
    client, opener, _, _, _ = repository
    body = opener.routes[_URL]
    opener.routes[_URL] = lambda url: _Response(url, body, headers={"Content-Length": "9" * 10000})

    with pytest.raises(MarketRepositoryError, match="size limit"):
        client.list_packages()


def test_overall_transfer_deadline_is_enforced(repository, monkeypatch):
    client, _, _, _, _ = repository
    moments = iter([0.0, 121.0])
    monkeypatch.setattr("omnibci.infrastructure.plugins.market_repository.time.monotonic", lambda: next(moments))

    with pytest.raises(MarketRepositoryError, match="time limit"):
        client.list_packages()


@pytest.mark.parametrize("keys", [{}, {"test-key": b"short"}, {"test-key": "not-base64"}])
def test_trusted_keys_must_be_explicit_valid_public_keys(tmp_path, keys):
    with pytest.raises(MarketRepositoryError):
        StaticMarketRepository(_URL, keys, PluginPackageInstaller(tmp_path / "installed"), tmp_path / "cache")


def test_base64_operator_public_keys_are_supported(repository, tmp_path):
    original, opener, key, _, _ = repository
    encoded = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    client = StaticMarketRepository(
        _URL, {"test-key": encoded}, original.installer, tmp_path / "cache",
        clock=lambda: _NOW,
    )
    client._opener = opener

    assert len(client.list_packages()) == 1


@pytest.mark.parametrize("fields", [
    {"max_index_bytes": 0}, {"max_download_bytes": -1}, {"max_packages": True},
    {"timeout_seconds": float("nan")}, {"transfer_seconds": float("inf")},
])
def test_resource_limits_require_finite_positive_values(fields):
    with pytest.raises(ValueError):
        MarketLimits(**fields)


@pytest.mark.integration
def test_signed_local_http_repository_installs_and_refuses_real_redirects(tmp_path):
    key = Ed25519PrivateKey.generate()
    archive = _package_bytes()
    signed = _signed(_index(archive), key)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/redirect.json":
                self.send_response(302)
                self.send_header("Location", "/index.json")
                self.end_headers()
                return
            body = {"/index.json": signed, "/packages/device-1.0.0.zip": archive}.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        installer = PluginPackageInstaller(tmp_path / "installed", host_version="1.0.0", host_dependencies={})
        client = StaticMarketRepository(
            base + "/index.json", {"test-key": key.public_key()}, installer,
            tmp_path / "cache", allow_http=True, clock=lambda: _NOW,
        )
        assert client.install("acme.device", "1.0.0").package_id == "acme.device"
        redirected = StaticMarketRepository(
            base + "/redirect.json", {"test-key": key.public_key()}, installer,
            tmp_path / "cache", allow_http=True, clock=lambda: _NOW,
        )
        with pytest.raises(MarketRepositoryError, match="redirects"):
            redirected.list_packages()
        assert requests == ["/index.json", "/packages/device-1.0.0.zip", "/redirect.json"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
