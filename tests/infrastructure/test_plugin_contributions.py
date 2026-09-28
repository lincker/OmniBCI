"""Package identity, contribution discovery and exact selection contracts."""
from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.plugins.descriptors import PluginSelection
from omnibci.infrastructure.plugins.loader import PluginLoader
from omnibci.infrastructure.plugins.plugin_catalog import (
    AmbiguousPluginError,
    PluginCatalog,
    PluginManifestError,
    PluginNotFoundError,
    PluginRoot,
)


def _contribution(identifier="reader", kind="acquisition", **fields):
    return {
        "id": identifier, "kind": kind, "entrypoint": "driver:Driver", **fields,
    }


def _write_package(root: Path, directory="device", *, publisher="acme",
                   version="1.0.0", contributions=None, package_fields=None):
    location = root / directory
    location.mkdir(parents=True)
    package = {
        "id": "device", "publisher": publisher, "version": version,
        "api_version": "1", **(package_fields or {}),
    }
    document = {
        "manifest_version": 2, "package": package,
        "contributions": contributions if contributions is not None else [_contribution()],
    }
    (location / "manifest.toml").write_text(tomlkit.dumps(document), encoding="utf-8")
    return location


def _catalog(*roots, category="acquisition"):
    return PluginCatalog({category: lambda: list(roots)})


def _selection(contribution="reader", version="1.0.0", publisher="acme"):
    return PluginSelection(f"{publisher}.device", contribution, version)


def test_cross_category_contributions_share_package_and_discovery_is_static(tmp_path):
    location = _write_package(tmp_path, contributions=[
        _contribution(display_name="Reader", params={"channels": 8}),
        _contribution("trigger", "trigger", plugin_type="legacy_trigger"),
    ], package_fields={"source": "builtin"})
    marker = tmp_path / "executed"
    (location / "driver.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n",
        encoding="utf-8",
    )
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    reader = catalog.resolve_selection("acquisition", _selection())
    trigger = catalog.resolve_selection("trigger", _selection("trigger"))

    assert reader.package == trigger.package
    assert reader.identifier == trigger.identifier == "acme.device"
    assert reader.contribution_id == "reader"
    assert reader.selection == _selection()
    assert reader.plugin_type == "reader"
    assert reader.display_name == "Reader"
    assert reader.params == {"channels": 8}
    assert trigger.plugin_type == "legacy_trigger"
    assert trigger.source == "user"
    assert trigger.location == location.resolve()
    assert catalog.scan("trigger") == (trigger,)
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["trigger", "filter", "epoch_detector"])
def test_new_categories_are_discovered_outside_their_legacy_root(tmp_path, kind):
    _write_package(tmp_path, contributions=[_contribution("component", kind)])
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    assert catalog.resolve_selection(kind, _selection("component")).category == kind
    assert len(catalog.list_contributions(kind)) == 1


def test_distinct_contributions_and_publishers_do_not_collapse_by_alias(tmp_path):
    for publisher in ("alice", "bob"):
        _write_package(tmp_path, publisher, publisher=publisher, contributions=[
            _contribution("first", plugin_type="shared_alias"),
            _contribution("second", plugin_type="shared_alias"),
        ])
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    descriptors = catalog.list_contributions("acquisition")

    assert {item.selection for item in descriptors} == {
        _selection(name, publisher=publisher)
        for publisher in ("alice", "bob") for name in ("first", "second")
    }
    assert catalog.resolve_selection(
        "acquisition", _selection("first", publisher="alice"),
    ).publisher == "alice"


def test_versions_coexist_and_exact_selection_never_substitutes(tmp_path):
    for version in ("1.0.0", "2.0.0"):
        _write_package(tmp_path, version, version=version)
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    assert {item.version for item in catalog.list_contributions("acquisition")} == {
        "1.0.0", "2.0.0",
    }
    assert catalog.resolve_selection("acquisition", _selection()).version == "1.0.0"
    for version in ("3.0.0", None):
        with pytest.raises(PluginNotFoundError):
            catalog.resolve_selection("acquisition", _selection(version=version))
    with pytest.raises(PluginNotFoundError):
        catalog.resolve_selection("trigger", _selection())


@pytest.mark.parametrize("precedence", [0, 100])
def test_duplicate_package_version_is_ambiguous_across_locations(tmp_path, precedence):
    user, builtin = tmp_path / "user", tmp_path / "builtin"
    _write_package(user)
    _write_package(builtin)
    catalog = _catalog(PluginRoot(user, "user", 0), PluginRoot(builtin, "builtin", precedence))

    with pytest.raises(AmbiguousPluginError):
        catalog.resolve_selection("acquisition", _selection())
    assert catalog.list_contributions("acquisition") == ()


def test_overlapping_roots_do_not_duplicate_or_relabel_builtin_packages(tmp_path):
    builtin = tmp_path / "acquisition"
    location = _write_package(builtin)
    catalog = PluginCatalog({
        "acquisition": lambda: [PluginRoot(builtin, "builtin", 100)],
        "effector": lambda: [PluginRoot(tmp_path, "user", 0)],
    })

    descriptors = catalog.list_contributions("acquisition")

    assert len(descriptors) == 1
    assert descriptors[0].location == location.resolve()
    assert descriptors[0].source == "builtin"
    assert catalog.resolve_selection("acquisition", _selection()) == descriptors[0]


@pytest.mark.parametrize("precedence", [0, 200])
def test_broken_same_package_version_blocks_exact_fallback(tmp_path, precedence):
    good, broken = tmp_path / "good", tmp_path / "broken"
    _write_package(good)
    _write_package(broken, contributions=[
        _contribution(), _contribution("trigger", "trigger", entrypoint="../bad:Driver"),
    ])
    catalog = _catalog(PluginRoot(good, "builtin", 100), PluginRoot(broken, "user", precedence))

    with pytest.raises(PluginManifestError):
        catalog.resolve_selection("acquisition", _selection())
    assert catalog.list_contributions("acquisition") == ()


def test_broken_unknown_version_blocks_same_package(tmp_path):
    _write_package(tmp_path, "good")
    location = _write_package(tmp_path, "broken")
    manifest = location / "manifest.toml"
    document = tomlkit.loads(manifest.read_text(encoding="utf-8"))
    del document["package"]["version"]
    manifest.write_text(tomlkit.dumps(document), encoding="utf-8")
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    with pytest.raises(PluginManifestError):
        catalog.resolve_selection("acquisition", _selection())


def test_other_broken_versions_and_publishers_do_not_block_exact_selection(tmp_path):
    good = _write_package(tmp_path, "good")
    for directory, version, publisher in (("old", "0.5.0", "acme"), ("other", "1.0.0", "bob")):
        _write_package(tmp_path, directory, version=version, publisher=publisher,
                       contributions=[_contribution(entrypoint="../bad:Driver")])
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    assert catalog.resolve_selection("acquisition", _selection()).location == good.resolve()
    assert len(catalog.list_contributions("acquisition")) == 1


@pytest.mark.parametrize("case", [
    "duplicate", "unknown_api", "missing_entrypoint", "bad_id",
    "unknown_kind", "empty_contributions", "invalid_controls",
])
def test_invalid_package_is_rejected_atomically(tmp_path, case):
    contributions = [_contribution()]
    fields = {}
    if case == "duplicate":
        contributions.append(_contribution("reader", "trigger"))
    elif case == "unknown_api":
        fields["api_version"] = "99"
    elif case == "missing_entrypoint":
        contributions.append({"id": "trigger", "kind": "trigger"})
    elif case == "bad_id":
        contributions.append(_contribution("../trigger", "trigger"))
    elif case == "unknown_kind":
        contributions.append(_contribution("unknown", "unknown"))
    elif case == "empty_contributions":
        contributions = []
    else:
        contributions.append(_contribution("trigger", "trigger", controls={"port": 1}))
    _write_package(tmp_path, contributions=contributions, package_fields=fields)
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    assert catalog.scan("acquisition") == ()
    with pytest.raises(PluginManifestError):
        catalog.resolve_selection("acquisition", _selection())


def test_v1_normalizes_to_default_and_keeps_legacy_resolution(tmp_path):
    location = tmp_path / "legacy"
    location.mkdir()
    (location / "manifest.toml").write_text(tomlkit.dumps({"package": {
        "id": "device", "publisher": "acme", "kind": "acquisition",
        "plugin_type": "legacy_reader", "entrypoint": "driver:Driver",
    }}), encoding="utf-8")
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    legacy = catalog.resolve_legacy("acquisition", "legacy_reader")

    assert legacy == catalog.resolve("acquisition", "legacy_reader")
    assert legacy.contribution_id == "default"
    assert legacy.selection == _selection("default", version=None)
    assert catalog.resolve_selection("acquisition", legacy.selection) == legacy
    assert catalog.scan("trigger") == ()


def test_v2_effector_can_be_loaded_from_exact_selection(tmp_path):
    location = _write_package(tmp_path, contributions=[_contribution("output", "effector")])
    (location / "driver.py").write_text(
        "from omnibci.domain.ports.effector import Effector\n"
        "class Driver(Effector):\n"
        "    plugin_type = 'output'\n"
        "    def setup(self): pass\n"
        "    def teardown(self): pass\n"
        "    def play(self, command): self.command = command\n",
        encoding="utf-8",
    )
    descriptor = _catalog(PluginRoot(tmp_path, "user", 0)).resolve_selection(
        "effector", _selection("output"),
    )

    instance = PluginLoader.instantiate(descriptor, Effector)
    instance.play({"target_id": 2})

    assert isinstance(instance, Effector)
    assert instance.command == {"target_id": 2}


def test_v2_aliases_remain_available_to_legacy_callers(tmp_path):
    _write_package(tmp_path, contributions=[
        _contribution(plugin_type="legacy_reader"),
        _contribution("trigger", "trigger", plugin_type="legacy_trigger"),
    ])
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    for category, alias, contribution in (
        ("acquisition", "legacy_reader", "reader"),
        ("trigger", "legacy_trigger", "trigger"),
    ):
        selected = catalog.resolve_selection(category, _selection(contribution))
        assert catalog.resolve_legacy(category, alias) == selected
        assert catalog.resolve(category, alias) == selected
        assert catalog.list(category) == (selected,)


def test_exact_builtin_selection_ignores_another_publishers_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path))
    catalog = PluginCatalog()
    builtin = catalog.resolve_legacy("effector", "logger")
    location = _write_package(tmp_path / "effectors", contributions=[
        _contribution("output", "effector", plugin_type="logger"),
    ])

    assert catalog.resolve_legacy("effector", "logger").location == location.resolve()
    assert catalog.resolve_selection("effector", builtin.selection) == builtin

    (location / "manifest.toml").unlink()

    assert catalog.resolve_legacy("effector", "logger") == builtin
    with pytest.raises(PluginNotFoundError):
        catalog.resolve_selection("effector", _selection("output"))


def test_duplicate_package_version_is_ambiguous_across_categories(tmp_path):
    _write_package(tmp_path, "source")
    _write_package(tmp_path, "trigger", contributions=[_contribution("trigger", "trigger")])
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    for category, contribution in (("acquisition", "reader"), ("trigger", "trigger")):
        with pytest.raises(AmbiguousPluginError):
            catalog.resolve_selection(category, _selection(contribution))
        assert catalog.list_contributions(category) == ()


@pytest.mark.parametrize("manifest_version", [3, True, "2"])
def test_unknown_or_incorrectly_typed_manifest_version_is_rejected(tmp_path, manifest_version):
    location = _write_package(tmp_path)
    manifest = location / "manifest.toml"
    document = tomlkit.loads(manifest.read_text(encoding="utf-8"))
    document["manifest_version"] = manifest_version
    manifest.write_text(tomlkit.dumps(document), encoding="utf-8")
    catalog = _catalog(PluginRoot(tmp_path, "user", 0))

    assert catalog.scan("acquisition") == ()
    with pytest.raises(PluginManifestError):
        catalog.resolve_selection("acquisition", _selection())
