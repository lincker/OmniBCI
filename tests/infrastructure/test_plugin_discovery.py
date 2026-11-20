"""Runtime descriptor, precedence and selected-entrypoint loading tests."""

from __future__ import annotations

import sys
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
from textwrap import dedent

import pytest
import tomlkit
from omnibci.sdk import PluginContext
from omnibci.application.plugin_identity import PluginSelection

from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.plugins.descriptors import PluginEntrypoint
from omnibci.infrastructure.plugins.loader import PluginLoadError, PluginLoader
from omnibci.infrastructure.plugins.plugin_catalog import (
    AmbiguousPluginError,
    MissingEntrypointError,
    PluginRoot,
    PluginManifestError,
    PluginCatalog,
)


def _write_effector_manifest(
    root: Path,
    directory: str,
    *,
    plugin_id: str,
    plugin_type: str,
    publisher: str = "acme",
    entrypoint: str | None = "driver:TestEffector",
    claimed_source: str = "user",
) -> Path:
    package_dir = root / directory
    package_dir.mkdir(parents=True)
    contribution = {"id": "default", "kind": "effector", "plugin_type": plugin_type,
                    "display_name": "Test Effector"}
    if entrypoint:
        contribution["entrypoint"] = entrypoint
    (package_dir / "manifest.toml").write_text(tomlkit.dumps({
        "manifest_version": 2,
        "package": {"id": plugin_id, "publisher": publisher, "version": "1.0.0",
                    "domain": "actuation", "api_version": "2", "source": claimed_source},
        "contributions": [contribution],
    }), encoding="utf-8")
    return package_dir


def _catalog_for(*roots: PluginRoot) -> PluginCatalog:
    return PluginCatalog({"effector": lambda: list(roots)})


def test_builtin_descriptor_retains_exact_location_and_publisher(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))

    descriptor = PluginCatalog().resolve_selection("effector", PluginSelection('omnibci.logger', "default", '0.1.0'))

    assert descriptor.name == "logger"
    assert descriptor.identifier == "omnibci.logger"
    assert descriptor.publisher == "omnibci"
    assert descriptor.source == "builtin"
    assert descriptor.location.name == "logger"
    assert descriptor.manifest_path.is_file()
    assert str(descriptor.entrypoint) == "effector:LogEffector"


def test_builtin_logger_descriptor_is_directly_loadable(monkeypatch, tmp_path):
    from omnibci.sdk import PluginContext
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve_selection("effector", PluginSelection('omnibci.logger', "default", '0.1.0'))

    instance = PluginLoader.instantiate(descriptor, Effector, parameters={}, context=PluginContext())

    assert type(instance).__name__ == "LogEffector"
    assert instance.plugin_type == "logger"


@pytest.mark.parametrize("directory", ["external", "vendor/series/external"])
def test_discovery_does_not_execute_plugin_and_source_comes_from_root(
    tmp_path, directory,
):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root,
        directory,
        plugin_id="external",
        plugin_type="external",
        claimed_source="builtin",
    )
    marker = tmp_path / "executed"
    (package_dir / "driver.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n",
        encoding="utf-8",
    )

    descriptor = _catalog_for(PluginRoot(root, "user", 0)).scan("effector")[0]

    assert descriptor.source == "user"
    assert descriptor.location == package_dir.resolve()
    assert not marker.exists()


def test_discovery_orders_mixed_depth_packages_and_skips_dunder_groups(tmp_path):
    root = tmp_path / "effectors"
    directories = ["flat", "vendor/reader", "vendor/series/deep"]
    for index, directory in enumerate(reversed(directories)):
        _write_effector_manifest(
            root, directory, plugin_id=str(index), plugin_type=str(index)
        )
    _write_effector_manifest(
        root, "vendor/__cache__/hidden", plugin_id="hidden", plugin_type="hidden"
    )

    descriptors = _catalog_for(PluginRoot(root, "builtin", 100)).scan("effector")

    assert [item.location.relative_to(root).as_posix() for item in descriptors] == (
        directories
    )
    assert all(item.source == "builtin" for item in descriptors)
    assert all(item.precedence == 100 for item in descriptors)


@pytest.mark.parametrize(
    "manifest_kind", ["valid", "malformed", "directory", "symlink", "broken-symlink"]
)
def test_discovery_stops_at_package_boundary(tmp_path, caplog, manifest_kind):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "vendor/package", plugin_id="parent", plugin_type="parent"
    )
    _write_effector_manifest(
        package_dir, "examples/child", plugin_id="child", plugin_type="child"
    )
    manifest = package_dir / "manifest.toml"
    if manifest_kind == "malformed":
        manifest.write_text("[package", encoding="utf-8")
    elif manifest_kind == "directory":
        manifest.unlink()
        manifest.mkdir()
    elif manifest_kind in ("symlink", "broken-symlink"):
        target = tmp_path / "outside.toml"
        if manifest_kind == "symlink":
            target.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
        manifest.unlink()
        try:
            manifest.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

    descriptors = _catalog_for(PluginRoot(root, "user", 0)).scan("effector")

    if manifest_kind == "valid":
        assert [item.plugin_type for item in descriptors] == ["parent"]
    else:
        assert descriptors == ()
        assert "Skipping invalid plugin" in caplog.text
        assert str(manifest) in caplog.text


def test_discovery_does_not_follow_symlinked_groups_or_cycles(tmp_path, caplog):
    root = tmp_path / "effectors"
    _write_effector_manifest(
        root, "working", plugin_id="working", plugin_type="working"
    )
    outside = tmp_path / "outside"
    _write_effector_manifest(
        outside, "driver", plugin_id="outside", plugin_type="outside"
    )
    group = root / "group"
    group.mkdir()
    try:
        (group / "linked-vendor").symlink_to(outside, target_is_directory=True)
        (group / "cycle").symlink_to(root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    descriptors = _catalog_for(PluginRoot(root, "user", 0)).scan("effector")

    assert [item.plugin_type for item in descriptors] == ["working"]
    assert "Skipping symlink" in caplog.text


def test_unreadable_group_does_not_hide_other_plugins(tmp_path, monkeypatch, caplog):
    root = tmp_path / "effectors"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    _write_effector_manifest(
        root, "vendor/working", plugin_id="working", plugin_type="working"
    )
    original_iterdir = Path.iterdir

    def iterdir(path):
        if path == blocked:
            raise PermissionError("Access denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    descriptors = _catalog_for(PluginRoot(root, "user", 0)).scan("effector")

    assert [item.plugin_type for item in descriptors] == ["working"]
    assert str(blocked) in caplog.text
    assert "Access denied" in caplog.text


@pytest.mark.parametrize("invalid", ["controls", "entrypoint"])
def test_malformed_nested_manifest_is_isolated(tmp_path, invalid):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(root, "bad", plugin_id="bad", plugin_type="bad")
    document = tomlkit.loads((package_dir / "manifest.toml").read_text())
    document["contributions"][0][invalid] = {"knob": 1} if invalid == "controls" else "../outside:Class"
    (package_dir / "manifest.toml").write_text(tomlkit.dumps(document))
    _write_effector_manifest(root, "good", plugin_id="good", plugin_type="good")
    catalog = _catalog_for(PluginRoot(root, "user", 0))
    assert [item.plugin_type for item in catalog.scan("effector")] == ["good"]
    with pytest.raises(PluginManifestError, match=invalid):
        catalog.resolve_selection("effector", PluginSelection("acme.bad", "default", "1.0.0"))


@pytest.mark.parametrize("directory", ["relative", "vendor/series/relative"])
def test_selected_entrypoint_supports_relative_imports_without_sys_path_pollution(
    tmp_path, directory,
):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, directory, plugin_id="relative", plugin_type="relative"
    )
    (package_dir.parent / "__init__.py").write_text(
        "raise AssertionError('Grouping directory must not be imported')\n",
        encoding="utf-8",
    )
    (package_dir / "helpers.py").write_text("ORIGIN = 'user-relative'\n", encoding="utf-8")
    package_init_marker = tmp_path / "package-init-executed"
    (package_dir / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(package_init_marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    (package_dir / "driver.py").write_text(
        dedent(
            """
            from omnibci.domain.ports.effector import Effector
            from .helpers import ORIGIN

            class TestEffector(Effector):
                def __init__(self, *, parameters, context): pass
                plugin_type = "relative"

                def __init__(self, *, parameters, context):
                    self.origin = ORIGIN

                def setup(self):
                    pass

                def execute(self, command):
                    pass

                def quiesce(self):
                    pass

                def teardown(self):
                    pass
            """
        ),
        encoding="utf-8",
    )
    bomb = tmp_path / "bomb-executed"
    (package_dir / "bomb.py").write_text(
        f"from pathlib import Path\nPath({str(bomb)!r}).write_text('boom')\n",
        encoding="utf-8",
    )
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve_selection(
        "effector", PluginSelection('acme.relative', "default", '1.0.0')
    )
    path_before = list(sys.path)

    instance = PluginLoader.instantiate(descriptor, Effector, parameters={}, context=PluginContext())

    assert instance.origin == "user-relative"
    assert sys.path == path_before
    assert not bomb.exists()
    assert not package_init_marker.exists()


def test_selected_entrypoint_executes_validated_source_path(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "exact-source", plugin_id="exact-source", plugin_type="exact_source"
    )
    (package_dir / "driver.py").write_text(
        dedent(
            """
            from omnibci.domain.ports.effector import Effector

            class TestEffector(Effector):
                def __init__(self, *, parameters, context): pass
                plugin_type = "exact_source"
                origin = "validated-python-source"
                def setup(self): pass
                def execute(self, command): pass
                def quiesce(self): pass
                def teardown(self): pass
            """
        ),
        encoding="utf-8",
    )
    # The normal FileFinder prefers native extension modules over source.  A
    # bogus same-name extension therefore proves that the loader executes the
    # already-validated driver.py path instead of resolving the name again.
    (package_dir / f"driver{EXTENSION_SUFFIXES[0]}").write_bytes(
        b"not-a-native-extension"
    )
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve_selection(
        "effector", PluginSelection('acme.exact-source', "default", '1.0.0')
    )

    instance = PluginLoader.instantiate(descriptor, Effector, parameters={}, context=PluginContext())

    assert instance.origin == "validated-python-source"


def test_nested_entrypoint_supports_parent_relative_import(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root,
        "nested",
        plugin_id="nested",
        plugin_type="nested",
        entrypoint="drivers.robot:NestedEffector",
    )
    (package_dir / "protocol.py").write_text("ORIGIN = 'parent'\n", encoding="utf-8")
    drivers = package_dir / "drivers"
    drivers.mkdir()
    (drivers / "robot.py").write_text(
        dedent(
            """
            from omnibci.domain.ports.effector import Effector
            from ..protocol import ORIGIN

            class NestedEffector(Effector):
                plugin_type = "nested"
                def __init__(self, *, parameters, context): self.origin = ORIGIN
                def setup(self): pass
                def execute(self, command): pass
                def quiesce(self): pass
                def teardown(self): pass
            """
        ),
        encoding="utf-8",
    )
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve_selection(
        "effector", PluginSelection('acme.nested', "default", '1.0.0')
    )

    instance = PluginLoader.instantiate(descriptor, Effector, parameters={}, context=PluginContext())

    assert instance.origin == "parent"


def test_loader_rejects_ambiguous_module_and_package(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "ambiguous", plugin_id="ambiguous", plugin_type="ambiguous"
    )
    (package_dir / "driver.py").write_text("class TestEffector: pass\n", encoding="utf-8")
    (package_dir / "driver").mkdir()
    (package_dir / "driver" / "__init__.py").write_text("", encoding="utf-8")
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve_selection(
        "effector", PluginSelection('acme.ambiguous', "default", '1.0.0')
    )

    with pytest.raises(PluginLoadError, match="ambiguous"):
        PluginLoader.instantiate(descriptor, Effector, parameters={}, context=PluginContext())


def test_loader_rejects_symlink_anywhere_in_plugin_tree(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "symlinked", plugin_id="symlinked", plugin_type="symlinked"
    )
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    try:
        (package_dir / "helper.py").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    (package_dir / "driver.py").write_text("class TestEffector: pass\n", encoding="utf-8")
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve_selection(
        "effector", PluginSelection('acme.symlinked', "default", '1.0.0')
    )

    with pytest.raises(PluginLoadError, match="contains symlink"):
        PluginLoader.instantiate(descriptor, Effector, parameters={}, context=PluginContext())


def test_removed_registration_api_is_rejected(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root,
        "decorated",
        plugin_id="decorated",
        plugin_type="decorated_override",
    )
    (package_dir / "driver.py").write_text(
        dedent(
            """
            from omnibci.domain.ports.effector import Effector
            from omnibci.infrastructure.plugins.registry import register_plugin

            @register_plugin(Effector)
            class TestEffector(Effector):
                def __init__(self, *, parameters, context): pass
                plugin_type = "decorated_override"
                def setup(self): pass
                def execute(self, command): pass
                def quiesce(self): pass
                def teardown(self): pass
            """
        ),
        encoding="utf-8",
    )
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve_selection(
        "effector", PluginSelection('acme.decorated', "default", '1.0.0')
    )

    with pytest.raises(PluginLoadError, match="omnibci.infrastructure.plugins.registry") as error:
        PluginLoader.instantiate(descriptor, Effector, parameters={}, context=PluginContext())
    assert isinstance(error.value.__cause__, ModuleNotFoundError)


def test_system_exit_cleans_synthetic_modules(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "exit", plugin_id="exit", plugin_type="exit"
    )
    (package_dir / "driver.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve_selection(
        "effector", PluginSelection('acme.exit', "default", '1.0.0')
    )
    prefix = PluginLoader._module_prefix(descriptor)

    with pytest.raises(SystemExit):
        PluginLoader.instantiate(descriptor, Effector, parameters={}, context=PluginContext())

    assert not any(
        name == prefix or name.startswith(f"{prefix}.") for name in sys.modules
    )


def test_constructor_failure_cleans_new_synthetic_modules(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "constructor", plugin_id="constructor", plugin_type="constructor"
    )
    (package_dir / "driver.py").write_text(
        dedent(
            """
            from omnibci.domain.ports.effector import Effector

            class TestEffector(Effector):
                def __init__(self, *, parameters, context): pass
                plugin_type = "constructor"
                def __init__(self, *, parameters, context): raise RuntimeError("constructor failed")
                def setup(self): pass
                def execute(self, command): pass
                def quiesce(self): pass
                def teardown(self): pass
            """
        ),
        encoding="utf-8",
    )
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve_selection(
        "effector", PluginSelection('acme.constructor', "default", '1.0.0')
    )
    prefix = PluginLoader._module_prefix(descriptor)

    with pytest.raises(PluginLoadError, match="constructor failed"):
        PluginLoader.instantiate(descriptor, Effector, parameters={}, context=PluginContext())

    assert not any(
        name == prefix or name.startswith(f"{prefix}.") for name in sys.modules
    )


@pytest.mark.parametrize(
    "entrypoint",
    ["/tmp/driver:Class", "../driver:Class", "driver.py", "driver:not-valid"],
)
def test_entrypoint_rejects_absolute_traversal_and_malformed_values(entrypoint):
    with pytest.raises(ValueError):
        PluginEntrypoint.parse(entrypoint)
