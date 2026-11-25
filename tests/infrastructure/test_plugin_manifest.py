"""Static manifest validation, decoder requirements, v1 declarations, and package domains."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
from pathlib import Path

import pytest
import tomlkit

from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.infrastructure.plugin_management.catalog import (
    CatalogConfigurationAdapter,
    PluginCatalog,
    PluginRoot,
    vendor_plugins_dir,
)
from omnibci.infrastructure.plugin_management.installer import PackageInstallError, PluginPackageInstaller
from omnibci.infrastructure.plugin_management.manifest import PluginManifestError, parse_manifest
from tests.infrastructure.test_package_installer import _archive, _installer, _manifest
from tests.infrastructure.test_plugin_contributions import _contribution, _selection, _write_package


def _static_package(root):
    (root / "manifest.toml").write_text(tomlkit.dumps({
        "manifest_version": 1,
        "package": {"id": "device", "publisher": "test", "version": "1.0.0",
                    "api_version": "1", "domain": "acquisition"},
        "contributions": [
            {"id": "source", "kind": "acquisition", "entrypoint": "driver:Source"},
            {"id": "output", "kind": "trigger", "entrypoint": "driver:Output",
             "configuration": {"schema": "output.json", "schema_version": 1}},
        ],
    }))
    (root / "output.json").write_text(json.dumps({
        "type": "object", "properties": {}, "additionalProperties": False,
    }))
    (root / "driver.py").write_text("raise AssertionError('Static validation imported code')\n")


def test_static_reader_returns_the_same_builtin_descriptors_as_catalog():
    manifest = importlib.import_module("omnibci.infrastructure.plugin_management.manifest")
    root = vendor_plugins_dir()
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(root, "builtin", 100)]})
    descriptors, errors = catalog._scan("acquisition", all_categories=True)
    assert not errors
    for location in {item.location for item in descriptors}:
        expected = tuple(item for item in descriptors if item.location == location)
        assert manifest.parse_manifest(manifest.read_manifest(location), location, source="builtin", precedence=100,
                                       host_version=catalog.host_version) == expected


def test_installation_reads_a_multi_contribution_manifest_once(tmp_path, monkeypatch):
    _static_package(tmp_path)
    original = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path.name == "manifest.toml":
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    installer = PluginPackageInstaller(tmp_path / "installed", host_version="1.0.0")
    descriptors = installer.validate_package(tmp_path)
    assert [item.contribution_id for item in descriptors] == ["source", "output"]
    assert reads == [tmp_path / "manifest.toml"]


def test_invalid_sibling_keeps_identity_for_no_fallback_resolution(tmp_path):
    manifest = importlib.import_module("omnibci.infrastructure.plugin_management.manifest")
    _static_package(tmp_path)
    (tmp_path / "output.json").write_text('{"type":"object"}')
    with pytest.raises(manifest.PluginManifestError) as caught:
        manifest.parse_manifest(manifest.read_manifest(tmp_path), tmp_path, source="user", precedence=0, host_version="1.0.0")
    assert caught.value.package_id == "test.device"
    assert caught.value.version == "1.0.0"


def _req_package(tmp_path, *, kind="decoder", **fields):
    document = {"manifest_version": 1, "package": {
        "id": "example", "publisher": "test", "version": "1", "api_version": "1", "domain": "processing",
    }, "contributions": [{"id": "default", "kind": kind, "entrypoint": "decoder:Decoder", **fields}]}
    (tmp_path / "manifest.toml").write_text(tomlkit.dumps(document), encoding="utf-8")
    return PluginCatalog({kind: lambda: [PluginRoot(tmp_path, "user", 0)]})


def test_catalog_reads_requirements_without_importing_plugin_code(tmp_path):
    (tmp_path / "decoder.py").write_text("raise RuntimeError('Do not execute')\n", encoding="utf-8")
    catalog = _req_package(tmp_path, channel_requirements={
        "signal_type": "eeg", "min_count": 2, "required_names": ["Oz"], "default_names": ["O2", "Oz"],
    })
    selected = PluginSelection("test.example", "default", "1")
    descriptor = catalog.resolve_selection("decoder", selected)
    adapted = CatalogConfigurationAdapter(catalog).get("decoder", selected)
    assert adapted.channel_requirements == descriptor.channel_requirements
    assert adapted.channel_requirements.default_names == ("O2", "Oz")


@pytest.mark.parametrize("fields", [
    {},
    {"signal_input_contract": 1, "channel_requirements": {}},
    {"signal_input_contract": 2, "channel_requirements": {}},
    {"signal_input_contract": True, "channel_requirements": {}},
    {"channel_requirements": []},
    {"channel_requirements": {"unknown": 1}},
    {"channel_requirements": {"min_count": True}},
    {"channel_requirements": {"min_count": 2, "default_names": ["Oz"]}},
    {"channel_requirements": {"required_names": ["Oz", "Oz"]}},
])
def test_invalid_or_retired_declarations_reject_package(tmp_path, fields):
    catalog = _req_package(tmp_path, **fields)
    with pytest.raises(PluginManifestError, match="channel_requirements|signal_input_contract"):
        catalog.resolve_selection("decoder", PluginSelection("test.example", "default", "1"))


@pytest.mark.parametrize("kind", ["acquisition", "filter", "epoch_detector", "effector"])
def test_requirements_are_decoder_only(tmp_path, kind):
    catalog = _req_package(tmp_path, kind=kind, channel_requirements={})
    with pytest.raises(PluginManifestError, match="channel_requirements"):
        catalog.resolve_selection(kind, PluginSelection("test.example", "default", "1"))


def test_decoder_without_requirements_is_rejected_during_discovery(tmp_path):
    catalog = _req_package(tmp_path)
    assert catalog.list_contributions("decoder") == ()
    with pytest.raises(PluginManifestError, match="channel_requirements"):
        catalog.resolve_selection("decoder", PluginSelection("test.example", "default", "1"))


def _parse(document, root):
    return parse_manifest(document, root, source="user", precedence=0, host_version="1.0.0")


def test_one_package_can_declare_roles_from_different_hosts_without_import(tmp_path):
    document = _manifest()
    document["contributions"].append({"id": "filter", "kind": "filter", "entrypoint": "driver:Filter"})
    (tmp_path / "driver.py").write_text("raise AssertionError('Static validation imported code')\n")
    descriptors = _parse(document, tmp_path)
    assert {item.category for item in descriptors} == {"acquisition", "filter"}
    assert all(item.package.domain == "acquisition" for item in descriptors)
    installed = _installer(tmp_path).install(_archive(tmp_path, manifest=document))
    assert {item.category for item in installed.contributions} == {"acquisition", "filter"}
    assert not list(installed.location.rglob("__pycache__"))


@pytest.mark.parametrize("kind", ["decoder", "filter", "epoch_detector"])
def test_processing_has_one_contract_and_decoder_always_declares_requirements(tmp_path, kind):
    document = _manifest(domain="processing")
    document["contributions"][0]["kind"] = kind
    if kind == "decoder":
        document["contributions"][0]["channel_requirements"] = {"min_count": 1}
    descriptor, = _parse(document, tmp_path)
    assert not hasattr(descriptor, "signal_input_contract")
    assert (descriptor.channel_requirements is not None) == (kind == "decoder")


@pytest.mark.parametrize("boundary", ["catalog", "installer"])
@pytest.mark.parametrize("section,field,value", [
    ("root", "unrecognized", "value"),
    ("package", "source", "builtin"),
    ("package", "location", "/outside"),
    ("contribution", "context", {}),
    ("contribution", "signal_input_contract", 1),
])
def test_discovery_and_installation_reject_undeclared_manifest_fields(tmp_path, section, field, value, boundary):
    document = _manifest()
    target = document if section == "root" else document["package"] if section == "package" else document["contributions"][0]
    target[field] = value
    with pytest.raises((PluginManifestError, PackageInstallError), match=field):
        if boundary == "catalog":
            _parse(document, tmp_path)
        else:
            _installer(tmp_path).install(_archive(tmp_path, manifest=document))


def test_market_lists_contribution_kinds_independently_of_package_domain(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from tests.infrastructure.test_market_repository import (
        StaticMarketRepository,
        _NOW,
        _Opener,
        _URL,
        _index,
        _package_bytes,
        _signed,
    )

    key = Ed25519PrivateKey.generate()
    index = _index(_package_bytes())
    index["packages"][0]["contributions"].append({"id": "filter", "kind": "filter"})
    client = StaticMarketRepository(_URL, {"test-key": key.public_key()}, tmp_path / "cache",
                                    expected_repository_id="example.plugins", clock=lambda: _NOW)
    opener = _Opener({_URL: _signed(index, key)})
    client._opener = opener
    package, = client.list_packages()
    assert package.domain == "acquisition"
    assert {item.kind for item in package.contributions} == {"acquisition", "filter"}
    assert len(opener.requests) == 1


def test_entrypoint_does_not_translate_a_source_filename_into_a_module(tmp_path):
    document = _manifest()
    document["contributions"][0]["entrypoint"] = "driver.py:Driver"
    with pytest.raises(PackageInstallError, match="Entrypoint"):
        _installer(tmp_path).install(_archive(tmp_path, manifest=document))


@pytest.mark.parametrize("domain", [None, "", "engine", "unknown", 1])
def test_manifest_requires_a_valid_package_domain(tmp_path, domain):
    location = _write_package(tmp_path)
    manifest = tomlkit.loads((location / "manifest.toml").read_text())
    if domain is None:
        del manifest["package"]["domain"]
    else:
        manifest["package"]["domain"] = domain
    (location / "manifest.toml").write_text(tomlkit.dumps(manifest))
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(tmp_path, "user", 0)]})
    with pytest.raises(PluginManifestError, match="domain"):
        catalog.resolve_selection("acquisition", _selection())


def test_acquisition_package_trigger_is_still_a_stimulation_contribution(tmp_path):
    _write_package(tmp_path, contributions=[_contribution(), _contribution("trigger", "trigger")])
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(tmp_path, "user", 0)]})
    trigger = catalog.resolve_selection("trigger", _selection("trigger"))
    assert trigger.package.domain == "acquisition"
    assert trigger.category == "trigger"
    assert catalog.resolve_selection("acquisition", _selection()).category == "acquisition"


@pytest.mark.parametrize("requirement", [">=1,<2", "==1.0", ">0.9,<=1.0.0", ">=1.0.0"])
def test_engine_ranges_match_in_installer_and_catalog(tmp_path, requirement):
    installer = _installer(tmp_path)
    installed = installer.install(_archive(tmp_path, manifest=_manifest(engines={"omnibci": requirement})))
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(installed.location, "user", 0)]}, host_version="1.0.0")
    assert catalog.resolve_selection("acquisition", _selection()).package.engines == {"omnibci": requirement}


@pytest.mark.parametrize("requirement", [">1", ">=2", "<1", "<=0.9", "==2", "^1", "~1", "1.*", "1.0.0", ">=1 || <2", ">=1,", ""])
def test_mismatched_and_unsupported_engines_are_rejected_consistently(tmp_path, requirement):
    with pytest.raises(PackageInstallError, match="engines"):
        _installer(tmp_path).install(_archive(tmp_path, manifest=_manifest(engines={"omnibci": requirement})))
    _write_package(tmp_path / "catalog", package_fields={"engines": {"omnibci": requirement}})
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(tmp_path / "catalog", "user", 0)]}, host_version="1.0.0")
    with pytest.raises(PluginManifestError, match="engines"):
        catalog.resolve_selection("acquisition", _selection())


def test_catalog_rechecks_host_compatibility_after_host_changes(tmp_path):
    installed = _installer(tmp_path).install(_archive(tmp_path, manifest=_manifest(engines={"omnibci": ">=1,<2"})))
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(installed.location, "user", 0)]}, host_version="2.0.0")
    with pytest.raises(PluginManifestError, match="engines"):
        catalog.resolve_selection("acquisition", _selection())


def test_installed_and_concurrent_versions_may_have_different_classifications(tmp_path):
    first = _archive(tmp_path, name="first.zip")
    manifest = _manifest(version="2.0.0", domain="actuation")
    manifest["contributions"][0]["kind"] = "effector"
    second = _archive(tmp_path, manifest=manifest, name="second.zip")

    def install(archive):
        try:
            return _installer(tmp_path).install(archive)
        except PackageInstallError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(install, [first, second]))
    assert not any(isinstance(result, PackageInstallError) for result in results)
    assert {result.contributions[0].package.domain for result in results} == {"acquisition", "actuation"}
    assert len(list((tmp_path / "installed" / "acme.device").iterdir())) == 2


def test_signed_domain_must_match_archive_domain(tmp_path):
    with pytest.raises(PackageInstallError, match="signed repository"):
        _installer(tmp_path).install(_archive(tmp_path), expected_domain="processing")


@pytest.mark.parametrize("field,value", [("api_version", "2"), ("version", ">=1"), ("version", "*")])
def test_catalog_rejects_legacy_api_and_non_exact_version(tmp_path, field, value):
    _write_package(tmp_path, package_fields={field: value})
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(tmp_path, "user", 0)]})
    assert catalog.list_contributions("acquisition") == ()
