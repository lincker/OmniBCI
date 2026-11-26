"""Plugin store lifecycle: zero persistent runtime state files and in-process serialization."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from omnibci.application.configuration.errors import PluginProfileValidationError
from omnibci.application.plugin_management.packages import PluginPackageConflictError
from omnibci.application.plugin_management.schema import PluginSchemaError
from omnibci.bootstrap import build_container
from tests.infrastructure.test_package_store import package_archive


_FORBIDDEN_RUNTIME_FILES = (
    ".controller.active",
    ".stimulation.active",
    ".actuation.active",
    ".controller.lock",
    ".stimulation.lock",
    ".actuation.lock",
    "controller.json",
)


@pytest.fixture
def isolated_plugin_store(tmp_path, monkeypatch):
    config_root = tmp_path / "configuration"
    plugin_root = tmp_path / "plugins"
    config_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(config_root))
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(plugin_root))
    return config_root, plugin_root


def _assert_no_runtime_state_files(plugin_root) -> None:
    state_dir = plugin_root / "state"
    for name in _FORBIDDEN_RUNTIME_FILES:
        assert not (state_dir / name).exists(), f"Unexpected runtime state file: {name}"


@pytest.mark.asyncio
async def test_container_package_lifecycle_creates_no_runtime_state_files(
    tmp_path, isolated_plugin_store
):
    _, plugin_root = isolated_plugin_store
    archive = package_archive(tmp_path)

    container = await build_container()
    try:
        packages = container.plugin_package_service
        _assert_no_runtime_state_files(plugin_root)
        installed = packages.install(archive).package
        _assert_no_runtime_state_files(plugin_root)
        packages.set_enabled(installed.package_id, installed.version, True)
        _assert_no_runtime_state_files(plugin_root)
        packages.set_enabled(installed.package_id, installed.version, False)
        _assert_no_runtime_state_files(plugin_root)
        packages.remove(installed.package_id, installed.version)
        _assert_no_runtime_state_files(plugin_root)
    finally:
        await container.engine_service.shutdown()
    _assert_no_runtime_state_files(plugin_root)


@pytest.mark.asyncio
async def test_legacy_runtime_state_markers_do_not_block_startup_or_mutations(
    tmp_path, isolated_plugin_store
):
    _, plugin_root = isolated_plugin_store
    state_dir = plugin_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    for name in (".controller.active", ".stimulation.active", ".actuation.active"):
        (state_dir / name).write_text("stale legacy marker\n", encoding="utf-8")
    (state_dir / "controller.json").write_text(
        json.dumps({"configuration_root": str(tmp_path / "other-config")}),
        encoding="utf-8",
    )

    archive = package_archive(tmp_path)
    container = await build_container()
    try:
        packages = container.plugin_package_service
        installed = packages.install(archive).package
        packages.set_enabled(installed.package_id, installed.version, True)
        packages.set_enabled(installed.package_id, installed.version, False)
        packages.remove(installed.package_id, installed.version)
    finally:
        await container.engine_service.shutdown()


@pytest.mark.asyncio
async def test_shared_in_process_lock_serializes_profile_creation_and_package_disable(
    tmp_path, isolated_plugin_store
):
    _, plugin_root = isolated_plugin_store
    archive = package_archive(tmp_path)
    container = await build_container()
    try:
        packages = container.plugin_package_service
        profiles = container.configuration_service
        guard = profiles.change_guard()
        assert isinstance(guard, type(threading.RLock()))
        assert container.runtime_configuration.reference_guard() is guard
        assert packages.admission_guard() is guard

        installed = packages.install(archive).package
        packages.set_enabled(installed.package_id, installed.version, True)
        selection = installed.contributions[0].selection

        # Race profile creation against package disable: the shared RLock must
        # guarantee that no saved profile ever points at a disabled package.
        barrier = threading.Barrier(2)

        def create_profile():
            barrier.wait(timeout=5)
            try:
                return ("created", profiles.create("effector", selection, "Raced profile", {}))
            except (PluginProfileValidationError, PluginSchemaError) as exc:
                return ("rejected", exc)

        def disable_package():
            barrier.wait(timeout=5)
            try:
                packages.set_enabled(installed.package_id, installed.version, False)
                return ("disabled", None)
            except PluginPackageConflictError as exc:
                return ("conflict", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            create_future = pool.submit(create_profile)
            disable_future = pool.submit(disable_package)
            create_outcome, profile_or_err = create_future.result(timeout=5)
            disable_outcome, _ = disable_future.result(timeout=5)

        if create_outcome == "created":
            assert disable_outcome == "conflict"
            assert any(
                item.package_id == installed.package_id and item.enabled
                for item in packages.list()
            )
            profiles.delete(profile_or_err.profile_id, expected_revision=profile_or_err.revision)
            packages.set_enabled(installed.package_id, installed.version, False)
        else:
            assert create_outcome == "rejected"
            assert disable_outcome == "disabled"
            assert profiles.repository.list_profiles() == []

        # Additive install/enable remain unblocked while RuntimeAdmission holds a transition.
        entered, release = threading.Event(), threading.Event()

        def hold_transition():
            with container.engine_service.admission.transition("processing"):
                entered.set()
                assert release.wait(timeout=5)

        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold_transition)
            try:
                assert entered.wait(timeout=2)
                packages.install(archive)
                packages.set_enabled(installed.package_id, installed.version, True)
            finally:
                release.set()
            holder.result(timeout=2)

        _assert_no_runtime_state_files(plugin_root)
    finally:
        await container.engine_service.shutdown()
