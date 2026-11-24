"""Package management consumes references; Configuration owns version profiles."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from omnibci.application.configuration.models import ModuleActivation, PluginProfile, ProfileBinding
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.plugin_management.packages import PackageReference, PluginPackageConflictError, PluginPackageError
from omnibci.application.plugin_management.package_service import PluginPackageService
from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def repository(tmp_path):
    return TomlConfigurationRepository(tmp_path / "config")


@pytest.fixture
def profile(repository):
    return repository.save_profile(PluginProfile(
        str(uuid4()), "Output", "effector", PluginSelection("acme.device", "output", "1.0.0"),
        {"gain": 2.0},
    ))


def test_configuration_references_ignore_engine_settings_and_refresh(repository, profile):
    binding = ProfileBinding("actuation.effector", profile.kind, profile.profile_id,
                             profile.selection, profile.parameters, profile.configuration_version,
                             profile.revision)
    repository.save_module(ModuleActivation("actuation", {}, (binding,)))
    settings = repository.root / "settings.toml"
    settings.write_text("invalid = [broken TOML")
    service = ConfigurationService(repository)

    references = service.package_references()
    assert references == (
        PackageReference("profile", profile.profile_id, profile.selection),
        PackageReference("accepted", binding.slot, binding.selection),
    )
    copied = repository.save_profile(PluginProfile(
        str(uuid4()), "Second output", profile.kind, profile.selection, {"gain": 3.0},
    ))
    refreshed = service.package_references()
    assert len(references) == 2 and len(refreshed) == 3
    assert PackageReference("profile", copied.profile_id, copied.selection) in refreshed
    assert settings.read_text() == "invalid = [broken TOML"


@pytest.mark.parametrize("reader", ["list_profiles", "get_module"])
def test_configuration_reference_read_failures_are_not_empty_results(reader):
    repository = SimpleNamespace(list_profiles=Mock(return_value=[]), get_module=Mock(return_value=None))
    getattr(repository, reader).side_effect = OSError("Configuration unreadable")
    with pytest.raises(OSError, match="unreadable"):
        ConfigurationService(repository).package_references()


def test_package_service_uses_only_reference_callbacks_and_refreshes_them():
    selection = PluginSelection("acme.device", "output", "1.0.0")
    references = [PackageReference(scope, scope, selection)
                  for scope in ("profile", "accepted", "running", "pending_apply", "recovery")]
    state = {"configuration": references[:2], "runtime": references[1:]}
    store = SimpleNamespace(remove=Mock(), set_enabled=Mock())
    service = PluginPackageService(
        store, configuration_references=lambda: state["configuration"],
        runtime_references=lambda: state["runtime"], admission_guard=nullcontext,
    )

    assert service.references() == tuple(references)
    with pytest.raises(PluginPackageConflictError, match="referenced"):
        service.remove("acme.device", "1.0.0")
    service.remove("acme.device", "2.0.0")
    state["configuration"] = []
    state["runtime"] = []
    service.set_enabled("acme.device", "1.0.0", False)
    store.remove.assert_called_once_with("acme.device", "2.0.0")
    store.set_enabled.assert_called_once_with("acme.device", "1.0.0", False)
    assert not hasattr(service, "profiles")
    assert not hasattr(service, "propose_profile")
    assert not hasattr(service, "create_version_profile")


@pytest.mark.parametrize("source", ["configuration", "runtime"])
@pytest.mark.parametrize("operation", ["disable", "remove"])
@pytest.mark.parametrize("failure", ["unreadable", "invalid"])
def test_uncertain_references_block_mutation_inside_the_guard(source, operation, failure):
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
    service = PluginPackageService(
        store, configuration_references=readers["configuration"],
        runtime_references=readers["runtime"], admission_guard=guard,
    )
    with pytest.raises(OSError if failure == "unreadable" else PluginPackageError):
        if operation == "disable":
            service.set_enabled("acme.device", "1.0.0", False)
        else:
            service.remove("acme.device", "1.0.0")
    store.remove.assert_not_called()
    store.set_enabled.assert_not_called()
    assert not state["held"]


def test_configuration_versions_need_only_two_read_operations(repository, profile, tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    store = ManagedPluginPackageStore()
    store.install_archive(package_archive(tmp_path, "2.0.0"))
    metadata = SimpleNamespace(get_package=store.get_package, configuration=store.configuration)
    service = ConfigurationService(repository, CatalogConfigurationAdapter(), profile_versions=metadata)

    proposal = service.propose_profile(profile.profile_id, "2.0.0")
    assert not proposal.target_enabled and proposal.parameters == profile.parameters
    assert repository.list_profiles() == [profile]
    with pytest.raises(PluginPackageConflictError, match="Enable"):
        service.create_version_profile(profile.profile_id, "2.0.0", expected_source_revision=profile.revision)
    store.set_enabled("acme.device", "2.0.0", True)
    copied = service.create_version_profile(profile.profile_id, "2.0.0", expected_source_revision=profile.revision)
    assert copied.profile_id != profile.profile_id and copied.selection.version == "2.0.0"
    assert copied.parameters == profile.parameters
    assert repository.get_profile(profile.profile_id) == profile
    assert all(value is None for value in service.selected_configuration().modules.values())
