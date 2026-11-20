"""Package reference checks cannot race profile validation and persistence."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from threading import Event, RLock
from unittest.mock import Mock

import pytest

from omnibci.application.plugin_packages import PluginPackageConflictError, package_references_from_settings
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.plugin_package_service import PluginPackageService
from omnibci.application.configuration.service import ConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.package_lifecycle_store import ManagedPluginPackageStore
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def services(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    engine = NeuroEngineService(Mock())
    references = RLock()

    @contextmanager
    def profile_guard():
        with references:
            yield

    store = ManagedPluginPackageStore()
    profiles = ConfigurationService(TomlConfigurationRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(),
                                    change_guard=profile_guard, profile_versions=store)
    packages = PluginPackageService(store, configuration_references=profiles.package_references,
        runtime_references=lambda: tuple(reference for snapshot in engine.runtime_package_snapshots
                                        for reference in package_references_from_settings(snapshot)),
        admission_guard=lambda: references)
    installed = packages.install(package_archive(tmp_path)).package
    packages.set_enabled(installed.package_id, installed.version, True)
    return engine, profiles, packages, installed.contributions[0].selection


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_entire_profile_validation_and_write_uses_matching_guard(services, monkeypatch, operation):
    engine, profiles, _, selection = services
    profile = profiles.create("effector", selection, "Original", {})
    state = {"held": False, "calls": []}
    change_guard = profiles.change_guard
    @contextmanager
    def guard():
        with change_guard():
            state["held"] = True
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
    assert state["calls"] and not state["held"]


def test_in_flight_validation_blocks_disable_until_reference_is_committed(services, monkeypatch):
    _, profiles, packages, selection = services
    entered, release = Event(), Event()
    original = profiles.catalog.get
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Test did not release profile validation"
        return original(*args, **kwargs)
    monkeypatch.setattr(profiles.catalog, "get", delayed)
    action = lambda: profiles.create("effector", selection, "New", {})
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
    copied = profiles.create_version_profile(source.profile_id, "2.0.0", expected_source_revision=1)
    assert copied.profile_id != source.profile_id and copied.selection.version == "2.0.0"


@pytest.mark.parametrize("stage", ["validation", "persistence"])
@pytest.mark.parametrize("operation", ["disable", "remove"])
def test_version_copy_holds_reference_guard_until_persistence(services, tmp_path, monkeypatch, stage, operation):
    _, profiles, packages, selection = services
    source = profiles.create("effector", selection, "Output", {})
    packages.install(package_archive(tmp_path, "2.0.0"))
    packages.set_enabled("acme.device", "2.0.0", True)
    owner, method = ((packages.store, "configuration") if stage == "validation"
                     else (profiles.repository, "save_profile"))
    original = getattr(owner, method)
    entered, release, attempted = Event(), Event(), Event()
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Test did not release version copy"
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, method, delayed)
    def mutate():
        attempted.set()
        if operation == "disable":
            return packages.set_enabled("acme.device", "2.0.0", False)
        return packages.remove("acme.device", "2.0.0")
    with ThreadPoolExecutor(max_workers=2) as executor:
        copying = executor.submit(profiles.create_version_profile, source.profile_id, "2.0.0",
                                  expected_source_revision=source.revision)
        try:
            assert entered.wait(2)
            mutation = executor.submit(mutate)
            assert attempted.wait(2)
            with pytest.raises(TimeoutError):
                mutation.result(timeout=.05)
        finally:
            release.set()
        copied = copying.result(timeout=3)
        with pytest.raises(PluginPackageConflictError, match="referenced"):
            mutation.result(timeout=3)
    assert copied.selection.version == "2.0.0"
    assert profiles.repository.get_profile(copied.profile_id) == copied
    assert packages.store.get_package("acme.device", "2.0.0").enabled
