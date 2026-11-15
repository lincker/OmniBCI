"""Package domains and host compatibility hold at discovery and publication."""
from concurrent.futures import ThreadPoolExecutor

import pytest
import tomlkit

from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginManifestError, PluginRoot
from omnibci.infrastructure.plugins.package_installer import PackageInstallError
from tests.infrastructure.test_package_installer import _archive, _installer, _manifest
from tests.infrastructure.test_plugin_contributions import _write_package, _selection, _contribution


@pytest.mark.parametrize("domain", [None, "", "system", "unknown", 1])
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


def test_foreign_kind_rejects_the_entire_package(tmp_path):
    _write_package(tmp_path, contributions=[_contribution(), _contribution("filter", "filter")])
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(tmp_path, "user", 0)]})
    with pytest.raises(PluginManifestError, match="not allowed"):
        catalog.resolve_selection("acquisition", _selection())


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


def test_installed_domain_cannot_change_even_before_enablement(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_archive(tmp_path))
    manifest = _manifest(version="2.0.0", domain="actuation")
    manifest["contributions"][0]["kind"] = "effector"
    with pytest.raises(PackageInstallError, match="domain cannot change"):
        installer.install(_archive(tmp_path, manifest=manifest, name="second.zip"))
    assert not (installer.root / "acme.device" / "2.0.0").exists()


def test_concurrent_first_versions_cannot_publish_different_domains(tmp_path):
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
    assert sum(isinstance(result, PackageInstallError) for result in results) == 1
    assert len(list((tmp_path / "installed" / "acme.device").iterdir())) == 1


def test_signed_domain_must_match_archive_domain(tmp_path):
    with pytest.raises(PackageInstallError, match="signed repository"):
        _installer(tmp_path).install(_archive(tmp_path), expected_domain="processing")


@pytest.mark.parametrize("field,value", [("api_version", "1"), ("version", ">=1"), ("version", "*")])
def test_catalog_rejects_legacy_api_and_non_exact_version(tmp_path, field, value):
    _write_package(tmp_path, package_fields={field: value})
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(tmp_path, "user", 0)]})
    assert catalog.list_contributions("acquisition") == ()
