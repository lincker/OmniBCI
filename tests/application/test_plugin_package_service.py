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
