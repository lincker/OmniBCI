"""References block destructive package lifecycle changes; version copies are explicit."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from omnibci.application.plugin_packages import PackageReference, PluginPackageConflictError
from omnibci.application.plugin_profiles import ActivationSnapshot, ProfileBinding, ProfileRevisionConflict
from omnibci.application.plugin_schema import PluginSchemaError, validate_parameters
from omnibci.application.services.plugin_package_service import PluginPackageService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.package_lifecycle_store import ManagedPluginPackageStore
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
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
    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(), settings_source=lambda: {})
    svc = PluginPackageService(store, profiles, runtime_references=lambda: state["references"], admission_guard=guard)
    for version in ("1.0.0", "2.0.0"):
        svc.install(package_archive(tmp_path, version))
    svc.set_enabled("acme.device", "1.0.0", True)
    return svc, state


def source_profile(service):
    selection = service.store.get_package("acme.device", "1.0.0").contributions[0].selection
    return service.profiles.create("effector", selection, "Output", {"gain": 2.0})


def complete_snapshot(profiles, snapshot):
    bindings = []
    for slot, kind, plugin_type in (
        ("acquisition.source", "acquisition", "file_reader"),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40FBCCA"),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ):
        definition = next(item for item in profiles.catalog.list(kind) if item.plugin_type == plugin_type)
        bindings.append(ProfileBinding(
            slot, kind, None, definition.selection, validate_parameters(definition.schema, {}),
            configuration_version=definition.configuration_version,
        ))
    return ActivationSnapshot(settings=snapshot.settings, bindings=(*bindings, *snapshot.bindings))


def test_saved_profile_and_accepted_binding_references_are_listed_and_block_disable(service):
    svc, _ = service
    profile = source_profile(svc)
    active = complete_snapshot(svc.profiles, svc.profiles.propose([
        {"slot": "actuation.effector", "profile_id": profile.profile_id},
    ]))
    svc.profiles.accept(active, expected_revision=None)
    references = svc.list()[0].references
    assert {item.scope for item in references} == {"profile", "accepted"}
    with pytest.raises(PluginPackageConflictError, match="profile:.*accepted:"):
        svc.set_enabled("acme.device", "1.0.0", False)
    with pytest.raises(PluginPackageConflictError):
        svc.remove("acme.device", "1.0.0")
    assert svc.profiles.repository.get_activation() == active
    assert svc.store.get_package("acme.device", "1.0.0").enabled


@pytest.mark.parametrize("scope", ["running", "pending_apply"])
def test_runtime_and_pending_references_checked_inside_admission(service, scope):
    svc, state = service
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
    svc, _ = service
    source_profile(svc)
    svc.remove("acme.device", "2.0.0")
    assert len(svc.list()) == 1


def test_unversioned_reference_conservatively_blocks_removal(service):
    svc, state = service
    selection = svc.store.get_package("acme.device", "1.0.0").contributions[0].selection
    state["references"] = [PackageReference("running", "legacy", replace(selection, version=None))]
    with pytest.raises(PluginPackageConflictError):
        svc.remove("acme.device", "2.0.0")


def test_busy_apply_rejects_package_mutations(service):
    svc, state = service
    state["busy"] = True
    with pytest.raises(ProfileRevisionConflict, match="Apply"):
        svc.set_enabled("acme.device", "2.0.0", True)
    with pytest.raises(ProfileRevisionConflict):
        svc.remove("acme.device", "2.0.0")


def test_proposal_and_new_profile_do_not_mutate_accepted_or_source_profile(service):
    svc, _ = service
    original = source_profile(svc)
    snapshot = complete_snapshot(svc.profiles, svc.profiles.propose([
        {"slot": "actuation.effector", "profile_id": original.profile_id},
    ]))
    svc.profiles.accept(snapshot, expected_revision=None)
    proposal = svc.propose_profile(original.profile_id, "2.0.0", name="Output v2")
    assert proposal.parameters == {"gain": 2.0} and not proposal.target_enabled
    assert len(svc.profiles.repository.list_profiles()) == 1
    with pytest.raises(PluginPackageConflictError, match="Enable"):
        svc.create_version_profile(original.profile_id, "2.0.0", expected_source_revision=1)
    svc.set_enabled("acme.device", "2.0.0", True)
    new = svc.create_version_profile(original.profile_id, "2.0.0", expected_source_revision=1, name="Output v2")
    assert new.profile_id != original.profile_id and new.selection.version == "2.0.0"
    assert new.parameters == original.parameters
    assert svc.profiles.repository.get_profile(original.profile_id) == original
    assert svc.profiles.repository.get_activation() == snapshot
    rollback = svc.propose_profile(new.profile_id, "1.0.0")
    assert rollback.selection.version == "1.0.0" and rollback.parameters == original.parameters


def test_profile_copy_rejects_changed_source_revision(service):
    svc, _ = service
    original = source_profile(svc)
    svc.profiles.update(original.profile_id, name="Changed", parameters={"gain": 3.0}, expected_revision=1)
    with pytest.raises(ProfileRevisionConflict, match="preview"):
        svc.create_version_profile(original.profile_id, "2.0.0", expected_source_revision=1)
    assert len(svc.profiles.repository.list_profiles()) == 1


def test_new_schema_rejects_old_removed_parameters_and_accepts_explicit_replacement(service, tmp_path):
    svc, _ = service
    original = source_profile(svc)
    svc.install(package_archive(tmp_path, "3.0.0", properties={"level": {"type": "integer", "default": 5}}, schema_version=2))
    with pytest.raises(PluginSchemaError, match="gain"):
        svc.propose_profile(original.profile_id, "3.0.0")
    proposal = svc.propose_profile(original.profile_id, "3.0.0", parameters={"level": 8})
    assert proposal.parameters == {"level": 8} and proposal.configuration_version == 2
    assert svc.profiles.repository.get_profile(original.profile_id).parameters == {"gain": 2.0}


def test_added_target_defaults_are_visible_in_preview(service, tmp_path):
    svc, _ = service
    original = source_profile(svc)
    svc.install(package_archive(tmp_path, "3.0.0", properties={
        "gain": {"type": "number", "default": 1.0}, "new_option": {"type": "boolean", "default": True},
    }))
    assert svc.propose_profile(original.profile_id, "3.0.0").parameters == {"gain": 2.0, "new_option": True}


@pytest.mark.parametrize("channel_input", [None, ProcessingInput(outputs=(ChannelBinding("O2", "channel-1"), ChannelBinding("O1", "channel-0")))])
def test_decoder_version_copy_preserves_input_including_legacy_absence(service, monkeypatch, channel_input):
    svc, _ = service
    definition = svc.profiles.catalog.legacy("decoder", "THUBCI_SSVEP_classic40CCA")
    source = svc.profiles.create("decoder", definition.selection, "Decoder", {}, processing_input=channel_input)
    target = replace(definition, selection=replace(definition.selection, version="99.0.0"))
    monkeypatch.setattr(svc.store, "get_package", lambda *_: SimpleNamespace(error=None, available=True, enabled=True))
    monkeypatch.setattr(svc.store, "configuration", lambda *_: target)
    monkeypatch.setattr(svc.profiles.catalog, "get", lambda *_: target)

    proposal = svc.propose_profile(source.profile_id, "99.0.0")
    copied = svc.create_version_profile(source.profile_id, "99.0.0", expected_source_revision=source.revision)
    assert proposal.processing_input == channel_input
    assert copied.processing_input == channel_input
    assert copied.profile_id != source.profile_id
    assert svc.profiles.repository.get_profile(source.profile_id) == source


def test_acquisition_version_copy_discards_retired_montage(service, monkeypatch):
    svc, _ = service
    definition = svc.profiles.catalog.legacy("acquisition", "file_reader")
    montage = {"source_id": "file_reader", "sample_rate": 500, "channels": [{"index": 0, "name": "O1"}]}
    source = svc.profiles.create("acquisition", definition.selection, "Reader", {
        "sample_rate": 500, "channel_count": 2})
    source = svc.profiles.repository.save_profile(replace(source, legacy={"acquisition_montage": montage, "manual_montage": montage}),
                                                   expected_revision=source.revision)
    target = replace(definition, selection=replace(definition.selection, version="99.0.0"))
    monkeypatch.setattr(svc.store, "get_package", lambda *_: SimpleNamespace(error=None, available=True, enabled=True))
    monkeypatch.setattr(svc.store, "configuration", lambda *_: target)
    monkeypatch.setattr(svc.profiles.catalog, "get", lambda *_: target)

    proposal = svc.propose_profile(source.profile_id, "99.0.0")
    assert not hasattr(proposal, "manual_montage")
    copied = svc.create_version_profile(source.profile_id, "99.0.0", expected_source_revision=source.revision)
    assert copied.legacy == {}
    changed = svc.propose_profile(source.profile_id, "99.0.0", parameters={**source.parameters, "sample_rate": 250})
    assert changed.parameters["sample_rate"] == 250


def test_after_all_references_are_released_disable_and_remove_work(service):
    svc, _ = service
    original = source_profile(svc)
    svc.profiles.delete(original.profile_id, expected_revision=1)
    svc.set_enabled("acme.device", "1.0.0", False)
    svc.remove("acme.device", "1.0.0")
    assert [item.version for item in svc.list()] == ["2.0.0"]
