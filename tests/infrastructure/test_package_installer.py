"""Offline package validation and atomic immutable installation contracts."""

from __future__ import annotations

import hashlib
import json
import stat
import struct
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
import tomlkit

from omnibci.infrastructure.plugins.package_installer import (
    InstallationLimits,
    PackageInstallError,
    PluginPackageInstaller,
)
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginRoot


def _manifest(**package_fields):
    return {
        "manifest_version": 2,
        "package": {
            "id": "device", "publisher": "acme", "version": "1.0.0",
            "api_version": "2", **package_fields,
        },
        "contributions": [{
            "id": "reader", "kind": "acquisition", "entrypoint": "driver:Driver",
        }],
    }


def _archive(tmp_path, *, manifest=None, entries=None, name="plugin.zip", compression=zipfile.ZIP_STORED):
    destination = tmp_path / name
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(destination, "w", compression=compression) as archive:
            archive.writestr("manifest.toml", tomlkit.dumps(manifest or _manifest()))
            archive.writestr("driver.py", "raise AssertionError('Plugin code must not execute')\n")
            for path, content in entries or ():
                archive.writestr(path, content)
    return destination


def _installer(tmp_path, **kwargs):
    return PluginPackageInstaller(
        tmp_path / "installed", host_version="1.0.0",
        host_dependencies={"numpy": "2.4.4", "scikit-learn": "1.8.0"},
        platform_name="linux", **kwargs,
    )


def _assert_no_package(root):
    assert not list(root.rglob("manifest.toml"))
    assert not list(root.glob("__install-*"))


def test_valid_archive_is_static_catalog_readable_and_not_activated(tmp_path):
    archive = _archive(tmp_path, entries=[("resources/", ""), ("resources/data.txt", "example")])
    installer = _installer(tmp_path)

    result = installer.install(archive, expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())

    assert result.package_id == "acme.device"
    assert result.version == "1.0.0"
    assert result.location == installer.root / "acme.device" / "1.0.0"
    assert not result.already_installed
    assert (result.location / "resources/data.txt").read_text() == "example"
    assert result.contributions[0].location == result.location
    assert result.contributions[0].source == "user"
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(installer.root, "user", 0)]})
    assert catalog.resolve_selection("acquisition", result.contributions[0].selection) == result.contributions[0]
    assert not list(installer.root.glob("__install-*"))
    assert not list(installer.root.rglob("__pycache__"))
    receipt = json.loads((result.location / ".omnibci-install.json").read_text())
    assert receipt["sha256"] == result.sha256
    assert receipt["files"]["resources/data.txt"] == hashlib.sha256(b"example").hexdigest()


def test_repeated_archive_is_idempotent_and_versions_coexist(tmp_path):
    installer = _installer(tmp_path)
    archive = _archive(tmp_path)
    first = installer.install(archive)
    manifest_mtime = (first.location / "manifest.toml").stat().st_mtime_ns

    repeated = installer.install(archive)
    second = installer.install(_archive(tmp_path, manifest=_manifest(version="2.0.0"), name="v2.zip"))

    assert repeated.already_installed
    assert repeated.location == first.location
    assert (first.location / "manifest.toml").stat().st_mtime_ns == manifest_mtime
    assert second.location != first.location
    assert not second.already_installed


@pytest.mark.parametrize("expectations", [
    {"expected_package_id": "another.package"}, {"expected_version": "2.0.0"},
    {"expected_contributions": {"reader": "trigger"}},
])
@pytest.mark.parametrize("already_installed", [False, True])
def test_signed_market_expectations_checked_before_publication_or_idempotent_return(
    tmp_path, expectations, already_installed,
):
    installer = _installer(tmp_path)
    archive = _archive(tmp_path)
    if already_installed:
        installer.install(archive)

    with pytest.raises(PackageInstallError, match="does not match|do not match"):
        installer.install(archive, **expectations)

    if not already_installed:
        _assert_no_package(installer.root)


def test_same_version_with_different_bytes_is_not_overwritten(tmp_path):
    installer = _installer(tmp_path)
    first = installer.install(_archive(tmp_path))
    original = (first.location / "manifest.toml").read_bytes()
    changed = _archive(tmp_path, name="changed.zip", entries=[("extra.txt", "different archive")])

    with pytest.raises(PackageInstallError, match="conflicting content"):
        installer.install(changed)

    assert (first.location / "manifest.toml").read_bytes() == original
    assert not (first.location / "extra.txt").exists()
    assert not list(installer.root.glob("__install-*"))


@pytest.mark.parametrize("tamper", ["code", "missing", "receipt", "symlink"])
def test_identical_archive_does_not_hide_changed_installation(tmp_path, tamper):
    installer = _installer(tmp_path)
    archive = _archive(tmp_path)
    first = installer.install(archive)
    code = first.location / "driver.py"
    if tamper == "code":
        code.write_text("print('changed')")
    elif tamper == "missing":
        code.unlink()
    elif tamper == "receipt":
        (first.location / ".omnibci-install.json").write_text("{}")
    else:
        code.unlink()
        code.symlink_to(archive)

    with pytest.raises(PackageInstallError, match="conflicting content"):
        installer.install(archive)


@pytest.mark.parametrize("name", [
    "../escape.py", "/absolute.py", "a/../../escape.py", "a/./b.py",
    "a//b.py", "C:/escape.py", "C:escape.py", "\\server\\share\\code.py",
    "a\\b.py", "trailing./code.py", "trailing /code.py", "CON", "aux.py",
    "dir/lpt1.txt", "COM9.log", "com¹.txt", "con .txt", "a:b.py", "a?b.py", "a\x01b.py",
    ".omnibci-install.json", ".OMNIBCI-INSTALL.JSON",
])
def test_unsafe_archive_names_are_rejected_before_extraction(tmp_path, name):
    installer = _installer(tmp_path)
    member = zipfile.ZipInfo(name)
    # Preserve malicious spelling instead of normalizing separators on Windows.
    member.filename = name
    archive = _archive(tmp_path, entries=[(member, "unsafe")])
    with zipfile.ZipFile(archive) as package:
        assert package.infolist()[-1].orig_filename == name

    with pytest.raises(PackageInstallError):
        installer.install(archive)

    _assert_no_package(installer.root)
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize("entries", [
    [("driver.py", "duplicate")],
    [("DRIVER.PY", "case collision")],
    [("data/A.txt", "one"), ("DATA/b.txt", "two")],
    [("data/é.txt", "one"), ("data/e\u0301.txt", "two")],
    [("data", "file"), ("data/child.txt", "child")],
    [("data/child.txt", "child"), ("data", "file")],
    [("data/", ""), ("data/", "")],
])
def test_duplicate_case_unicode_and_file_directory_collisions_are_rejected(tmp_path, entries):
    installer = _installer(tmp_path)

    with pytest.raises(PackageInstallError, match="[Cc]onflicting|[Cc]ollid|[Dd]uplicate"):
        installer.install(_archive(tmp_path, entries=entries))

    _assert_no_package(installer.root)


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFBLK, stat.S_IFCHR])
def test_symlinks_and_special_files_are_rejected(tmp_path, mode):
    member = zipfile.ZipInfo("special")
    member.create_system = 3
    member.external_attr = (mode | 0o777) << 16
    installer = _installer(tmp_path)

    with pytest.raises(PackageInstallError, match="regular file"):
        installer.install(_archive(tmp_path, entries=[(member, "../outside")]))

    _assert_no_package(installer.root)


def test_null_byte_filename_is_rejected(tmp_path):
    archive = _archive(tmp_path, entries=[("badXname", "content")])
    archive.write_bytes(archive.read_bytes().replace(b"badXname", b"bad\x00name"))

    with pytest.raises(PackageInstallError, match="null byte"):
        _installer(tmp_path).install(archive)


@pytest.mark.parametrize("kwargs, entries", [
    ({"max_archive_bytes": 1}, []),
    ({"max_expanded_bytes": 8}, []),
    ({"max_file_bytes": 8}, []),
    ({"max_manifest_bytes": 8}, []),
    ({"max_entries": 1}, []),
    ({"max_compression_ratio": 2}, [("bomb.txt", "0" * 10000)]),
])
def test_resource_limits_reject_oversized_packages(tmp_path, kwargs, entries):
    limits = replace(InstallationLimits(), **kwargs)
    installer = _installer(tmp_path, limits=limits)

    with pytest.raises(PackageInstallError, match="limit|number of entries"):
        installer.install(_archive(tmp_path, entries=entries, compression=zipfile.ZIP_DEFLATED))

    _assert_no_package(installer.root)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_resource_limits_require_positive_integers(value):
    with pytest.raises(ValueError, match="positive integers"):
        InstallationLimits(max_entries=value)


def test_directory_payload_and_unsupported_compression_are_rejected(tmp_path):
    installer = _installer(tmp_path)
    with pytest.raises(PackageInstallError, match="Directory entries"):
        installer.install(_archive(tmp_path, entries=[("data/", "hidden data")]))
    with pytest.raises(PackageInstallError, match="unsupported compressed"):
        installer.install(_archive(tmp_path, compression=zipfile.ZIP_BZIP2))
    _assert_no_package(installer.root)


def test_encrypted_entry_is_rejected_before_password_prompt(tmp_path):
    archive = _archive(tmp_path)
    content = bytearray(archive.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (position := content.find(signature, start)) >= 0:
            content[position + flag_offset] |= 1
            start = position + len(signature)
    archive.write_bytes(content)

    with pytest.raises(PackageInstallError, match="Encrypted"):
        _installer(tmp_path).install(archive)


@pytest.mark.parametrize("expected", ["wrong", "0" * 64])
def test_expected_hash_is_checked_before_extraction(tmp_path, expected):
    installer = _installer(tmp_path)

    with pytest.raises(PackageInstallError, match="SHA-256"):
        installer.install(_archive(tmp_path), expected_sha256=expected)

    _assert_no_package(installer.root)


def test_copy_pins_input_bytes_even_if_original_is_changed(tmp_path, monkeypatch):
    installer = _installer(tmp_path)
    archive = _archive(tmp_path)
    original_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    copy = installer._copy_archive

    def copy_then_replace(source, destination):
        digest = copy(source, destination)
        source.write_bytes(b"not a zip anymore")
        return digest

    monkeypatch.setattr(installer, "_copy_archive", copy_then_replace)

    assert installer.install(archive).sha256 == original_digest


@pytest.mark.parametrize("fields", [
    {"api_version": "999"}, {"platforms": ["win32"]},
    {"host_version": "2.0.0"}, {"host_version": 1},
    {"requires": {"numpy": "99.0"}}, {"requires": {"unlisted-library": "1.0"}},
    {"requires": {"numpy": ">=2.0"}}, {"requires": {"numpy": 2}},
    {"requires": {"scikit-learn": "1.8.0", "scikit_learn": "1.8.0"}},
    {"requires": ["numpy"]}, {"version": "../../elsewhere"},
    {"dependencies": {"numpy": "2.4.4"}},
    {"version": "C:\\elsewhere"}, {"version": "1.0."}, {"version": "latest"},
    {"publisher": "con"},
])
def test_incompatible_or_nonportable_metadata_is_rejected(tmp_path, fields):
    installer = _installer(tmp_path)

    with pytest.raises(PackageInstallError):
        installer.install(_archive(tmp_path, manifest=_manifest(**fields)))

    _assert_no_package(installer.root)


def test_exact_host_dependency_constraints_and_partial_platform_support(tmp_path):
    manifest = _manifest(host_version="1.0.0", requires={"NumPy": "2.4.4", "scikit_learn": "1.8.0"})
    manifest["contributions"].append({
        "id": "trigger", "kind": "trigger", "entrypoint": "driver:Trigger", "platforms": ["win32"],
    })

    result = _installer(tmp_path).install(_archive(tmp_path, manifest=manifest))

    assert len(result.contributions) == 2
    assert result.contributions[0].supports_platform("linux")
    assert not result.contributions[1].supports_platform("linux")


def test_invalid_sibling_contribution_blocks_entire_installation(tmp_path):
    manifest = _manifest()
    manifest["contributions"].append({"id": "broken", "kind": "trigger", "entrypoint": "../unsafe:Driver"})
    installer = _installer(tmp_path)

    with pytest.raises(PackageInstallError, match="manifest"):
        installer.install(_archive(tmp_path, manifest=manifest))

    _assert_no_package(installer.root)


@pytest.mark.parametrize("entrypoint, entries", [
    ("missing:Driver", []),
    ("missing.child:Driver", []),
    ("driver.child:Driver", [("driver/child.py", "pass")]),
    ("driver:Driver", [("driver/__init__.py", "pass")]),
])
def test_missing_or_ambiguous_entrypoints_are_rejected_without_imports(tmp_path, entrypoint, entries):
    manifest = _manifest()
    manifest["contributions"][0]["entrypoint"] = entrypoint

    with pytest.raises(PackageInstallError, match="[Ee]ntrypoint"):
        _installer(tmp_path).install(_archive(tmp_path, manifest=manifest, entries=entries))


def test_nested_namespace_entrypoint_and_package_entrypoint_are_accepted(tmp_path):
    manifest = _manifest()
    manifest["contributions"][0]["entrypoint"] = "nested.driver:Driver"
    archive = _archive(tmp_path, manifest=manifest, entries=[("nested/driver/__init__.py", "raise RuntimeError()")])

    assert _installer(tmp_path).install(archive).contributions[0].entrypoint.module == "nested.driver"


@pytest.mark.parametrize("manifest", [
    {"manifest_version": 1, "package": {"kind": "acquisition"}},
    {"manifest_version": 2, "package": {}, "contributions": []},
    {"manifest_version": 2, "package": {}, "contributions": [{"kind": []}]},
])
def test_old_or_malformed_manifests_are_rejected(tmp_path, manifest):
    with pytest.raises(PackageInstallError):
        _installer(tmp_path).install(_archive(tmp_path, manifest=manifest))


def test_archive_requires_manifest_at_root(tmp_path):
    archive = tmp_path / "wrapper.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("wrapper/manifest.toml", tomlkit.dumps(_manifest()))

    with pytest.raises(PackageInstallError, match="at its root"):
        _installer(tmp_path).install(archive)


def test_broken_crc_and_bad_zip_leave_no_partial_installation(tmp_path):
    installer = _installer(tmp_path)
    archive = _archive(tmp_path)
    archive.write_bytes(archive.read_bytes().replace(b"Plugin code", b"Broken code"))
    with pytest.raises(PackageInstallError):
        installer.install(archive)
    archive.write_bytes(b"invalid ZIP")
    with pytest.raises(PackageInstallError):
        installer.install(archive)
    _assert_no_package(installer.root)


def test_invalid_toml_is_reported_as_installation_error(tmp_path):
    archive = tmp_path / "broken-manifest.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("manifest.toml", "not [ valid TOML")
        writer.writestr("driver.py", "raise RuntimeError()")

    with pytest.raises(PackageInstallError, match="Cannot install"):
        _installer(tmp_path).install(archive)


def test_directory_entry_limit_is_checked_before_zipfile_allocations(tmp_path, monkeypatch):
    archive = _archive(tmp_path, entries=[("extra.txt", "extra")])
    content = bytearray(archive.read_bytes())
    end = content.rfind(b"PK\x05\x06")
    # Lie about the entry count; the preflight must count directory records.
    struct.pack_into("<HH", content, end + 8, 1, 1)
    archive.write_bytes(content)
    installer = _installer(tmp_path, limits=replace(InstallationLimits(), max_entries=2))

    def must_not_allocate(*args, **kwargs):
        raise AssertionError("ZipFile was allocated before directory limits were checked")

    monkeypatch.setattr(zipfile, "ZipFile", must_not_allocate)
    with pytest.raises(PackageInstallError, match="number of entries"):
        installer.install(archive)
    _assert_no_package(installer.root)


@pytest.mark.parametrize("mutation", ["split", "zip64", "count", "prefix"])
def test_nonstandard_or_inconsistent_zip_directories_are_rejected(tmp_path, mutation):
    archive = _archive(tmp_path)
    content = bytearray(archive.read_bytes())
    end = content.rfind(b"PK\x05\x06")
    if mutation == "split":
        struct.pack_into("<H", content, end + 4, 1)
    elif mutation == "zip64":
        struct.pack_into("<HH", content, end + 8, 0xFFFF, 0xFFFF)
    elif mutation == "count":
        struct.pack_into("<HH", content, end + 8, 1, 1)
    else:
        content = b"executable prefix" + content
    archive.write_bytes(content)

    with pytest.raises(PackageInstallError, match="ZIP"):
        _installer(tmp_path).install(archive)


def test_atomic_publish_failure_preserves_existing_versions(tmp_path, monkeypatch):
    installer = _installer(tmp_path)
    old = installer.install(_archive(tmp_path))
    archive = _archive(tmp_path, manifest=_manifest(version="2.0.0"), name="second.zip")
    original_rename = Path.rename

    def fail_publish(path, target):
        if path.name == "package":
            raise OSError("simulated publication failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(PackageInstallError, match="publication failure"):
        installer.install(archive)

    assert (old.location / "manifest.toml").is_file()
    assert not (old.location.parent / "2.0.0").exists()
    assert not list(installer.root.glob("__install-*"))


def test_preexisting_empty_version_is_not_replaced(tmp_path):
    installer = _installer(tmp_path)
    destination = installer.root / "acme.device" / "1.0.0"
    destination.mkdir(parents=True)

    with pytest.raises(PackageInstallError, match="conflicting content"):
        installer.install(_archive(tmp_path))

    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("target", ["package", "version"])
def test_symlink_destinations_are_rejected(tmp_path, target):
    installer = _installer(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    installer.root.mkdir()
    package = installer.root / "acme.device"
    if target == "package":
        package.symlink_to(outside, target_is_directory=True)
    else:
        package.mkdir()
        (package / "1.0.0").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PackageInstallError, match="symlink|real directory"):
        installer.install(_archive(tmp_path))

    assert list(outside.iterdir()) == []


def test_identity_case_collision_is_rejected_portably(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_archive(tmp_path))
    archive = _archive(tmp_path, manifest=_manifest(publisher="ACME"), name="case.zip")

    with pytest.raises(PackageInstallError, match="collides across platforms"):
        installer.install(archive)
