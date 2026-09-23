"""Saving, selecting, and resolving configuration have distinct effects."""
from contextlib import contextmanager
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return PluginProfileService(
        TomlPluginProfileRepository(tmp_path / "config"), CatalogConfigurationAdapter(),
        settings_source=lambda: {"ACQUISITION.sample_rate": 1000, "host": "original"},
    )


def create_source(service, name="Replay", **parameters):
    plugin = service.catalog.legacy("acquisition", "file_reader")
    return service.create("acquisition", plugin.selection, name, parameters)


def test_save_never_changes_accepted_values_even_for_active_profile(service):
    profile = create_source(service)
    candidate = service.propose([{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    service.accept(candidate, expected_revision=None)
    before = service.resolve_settings()
    changed = service.update(profile.profile_id, name="Changed", parameters={"sample_rate": 250},
                             expected_revision=profile.revision)
    assert changed.revision == profile.revision + 1
    assert service.resolve_settings() == before
    assert before["ACQUISITION.sample_rate"] == 1000
    assert before["PLUGINS.bindings"][0]["parameters"]["sample_rate"] == 1000

    next_snapshot = service.propose([{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    service.accept(next_snapshot, expected_revision=candidate.revision)
    assert service.resolve_settings()["ACQUISITION.sample_rate"] == 250
    assert service.resolve_settings()["CONFIG.revision"] != before["CONFIG.revision"]


def test_profile_and_activation_conflicts_do_not_overwrite(service):
    profile = create_source(service)
    service.update(profile.profile_id, name="First", parameters={}, expected_revision=profile.revision)
    with pytest.raises(ProfileRevisionConflict):
        service.update(profile.profile_id, name="Stale", parameters={}, expected_revision=profile.revision)
    candidate = service.propose([{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    service.accept(candidate, expected_revision=None)
    with pytest.raises(ProfileRevisionConflict):
        service.accept(candidate, expected_revision="0" * 64)
    assert service.repository.get_profile(profile.profile_id).name == "First"


def test_repeated_filter_instances_keep_order_and_unique_slots(service):
    plugin = service.catalog.legacy("filter", "BASELINE_CORRECTION")
    profile = service.create("filter", plugin.selection, "Baseline", {})
    selections = [{"slot": f"processing.filter.{uuid4()}", "profile_id": profile.profile_id}
                  for _ in range(2)]
    snapshot = service.propose(selections)
    assert [binding.slot for binding in snapshot.bindings] == [item["slot"] for item in selections]
    assert snapshot.revision != service.propose(list(reversed(selections))).revision
    assert service.materialize(snapshot)["ALGORITHM.filters"] == ["BASELINE_CORRECTION"] * 2


def test_profile_parameters_are_validated_by_the_selected_package(service):
    with pytest.raises(PluginSchemaError, match="sample_rate"):
        create_source(service, sample_rate=True)
    with pytest.raises(PluginSchemaError, match="invented"):
        create_source(service, invented=42)


def test_running_guard_blocks_accept_but_allows_saving(service):
    @contextmanager
    def running_guard(requires_idle):
        if requires_idle:
            raise ProfileRevisionConflict("Engine is running")
        yield
    service.change_guard = running_guard
    profile = create_source(service)
    candidate = service.propose([{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    with pytest.raises(ProfileRevisionConflict, match="running"):
        service.accept(candidate, expected_revision=None)
    assert service.repository.get_activation() is None
    service.update(profile.profile_id, name="Edited while running", parameters={}, expected_revision=profile.revision)


def test_delete_requires_replacing_selected_binding(service):
    profile = create_source(service)
    candidate = service.propose([{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    service.accept(candidate, expected_revision=None)
    with pytest.raises(ProfileRevisionConflict, match="selected"):
        service.delete(profile.profile_id, expected_revision=profile.revision)
