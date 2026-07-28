"""Runtime descriptor, precedence and selected-entrypoint loading tests."""

from __future__ import annotations

import sys
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
from textwrap import dedent

import pytest

from omnibci.domain.ports.effector import Effector
from omnibci.infrastructure.plugins.descriptors import PluginEntrypoint
from omnibci.infrastructure.plugins.loader import PluginLoadError, PluginLoader
from omnibci.infrastructure.plugins.registry import register_plugin, registry_for
from omnibci.infrastructure.plugins.plugin_catalog import (
    AmbiguousPluginError,
    MissingEntrypointError,
    PluginRoot,
    PluginIdentityError,
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
    entrypoint_line = f'entrypoint = "{entrypoint}"\n' if entrypoint else ""
    (package_dir / "manifest.toml").write_text(
        dedent(
            f"""
            [package]
            id = "{plugin_id}"
            publisher = "{publisher}"
            version = "1.0.0"
            kind = "effector"
            api_version = "1"
            plugin_type = "{plugin_type}"
            {entrypoint_line}display_name = "Test Effector"
            source = "{claimed_source}"
            """
        ),
        encoding="utf-8",
    )
    return package_dir


def _catalog_for(*roots: PluginRoot) -> PluginCatalog:
    return PluginCatalog({"effector": lambda: list(roots)})


def test_builtin_descriptor_retains_exact_location_and_publisher(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))

    descriptor = PluginCatalog().resolve("effector", "logger")

    assert descriptor.name == "logger"
    assert descriptor.identifier == "omnibci.logger"
    assert descriptor.publisher == "omnibci"
    assert descriptor.source == "builtin"
    assert descriptor.location.name == "logger"
    assert descriptor.manifest_path.is_file()
    assert str(descriptor.entrypoint) == "effector:LogEffector"


def test_builtin_logger_descriptor_is_directly_loadable(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve("effector", "logger")

    instance = PluginLoader.instantiate(descriptor, Effector)

    assert type(instance).__name__ == "LogEffector"
    assert instance.plugin_type == "logger"


def test_discovery_does_not_execute_plugin_and_source_comes_from_root(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root,
        "external",
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


def test_user_plugin_shadows_builtin_by_plugin_type(monkeypatch, tmp_path):
    user_plugins = tmp_path / "plugins"
    package_dir = _write_effector_manifest(
        user_plugins / "effectors",
        "custom-logger",
        plugin_id="custom-logger",
        plugin_type="logger",
        publisher="alice",
    )
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(user_plugins))

    descriptor = PluginCatalog().resolve("effector", "logger")

    assert descriptor.location == package_dir.resolve()
    assert descriptor.identifier == "alice.custom-logger"
    assert descriptor.source == "user"


def test_missing_user_entrypoint_does_not_fall_back_to_builtin(monkeypatch, tmp_path):
    user_plugins = tmp_path / "plugins"
    _write_effector_manifest(
        user_plugins / "effectors",
        "broken-override",
        plugin_id="broken-override",
        plugin_type="logger",
        entrypoint=None,
    )
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(user_plugins))

    with pytest.raises(MissingEntrypointError, match="has no entrypoint"):
        PluginCatalog().resolve("effector", "logger")


def test_invalid_user_override_does_not_fall_back_to_builtin(monkeypatch, tmp_path):
    user_plugins = tmp_path / "plugins"
    _write_effector_manifest(
        user_plugins / "effectors",
        "invalid-override",
        plugin_id="invalid-override",
        plugin_type="logger",
        entrypoint="../driver:TestEffector",
    )
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(user_plugins))

    with pytest.raises(PluginManifestError, match="refusing fallback"):
        PluginCatalog().resolve("effector", "logger")


def test_same_precedence_duplicate_plugin_type_is_ambiguous(tmp_path):
    root = tmp_path / "effectors"
    _write_effector_manifest(
        root, "one", plugin_id="one", plugin_type="duplicate"
    )
    _write_effector_manifest(
        root, "two", plugin_id="two", plugin_type="duplicate"
    )

    with pytest.raises(AmbiguousPluginError, match="Ambiguous effector"):
        _catalog_for(PluginRoot(root, "user", 0)).resolve(
            "effector", "duplicate"
        )


def test_same_identifier_cannot_declare_different_plugin_types(tmp_path):
    root = tmp_path / "effectors"
    _write_effector_manifest(
        root,
        "one",
        plugin_id="shared-id",
        plugin_type="first",
        publisher="alice",
    )
    _write_effector_manifest(
        root,
        "two",
        plugin_id="shared-id",
        plugin_type="second",
        publisher="alice",
    )
    catalog = _catalog_for(PluginRoot(root, "user", 0))

    with pytest.raises(PluginIdentityError, match="declares conflicting"):
        catalog.resolve("effector", "first")
    assert catalog.list("effector") == ()


def test_conflicting_identity_does_not_block_unrelated_runtime_candidate(tmp_path):
    root = tmp_path / "effectors"
    _write_effector_manifest(
        root,
        "shared-first",
        plugin_id="shared",
        plugin_type="first",
        publisher="alice",
    )
    _write_effector_manifest(
        root,
        "shared-second",
        plugin_id="shared",
        plugin_type="second",
        publisher="alice",
    )
    candidate = _write_effector_manifest(
        root,
        "unrelated-first",
        plugin_id="unrelated",
        plugin_type="first",
        publisher="bob",
    )
    catalog = _catalog_for(PluginRoot(root, "user", 0))

    listed = catalog.list("effector")
    resolved = catalog.resolve("effector", "first")

    assert [descriptor.location for descriptor in listed] == [candidate.resolve()]
    assert resolved.location == candidate.resolve()
    with pytest.raises(PluginIdentityError, match="alice.shared"):
        catalog.resolve("effector", "second")


@pytest.mark.parametrize(
    ("bad_manifest", "message"),
    [
        (
            'runtime = "not-a-table"\n'
            '[package]\n'
            'id = "bad"\nkind = "effector"\nplugin_type = "bad"\n',
            "runtime.*TOML table",
        ),
        (
            '[package]\n'
            'id = "bad"\nkind = "effector"\nplugin_type = "bad"\n'
            '[controls]\nknob = 1\n',
            "controls.knob.*TOML table",
        ),
    ],
)
def test_malformed_nested_manifest_is_isolated(tmp_path, bad_manifest, message):
    root = tmp_path / "effectors"
    package_dir = root / "bad"
    package_dir.mkdir(parents=True)
    (package_dir / "manifest.toml").write_text(bad_manifest, encoding="utf-8")
    catalog = _catalog_for(PluginRoot(root, "user", 0))

    assert catalog.scan("effector") == ()
    with pytest.raises(PluginManifestError, match=message):
        catalog.resolve("effector", "bad")


def test_non_actuation_v1_manifest_remains_visible_without_entrypoint(tmp_path):
    root = tmp_path / "paradigms"
    package_dir = root / "legacy"
    package_dir.mkdir(parents=True)
    (package_dir / "manifest.toml").write_text(
        dedent(
            """
            [package]
            id = "legacy"
            publisher = "alice"
            kind = "paradigm"
            api_version = "1"
            plugin_type = "legacy_paradigm"
            """
        ),
        encoding="utf-8",
    )
    catalog = PluginCatalog(
        {"paradigm": lambda: [PluginRoot(root, "user", 0)]}
    )

    descriptors = catalog.list("paradigm")

    assert len(descriptors) == 1
    assert descriptors[0].entrypoint is None


def test_selected_entrypoint_supports_relative_imports_without_sys_path_pollution(
    tmp_path,
):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "relative", plugin_id="relative", plugin_type="relative"
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
                plugin_type = "relative"

                def __init__(self):
                    self.origin = ORIGIN

                def setup(self):
                    pass

                def play(self, command):
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
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve(
        "effector", "relative"
    )
    path_before = list(sys.path)

    instance = PluginLoader.instantiate(descriptor, Effector)

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
                plugin_type = "exact_source"
                origin = "validated-python-source"
                def setup(self): pass
                def play(self, command): pass
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
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve(
        "effector", "exact_source"
    )

    instance = PluginLoader.instantiate(descriptor, Effector)

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
                def __init__(self): self.origin = ORIGIN
                def setup(self): pass
                def play(self, command): pass
                def teardown(self): pass
            """
        ),
        encoding="utf-8",
    )
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve(
        "effector", "nested"
    )

    instance = PluginLoader.instantiate(descriptor, Effector)

    assert instance.origin == "parent"


def test_loader_rejects_manifest_and_class_plugin_type_mismatch(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "mismatch", plugin_id="mismatch", plugin_type="manifest-value"
    )
    (package_dir / "driver.py").write_text(
        dedent(
            """
            from omnibci.domain.ports.effector import Effector

            class TestEffector(Effector):
                plugin_type = "class-value"
                def setup(self): pass
                def play(self, command): pass
                def teardown(self): pass
            """
        ),
        encoding="utf-8",
    )
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve(
        "effector", "manifest-value"
    )

    with pytest.raises(PluginLoadError, match="does not match manifest"):
        PluginLoader.instantiate(descriptor, Effector)


def test_loader_rejects_ambiguous_module_and_package(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "ambiguous", plugin_id="ambiguous", plugin_type="ambiguous"
    )
    (package_dir / "driver.py").write_text("class TestEffector: pass\n", encoding="utf-8")
    (package_dir / "driver").mkdir()
    (package_dir / "driver" / "__init__.py").write_text("", encoding="utf-8")
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve(
        "effector", "ambiguous"
    )

    with pytest.raises(PluginLoadError, match="ambiguous"):
        PluginLoader.instantiate(descriptor, Effector)


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
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve(
        "effector", "symlinked"
    )

    with pytest.raises(PluginLoadError, match="contains symlink"):
        PluginLoader.instantiate(descriptor, Effector)


def test_decorated_entrypoint_does_not_touch_global_registry(tmp_path):
    class Existing(Effector):
        plugin_type = "decorated_override"
        def setup(self): pass
        def play(self, command): pass
        def teardown(self): pass

    register_plugin(Effector)(Existing)
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
                plugin_type = "decorated_override"
                def setup(self): pass
                def play(self, command): pass
                def teardown(self): pass
            """
        ),
        encoding="utf-8",
    )
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve(
        "effector", "decorated_override"
    )

    instance = PluginLoader.instantiate(descriptor, Effector)

    assert type(instance).__name__ == "TestEffector"
    assert registry_for(Effector)["decorated_override"] is Existing


def test_system_exit_cleans_synthetic_modules(tmp_path):
    root = tmp_path / "effectors"
    package_dir = _write_effector_manifest(
        root, "exit", plugin_id="exit", plugin_type="exit"
    )
    (package_dir / "driver.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve(
        "effector", "exit"
    )
    prefix = PluginLoader._module_prefix(descriptor)

    with pytest.raises(SystemExit):
        PluginLoader.instantiate(descriptor, Effector)

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
                plugin_type = "constructor"
                def __init__(self): raise RuntimeError("constructor failed")
                def setup(self): pass
                def play(self, command): pass
                def teardown(self): pass
            """
        ),
        encoding="utf-8",
    )
    descriptor = _catalog_for(PluginRoot(root, "user", 0)).resolve(
        "effector", "constructor"
    )
    prefix = PluginLoader._module_prefix(descriptor)

    with pytest.raises(PluginLoadError, match="constructor failed"):
        PluginLoader.instantiate(descriptor, Effector)

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
