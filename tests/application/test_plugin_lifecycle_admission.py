"""Package reference checks cannot race profile validation and persistence."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from threading import Event, RLock
from unittest.mock import Mock

import pytest

from omnibci.application.plugin_packages import PluginPackageConflictError, package_references_from_settings
from omnibci.application.plugin_profiles import ModuleActivation, ProfileBinding
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.plugin_package_service import PluginPackageService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.package_lifecycle_store import ManagedPluginPackageStore
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def services(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    engine = NeuroEngineService(Mock())
    references = RLock()

    @contextmanager
    def profile_guard(requires_idle):
        with references:
            if requires_idle:
                with engine.configuration_change(True):
                    yield
            else:
                yield

    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(), settings_source=lambda: {},
                                    change_guard=profile_guard)
    packages = PluginPackageService(ManagedPluginPackageStore(), profiles,
        runtime_references=lambda: tuple(reference for snapshot in engine.runtime_package_snapshots
                                        for reference in package_references_from_settings(snapshot)),
        admission_guard=lambda: references)
    installed = packages.install(package_archive(tmp_path)).package
    packages.set_enabled(installed.package_id, installed.version, True)
    return engine, profiles, packages, installed.contributions[0].selection


@pytest.mark.parametrize("operation", ["create", "update", "delete", "accept"])
def test_entire_profile_validation_and_write_uses_matching_guard(services, monkeypatch, operation):
    engine, profiles, _, selection = services
    profile = profiles.create("effector", selection, "Original", {})
    snapshot = profiles.propose("actuation", [
        {"slot": "actuation.effector", "profile_id": profile.profile_id},
    ])
    state = {"held": False, "idle": None, "calls": []}
    change_guard = profiles.change_guard
    @contextmanager
    def guard(requires_idle):
        with change_guard(requires_idle):
            state["held"] = True
            state["idle"] = requires_idle
            try:
                yield
            finally:
                state["held"] = False
    profiles.change_guard = guard
    def observe(owner, method):
        original = getattr(owner, method)
        def checked(*args, **kwargs):
            assert state["held"], f"{method} escaped mutation admission"
            state["calls"].append(method)
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, method, checked)
    observe(profiles.catalog, "get")
    for method in ("get_profile", "get_module", "save_profile", "delete_profile", "save_module"):
        observe(profiles.repository, method)
    if operation == "create":
        profiles.create("effector", selection, "New", {})
    elif operation == "update":
        profiles.update(profile.profile_id, name="Changed", parameters={"gain": 2.0}, expected_revision=1)
    elif operation == "delete":
        profiles.delete(profile.profile_id, expected_revision=1)
    else:
        profiles.accept_module(snapshot, expected_revision=None)
    assert state["idle"] is (operation == "accept")
    assert state["calls"] and not state["held"]


@pytest.mark.parametrize("operation", ["create", "accept"])
def test_in_flight_validation_blocks_disable_until_reference_is_committed(services, monkeypatch, operation):
    _, profiles, packages, selection = services
    snapshot = ModuleActivation("actuation", {}, (ProfileBinding(
        "actuation.effector", "effector", None, selection, {"gain": 1.0}),))
    entered, release = Event(), Event()
    original = profiles.catalog.get
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Test did not release profile validation"
        return original(*args, **kwargs)
    monkeypatch.setattr(profiles.catalog, "get", delayed)
    action = (lambda: profiles.create("effector", selection, "New", {})) if operation == "create" else (
        lambda: profiles.accept_module(snapshot, expected_revision=None))
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(action)
        try:
            assert entered.wait(2)
            disabling = executor.submit(packages.set_enabled, "acme.device", "1.0.0", False)
            with pytest.raises(TimeoutError):
                disabling.result(timeout=.05)
        finally:
            release.set()
        pending.result(timeout=3)
        with pytest.raises(PluginPackageConflictError, match="referenced"):
            disabling.result(timeout=3)
    with pytest.raises(PluginPackageConflictError, match="referenced"):
        packages.set_enabled("acme.device", "1.0.0", False)
    assert packages.store.get_package("acme.device", "1.0.0").enabled


def test_package_reference_write_delays_profile_validation_until_it_finishes(services, monkeypatch):
    _, profiles, packages, selection = services
    entered, release = Event(), Event()
    get = Mock(wraps=profiles.catalog.get)
    monkeypatch.setattr(profiles.catalog, "get", get)
    def transition():
        with packages.admission_guard():
            entered.set()
            assert release.wait(3)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(transition)
        try:
            assert entered.wait(2)
            creating = executor.submit(profiles.create, "effector", selection, "Pending", {})
            with pytest.raises(TimeoutError):
                creating.result(timeout=.05)
            get.assert_not_called()
        finally:
            release.set()
        pending.result(timeout=3)
        created = creating.result(timeout=3)
    get.assert_called()
    assert profiles.repository.list_profiles() == [created]


def test_new_version_profile_can_nest_profile_guard_inside_package_transition(services, tmp_path):
    _, profiles, packages, selection = services
    source = profiles.create("effector", selection, "Output", {})
    packages.install(package_archive(tmp_path, "2.0.0"))
    packages.set_enabled("acme.device", "2.0.0", True)
    copied = packages.create_version_profile(source.profile_id, "2.0.0", expected_source_revision=1)
    assert copied.profile_id != source.profile_id and copied.selection.version == "2.0.0"
