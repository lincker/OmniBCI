"""References block destructive package lifecycle changes; version copies are explicit."""
from contextlib import contextmanager

import pytest

from omnibci.application.plugin_management.packages import PackageReference, PluginPackageConflictError
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.plugin_management.package_service import PluginPackageService
from omnibci.application.configuration.service import ConfigurationService
from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    state = {"held": False, "busy": False, "references": ()}
    @contextmanager
    def guard():
        if state["busy"]:
            raise ProfileRevisionConflict("Apply in progress")
        assert not state["held"]
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False
    store = ManagedPluginPackageStore()
    profiles = ConfigurationService(TomlConfigurationRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(), change_guard=guard)
    svc = PluginPackageService(store, configuration_references=profiles.package_references, runtime_references=lambda: state["references"], admission_guard=guard)
    for version in ("1.0.0", "2.0.0"):
        svc.install(package_archive(tmp_path, version))
    svc.set_enabled("acme.device", "1.0.0", True)
    return svc, profiles, state


def source_profile(service, profiles):
    selection = service.store.get_package("acme.device", "1.0.0").contributions[0].selection
    return profiles.create("effector", selection, "Output", {"gain": 2.0})


def test_saved_profile_and_accepted_binding_references_are_listed_and_block_disable(service):
    svc, profiles, _ = service
    profile = source_profile(svc, profiles)
    active = profiles.propose("actuation", [
        {"slot": "actuation.effector", "profile_id": profile.profile_id},
    ], settings=profiles.default_module_settings("actuation"))
    profiles.repository.save_module(active, expected_revision=None)
    references = svc.list()[0].references
    assert {item.scope for item in references} == {"profile", "accepted"}
    with pytest.raises(PluginPackageConflictError, match="profile:.*accepted:"):
        svc.set_enabled("acme.device", "1.0.0", False)
    with pytest.raises(PluginPackageConflictError):
        svc.remove("acme.device", "1.0.0")
    assert profiles.repository.get_module("actuation") == active
    assert svc.store.get_package("acme.device", "1.0.0").enabled


@pytest.mark.parametrize("scope", ["running", "pending_apply", "recovery"])
def test_runtime_and_pending_references_checked_inside_admission(service, scope):
    svc, _, state = service
    selection = svc.store.get_package("acme.device", "1.0.0").contributions[0].selection
    def references():
        assert state["held"]
        return [PackageReference(scope, "actuation.effector", selection)]
    svc.runtime_references = references
    with pytest.raises(PluginPackageConflictError, match=scope):
        svc.set_enabled("acme.device", "1.0.0", False)
    with pytest.raises(PluginPackageConflictError, match=scope):
        svc.remove("acme.device", "1.0.0")


def test_other_version_references_do_not_prevent_unused_version_removal(service):
    svc, profiles, _ = service
    source_profile(svc, profiles)
    svc.remove("acme.device", "2.0.0")
    assert len(svc.list()) == 1


def test_busy_apply_rejects_package_mutations(service):
    svc, _, state = service
    state["busy"] = True
    with pytest.raises(ProfileRevisionConflict, match="Apply"):
        svc.set_enabled("acme.device", "2.0.0", True)
    with pytest.raises(ProfileRevisionConflict):
        svc.remove("acme.device", "2.0.0")


def test_after_all_references_are_released_disable_and_remove_work(service):
    svc, profiles, _ = service
    original = source_profile(svc, profiles)
    profiles.delete(original.profile_id, expected_revision=1)
    svc.set_enabled("acme.device", "1.0.0", False)
    svc.remove("acme.device", "1.0.0")
    assert [item.version for item in svc.list()] == ["2.0.0"]


@pytest.fixture
def boundary_repository(tmp_path):
    return TomlConfigurationRepository(tmp_path / "config")


@pytest.fixture
def boundary_profile(boundary_repository):
    from uuid import uuid4
    from omnibci.application.configuration.models import PluginProfile
    from omnibci.application.plugin_management.identity import PluginSelection

    return boundary_repository.save_profile(PluginProfile(
        str(uuid4()), "Output", "effector", PluginSelection("acme.device", "output", "1.0.0"),
        {"gain": 2.0},
    ))


def test_configuration_references_ignore_engine_settings_and_refresh(boundary_repository, boundary_profile):
    from uuid import uuid4
    from omnibci.application.configuration.models import ModuleActivation, PluginProfile, ProfileBinding

    binding = ProfileBinding("actuation.effector", boundary_profile.kind, boundary_profile.profile_id,
                             boundary_profile.selection, boundary_profile.parameters, boundary_profile.configuration_version,
                             boundary_profile.revision)
    boundary_repository.save_module(ModuleActivation("actuation", {}, (binding,)))
    settings = boundary_repository.root / "settings.toml"
    settings.write_text("invalid = [broken TOML")
    svc = ConfigurationService(boundary_repository)

    references = svc.package_references()
    assert references == (
        PackageReference("profile", boundary_profile.profile_id, boundary_profile.selection),
        PackageReference("accepted", binding.slot, binding.selection),
    )
    copied = boundary_repository.save_profile(PluginProfile(
        str(uuid4()), "Second output", boundary_profile.kind, boundary_profile.selection, {"gain": 3.0},
    ))
    refreshed = svc.package_references()
    assert len(references) == 2 and len(refreshed) == 3
    assert PackageReference("profile", copied.profile_id, copied.selection) in refreshed
    assert settings.read_text() == "invalid = [broken TOML"


@pytest.mark.parametrize("reader", ["list_profiles", "get_module"])
def test_configuration_reference_read_failures_are_not_empty_results(reader):
    from types import SimpleNamespace
    from unittest.mock import Mock

    repository = SimpleNamespace(list_profiles=Mock(return_value=[]), get_module=Mock(return_value=None))
    getattr(repository, reader).side_effect = OSError("Configuration unreadable")
    with pytest.raises(OSError, match="unreadable"):
        ConfigurationService(repository).package_references()


def test_package_service_uses_only_reference_callbacks_and_refreshes_them():
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import Mock
    from omnibci.application.plugin_management.identity import PluginSelection

    selection = PluginSelection("acme.device", "output", "1.0.0")
    references = [PackageReference(scope, scope, selection)
                  for scope in ("profile", "accepted", "running", "pending_apply", "recovery")]
    state = {"configuration": references[:2], "runtime": references[1:]}
    store = SimpleNamespace(remove=Mock(), set_enabled=Mock())
    svc = PluginPackageService(
        store, configuration_references=lambda: state["configuration"],
        runtime_references=lambda: state["runtime"], admission_guard=nullcontext,
    )

    assert svc.references() == tuple(references)
    with pytest.raises(PluginPackageConflictError, match="referenced"):
        svc.remove("acme.device", "1.0.0")
    svc.remove("acme.device", "2.0.0")
    state["configuration"] = []
    state["runtime"] = []
    svc.set_enabled("acme.device", "1.0.0", False)
    store.remove.assert_called_once_with("acme.device", "2.0.0")
    store.set_enabled.assert_called_once_with("acme.device", "1.0.0", False)
    assert not hasattr(svc, "profiles")
    assert not hasattr(svc, "propose_profile")
    assert not hasattr(svc, "create_version_profile")


@pytest.mark.parametrize("source", ["configuration", "runtime"])
@pytest.mark.parametrize("operation", ["disable", "remove"])
@pytest.mark.parametrize("failure", ["unreadable", "invalid"])
def test_uncertain_references_block_mutation_inside_the_guard(source, operation, failure):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from omnibci.application.plugin_management.packages import PluginPackageError

    state = {"held": False}

    @contextmanager
    def guard():
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False

    def references():
        assert state["held"]
        if failure == "unreadable":
            raise OSError("Reference source unavailable")
        return ["not a package reference"]

    readers = {"configuration": lambda: (), "runtime": lambda: ()}
    readers[source] = references
    store = SimpleNamespace(remove=Mock(), set_enabled=Mock())
    svc = PluginPackageService(
        store, configuration_references=readers["configuration"],
        runtime_references=readers["runtime"], admission_guard=guard,
    )
    with pytest.raises(OSError if failure == "unreadable" else PluginPackageError):
        if operation == "disable":
            svc.set_enabled("acme.device", "1.0.0", False)
        else:
            svc.remove("acme.device", "1.0.0")
    store.remove.assert_not_called()
    store.set_enabled.assert_not_called()
    assert not state["held"]


def test_configuration_versions_need_only_two_read_operations(boundary_repository, boundary_profile, tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    store = ManagedPluginPackageStore()
    store.install_archive(package_archive(tmp_path, "2.0.0"))
    metadata = SimpleNamespace(get_package=store.get_package, configuration=store.configuration)
    svc = ConfigurationService(boundary_repository, CatalogConfigurationAdapter(), profile_versions=metadata)

    proposal = svc.propose_profile(boundary_profile.profile_id, "2.0.0")
    assert not proposal.target_enabled and proposal.parameters == boundary_profile.parameters
    assert boundary_repository.list_profiles() == [boundary_profile]
    with pytest.raises(PluginPackageConflictError, match="Enable"):
        svc.create_version_profile(boundary_profile.profile_id, "2.0.0", expected_source_revision=boundary_profile.revision)
    store.set_enabled("acme.device", "2.0.0", True)
    copied = svc.create_version_profile(boundary_profile.profile_id, "2.0.0", expected_source_revision=boundary_profile.revision)
    assert copied.profile_id != boundary_profile.profile_id and copied.selection.version == "2.0.0"
    assert copied.parameters == boundary_profile.parameters
    assert boundary_repository.get_profile(boundary_profile.profile_id) == boundary_profile
    assert all(value is None for value in svc.selected_configuration().modules.values())


@pytest.fixture
def admission_services(tmp_path, monkeypatch):
    from threading import RLock
    from unittest.mock import Mock
    from omnibci.application.plugin_management.packages import package_references_from_settings
    from omnibci.application.services.engine_service import NeuroEngineService

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
    packages = PluginPackageService(
        store,
        configuration_references=profiles.package_references,
        runtime_references=lambda: tuple(reference for snapshot in engine.runtime_package_snapshots
                                         for reference in package_references_from_settings(snapshot)),
        admission_guard=lambda: references,
    )
    installed = packages.install(package_archive(tmp_path)).package
    packages.set_enabled(installed.package_id, installed.version, True)
    return engine, profiles, packages, installed.contributions[0].selection


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_entire_profile_validation_and_write_uses_matching_guard(admission_services, monkeypatch, operation):
    _, profiles, _, selection = admission_services
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


def test_in_flight_validation_blocks_disable_until_reference_is_committed(admission_services, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    _, profiles, packages, selection = admission_services
    entered, release = Event(), Event()
    original = profiles.catalog.get

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Test did not release profile validation"
        return original(*args, **kwargs)

    monkeypatch.setattr(profiles.catalog, "get", delayed)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(profiles.create, "effector", selection, "New", {})
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


def test_package_reference_write_delays_profile_validation_until_it_finishes(admission_services, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from unittest.mock import Mock

    _, profiles, packages, selection = admission_services
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


def test_new_version_profile_can_nest_profile_guard_inside_package_transition(admission_services, tmp_path):
    _, profiles, packages, selection = admission_services
    source = profiles.create("effector", selection, "Output", {})
    packages.install(package_archive(tmp_path, "2.0.0"))
    packages.set_enabled("acme.device", "2.0.0", True)
    copied = profiles.create_version_profile(source.profile_id, "2.0.0", expected_source_revision=1)
    assert copied.profile_id != source.profile_id and copied.selection.version == "2.0.0"


@pytest.mark.parametrize("stage", ["validation", "persistence"])
@pytest.mark.parametrize("operation", ["disable", "remove"])
def test_version_copy_holds_reference_guard_until_persistence(admission_services, tmp_path, monkeypatch, stage, operation):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    _, profiles, packages, selection = admission_services
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
