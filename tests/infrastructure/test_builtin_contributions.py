"""The bundled manifests describe executable, independently selected capabilities."""
import sys
from dataclasses import replace
from pathlib import Path

from tests.plugin_configuration_helpers import builtin_selection

import pytest
import tomlkit

from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.domain.ports.acquisition import SignalSource, TriggerEmitter
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter
from omnibci.domain.ports.effector import Effector
from omnibci.domain.ports.paradigm import Paradigm
from omnibci.infrastructure.plugin_management.loader import PluginLoadError, PluginLoader
from omnibci.infrastructure.plugin_management.catalog import (
    PluginCatalog, PluginRoot, vendor_plugins_dir,
)
from omnibci.infrastructure.plugin_management.manifest import PluginManifestError


CAPABILITIES = [
    ("acquisition", "file_reader", SignalSource),
    ("acquisition", "neuroscan_scan45_reader", SignalSource),
    ("acquisition", "neuroscan_curry8_reader", SignalSource),
    ("trigger", "neuroscan_scan45_trigger", TriggerEmitter),
    ("trigger", "neuroscan_curry8_trigger", TriggerEmitter),
    ("decoder", "THUBCI_SSVEP_classic40CCA", Decoder),
    ("decoder", "THUBCI_SSVEP_classic40FBCCA", Decoder),
    ("epoch_detector", "DEFAULT_EPOCH_DETECTOR", EpochDetector),
    ("filter", "BASELINE_CORRECTION", Filter),
    ("paradigm", "STIMULATION_THUBCI_SSVEP_classic40", Paradigm),
    ("effector", "logger", Effector),
]


@pytest.fixture
def catalog():
    return PluginCatalog({
        "acquisition": lambda: [PluginRoot(vendor_plugins_dir(), "builtin", 100)],
    })


@pytest.mark.parametrize("kind,alias,port", CAPABILITIES)
def test_builtin_exact_selection_loads_its_domain_port(catalog, monkeypatch, kind, alias, port):
    descriptor = catalog.resolve_selection(kind, builtin_selection(kind, alias))
    assert descriptor.package.manifest_version == 1
    assert descriptor in catalog.list_contributions(kind)
    assert catalog.resolve_selection(kind, descriptor.selection) == descriptor
    if kind == "trigger":
        # Loading a class requires no Windows device connection.
        monkeypatch.setattr(sys, "platform", "win32")
    assert issubclass(PluginLoader.load_entrypoint(descriptor, port), port)


@pytest.mark.parametrize("device", ["scan45", "curry8"])
def test_trigger_platform_is_independent_from_acquisition(catalog, monkeypatch, device):
    trigger = catalog.resolve_selection("trigger", builtin_selection("trigger", f"neuroscan_{device}_trigger"))
    reader = catalog.resolve_selection("acquisition", builtin_selection("acquisition", f"neuroscan_{device}_reader"))
    assert trigger.package == reader.package
    assert trigger.supports_platform("win32")
    for host in ("darwin", "linux"):
        assert reader.supports_platform(host)
        assert not trigger.supports_platform(host)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(PluginLoader, "_prepare_module", lambda *a: pytest.fail("imported"))
    with pytest.raises(PluginLoadError, match="not supported on linux"):
        PluginLoader.load_entrypoint(trigger, TriggerEmitter)


def test_package_and_contribution_platforms_both_apply(catalog):
    trigger = catalog.resolve_selection("trigger", builtin_selection("trigger", "neuroscan_scan45_trigger"))
    restricted = replace(trigger, package=replace(trigger.package, platforms=("linux",)))
    assert not any(restricted.supports_platform(host) for host in ("darwin", "linux", "win32"))


@pytest.mark.parametrize("platforms", [[], "win32", ["windows"], ["linux", "linux"], [1]])
@pytest.mark.parametrize("section", ["package", "contribution"])
def test_malformed_platforms_reject_package(tmp_path, platforms, section):
    package = {"id": "example", "publisher": "test", "version": "1", "domain": "acquisition", "api_version": "1"}
    contribution = {"id": "reader", "kind": "acquisition", "entrypoint": "reader:Reader"}
    (package if section == "package" else contribution)["platforms"] = platforms
    (tmp_path / "manifest.toml").write_text(tomlkit.dumps({
        "manifest_version": 1, "package": package, "contributions": [contribution],
    }), encoding="utf-8")
    catalog = PluginCatalog({"acquisition": lambda: [PluginRoot(tmp_path, "user", 0)]})
    with pytest.raises(PluginManifestError, match="platforms"):
        catalog.resolve_selection("acquisition", PluginSelection("test.example", "reader", "1"))


def test_selected_paradigm_can_read_its_pack_without_executing_package_initializers(catalog):
    descriptor = catalog.resolve_selection("paradigm", builtin_selection("paradigm", "STIMULATION_THUBCI_SSVEP_classic40"))
    implementation = PluginLoader.load_entrypoint(descriptor, Paradigm)
    path = sys.modules[implementation.__module__]._frame_pack_path(descriptor.location)
    assert Path(path) == descriptor.location / "resource" / "classic40_ssvep.ofp"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("kind,subpath", [
    ("filter", "algorithms/filters"), ("epoch_detector", "algorithms/epoch"),
])
def test_category_root_can_itself_be_a_package(kind, subpath):
    root = vendor_plugins_dir() / subpath
    catalog = PluginCatalog({kind: lambda: [PluginRoot(root, "builtin", 100)]})
    assert len(catalog.list_contributions(kind)) == 1
