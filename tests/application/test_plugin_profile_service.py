"""Saving, selecting, and resolving configuration have distinct effects."""
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import ActivationSnapshot, PluginSelection, ProfileBinding, ProfileRevisionConflict
from omnibci.application.processing_source import processing_input
from omnibci.domain.montage import ChannelDescriptor, ProcessingInput, ReferenceSpec, SourceMetadata
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


def test_decoder_defaults_initialize_once_and_never_replace_explicit_all_channels(service, monkeypatch):
    from omnibci.domain.decoder_input import DecoderChannelRequirements
    definition = service.catalog.legacy("decoder", "THUBCI_SSVEP_classic40CCA")
    definition = replace(definition, channel_requirements=DecoderChannelRequirements(default_names=("O1", "Oz")))
    monkeypatch.setattr(service.catalog, "get", lambda *args: definition)
    initialized = service.create("decoder", definition.selection, "Defaults", {})
    assert initialized.processing_input.outputs == ("O1", "Oz")
    explicit = service.create("decoder", definition.selection, "All channels", {}, processing_input=ProcessingInput())
    assert explicit.processing_input.outputs == ()
    definition = replace(definition, channel_requirements=DecoderChannelRequirements(default_names=("O2",)))
    retained = service.update(initialized.profile_id, name="Renamed", parameters=initialized.parameters,
                              expected_revision=initialized.revision)
    assert retained.processing_input.outputs == ("O1", "Oz")
    retained_all = service.update(explicit.profile_id, name="Still all", parameters=explicit.parameters,
                                  expected_revision=explicit.revision)
    assert retained_all.processing_input.outputs == ()


def _source_montage(*, missing=False):
    return SourceMetadata("file_reader", 250, tuple(
        ChannelDescriptor(index, f"CH{index + 1}" if missing else name,
                          signal_type="unknown" if missing else "eeg",
                          unit=None if missing else "V", name_provenance="generated" if missing else None)
        for index, name in enumerate(("O1", "O2"))
    ), 2, connection_id="connection-one", provenance="file-reported")


def _source_binding(profile):
    return ProfileBinding("acquisition.source", "acquisition", profile.profile_id,
                          profile.selection, profile.parameters, profile.configuration_version,
                          profile.revision, manual_montage=profile.manual_montage)


def test_observed_montage_cache_is_idempotent_and_does_not_apply_configuration(service, monkeypatch):
    profile = create_source(service, sample_rate=250, channel_number=3)
    selections = complete_selections(service, [{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    accepted = service.accept(service.propose(selections), expected_revision=None)
    activation_bytes = (service.repository.root / "activation.toml").read_bytes()
    binding = _source_binding(profile)
    monkeypatch.setattr(service, "change_guard", lambda *_: pytest.fail("Acquisition reads must not take the engine change guard"))

    cached = service.record_acquisition_montage(binding, _source_montage())
    assert cached.revision == profile.revision + 1
    assert cached.acquisition_montage.connection_id == ""
    repeated = service.record_acquisition_montage(binding, replace(_source_montage(), connection_id="reconnected"))
    assert repeated == cached
    assert service.repository.get_activation() == accepted
    assert (service.repository.root / "activation.toml").read_bytes() == activation_bytes
    assert service.propose(selections).revision == accepted.revision
    assert "ACQUISITION.manual_montage" not in service.resolve_settings()


@pytest.mark.parametrize("change", ["no_profile", "other_profile", "parameters", "selection", "configuration_version", "deleted"])
def test_observation_never_overwrites_an_unrelated_or_changed_saved_profile(service, change):
    profile = create_source(service, sample_rate=250, channel_number=3)
    binding = _source_binding(profile)
    if change == "no_profile":
        binding = replace(binding, profile_id=None, profile_revision=0)
    elif change == "other_profile":
        binding = replace(binding, profile_id=str(uuid4()))
    elif change == "parameters":
        binding = replace(binding, parameters={**binding.parameters, "sample_rate": 500})
    elif change == "selection":
        binding = replace(binding, selection=replace(binding.selection, version="different"))
    elif change == "configuration_version":
        binding = replace(binding, configuration_version=2)
    else:
        service.delete(profile.profile_id, expected_revision=profile.revision)
    assert service.record_acquisition_montage(binding, _source_montage()) is None
    if change != "deleted":
        assert service.repository.get_profile(profile.profile_id) == profile


def test_observation_cas_conflict_preserves_concurrent_user_edit(service, monkeypatch):
    profile = create_source(service, sample_rate=250, channel_number=3)
    save = service.repository.save_profile

    def competing_save(candidate, expected_revision):
        save(replace(profile, name="User edit"), expected_revision=profile.revision)
        return save(candidate, expected_revision=expected_revision)

    monkeypatch.setattr(service.repository, "save_profile", competing_save)
    assert service.record_acquisition_montage(_source_binding(profile), _source_montage()) is None
    retained = service.repository.get_profile(profile.profile_id)
    assert retained.name == "User edit"
    assert retained.acquisition_montage is None


def test_regular_edit_retains_observation_and_parameter_change_invalidates_it(service):
    profile = create_source(service, sample_rate=250, channel_number=3)
    cached = service.record_acquisition_montage(_source_binding(profile), _source_montage())
    renamed = service.update(cached.profile_id, name="Renamed", parameters=cached.parameters,
                             expected_revision=cached.revision)
    assert renamed.acquisition_montage == cached.acquisition_montage
    changed = service.update(renamed.profile_id, name=renamed.name,
                             parameters={**renamed.parameters, "data_file_path": "another.cnt"},
                             expected_revision=renamed.revision)
    assert changed.acquisition_montage is None
    with pytest.raises(TypeError, match="acquisition_montage"):
        service.update(changed.profile_id, name=changed.name, parameters=changed.parameters,
                       expected_revision=changed.revision, acquisition_montage=_source_montage())


def test_manual_montage_is_saved_selected_and_materialized_independently(service):
    definition = service.catalog.legacy("acquisition", "file_reader")
    profile = service.create("acquisition", definition.selection, "Manual", {"sample_rate": 250, "channel_number": 3},
                             manual_montage=_source_montage())
    assert profile.manual_montage.provenance == "user-declared"
    selections = complete_selections(service, [{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    accepted = service.accept(service.propose(selections), expected_revision=None)
    frozen = next(binding for binding in accepted.bindings if binding.kind == "acquisition")
    assert frozen.manual_montage == profile.manual_montage
    assert service.resolve_settings()["ACQUISITION.manual_montage"] == profile.manual_montage.to_dict()
    renamed = service.update(profile.profile_id, name="Renamed", parameters=profile.parameters,
                             expected_revision=profile.revision)
    assert renamed.manual_montage == profile.manual_montage
    cleared = service.update(profile.profile_id, name=renamed.name, parameters=renamed.parameters,
                             expected_revision=renamed.revision, manual_montage=None)
    assert cleared.manual_montage is None
    assert service.repository.get_activation() == accepted
    assert service.propose(selections).revision != accepted.revision
    assert service.resolve_settings()["ACQUISITION.manual_montage"] == profile.manual_montage.to_dict()


@pytest.mark.parametrize("change", ["source", "rate", "rows"])
def test_manual_montage_must_match_its_acquisition_profile(service, change):
    observed = _source_montage()
    if change == "source":
        observed = replace(observed, source_id="another-source")
    elif change == "rate":
        observed = replace(observed, sample_rate=500)
    else:
        observed = replace(observed, channels=observed.channels[:1], trigger_row_index=1)
    definition = service.catalog.legacy("acquisition", "file_reader")
    with pytest.raises(PluginSchemaError, match="Manual montage"):
        service.create("acquisition", definition.selection, "Invalid", {"sample_rate": 250, "channel_number": 3},
                       manual_montage=observed)


def test_manual_montage_fills_missing_fields_but_cannot_replace_observed_labels(service):
    profile = create_source(service, sample_rate=250, channel_number=3)
    cached = service.record_acquisition_montage(_source_binding(profile), _source_montage(missing=True))
    supplemented = service.update(cached.profile_id, name=cached.name, parameters=cached.parameters,
                                  expected_revision=cached.revision, manual_montage=_source_montage())
    assert supplemented.acquisition_montage.channels[0].name == "CH1"
    assert supplemented.manual_montage.channels[0].name == "O1"
    observed = service.record_acquisition_montage(_source_binding(supplemented), _source_montage())
    conflicting = replace(_source_montage(), channels=(replace(_source_montage().channels[0], name="C3"), _source_montage().channels[1]))
    with pytest.raises(PluginSchemaError, match="changed"):
        service.update(observed.profile_id, name=observed.name, parameters=observed.parameters,
                       expected_revision=observed.revision, manual_montage=conflicting)
    assert service.repository.get_profile(observed.profile_id) == observed


def test_parameter_edits_require_manual_montage_to_remain_consistent_or_be_cleared(service):
    definition = service.catalog.legacy("acquisition", "file_reader")
    profile = service.create("acquisition", definition.selection, "Manual", {"sample_rate": 250, "channel_number": 3},
                             manual_montage=_source_montage())
    parameters = {**profile.parameters, "sample_rate": 500}
    with pytest.raises(PluginSchemaError, match="rate and output rows"):
        service.update(profile.profile_id, name=profile.name, parameters=parameters,
                       expected_revision=profile.revision)
    updated = service.update(profile.profile_id, name=profile.name, parameters=parameters,
                             expected_revision=profile.revision, manual_montage=None)
    assert updated.manual_montage is None


REQUIRED_SELECTIONS = (
    ("acquisition.source", "acquisition", "file_reader"),
    ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
    ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
    ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
)


def complete_selections(service, selections=()):
    selections = list(selections)
    slots = {item["slot"] for item in selections}
    current = service.repository.get_activation()
    previous = {item.slot: item.profile_id for item in current.bindings} if current else {}
    for slot, kind, alias in REQUIRED_SELECTIONS:
        if slot in slots:
            continue
        profile_id = previous.get(slot)
        if profile_id is None:
            plugin = service.catalog.legacy(kind, alias)
            profile_id = service.create(kind, plugin.selection, f"Required {kind}", {}).profile_id
        selections.append({"slot": slot, "profile_id": profile_id})
    return selections


@pytest.mark.parametrize("outputs", [("O2", "O1"), ({"name": "O2", "source": "CH2"}, "O1")])
def test_decoder_inputs_switch_independently_and_resolve_against_current_source(service, outputs):
    definition = service.catalog.legacy("decoder", "THUBCI_SSVEP_classic40CCA")
    first_input = ProcessingInput.from_dict({"outputs": list(outputs),
        "reference": {"mode": "single", "channels": ["M1"]}, "excluded_channels": ["Oz"]})
    first = service.create("decoder", definition.selection, "Visual", {}, processing_input=first_input)
    assert service.repository.get_profile(first.profile_id).processing_input == first_input
    second = service.create("decoder", definition.selection, "Motor", {},
                             processing_input=ProcessingInput(outputs=("C3", "C4")))
    selections = complete_selections(service, [{"slot": "processing.decoder", "profile_id": first.profile_id}])
    accepted = service.accept(service.propose(selections), expected_revision=None)
    assert processing_input(service.resolve_settings()) == first_input
    assert "SIGNAL.setup" not in service.resolve_settings()

    edited = service.update(first.profile_id, name=first.name, parameters=first.parameters,
                            expected_revision=first.revision, processing_input=ProcessingInput(outputs=("Oz",)))
    assert processing_input(service.resolve_settings()) == first_input
    other_selections = [dict(item, profile_id=second.profile_id) if item["slot"] == "processing.decoder" else item
                        for item in selections]
    second_activation = service.accept(service.propose(other_selections), expected_revision=accepted.revision)
    assert processing_input(service.resolve_settings()) == second.processing_input
    assert "SIGNAL.setup" not in service.resolve_settings()

    source_definition = service.catalog.legacy("acquisition", "neuroscan_curry8_reader")
    source = service.create("acquisition", source_definition.selection, "Device", {})
    changed_source = [dict(item, profile_id=source.profile_id) if item["slot"] == "acquisition.source" else item
                      for item in selections]
    service.accept(service.propose(changed_source), expected_revision=second_activation.revision)
    assert processing_input(service.resolve_settings()) == edited.processing_input
    assert service.resolve_settings()["ACQUISITION.signal_source"] == "neuroscan_curry8_reader"
    assert "SIGNAL.setup" not in service.resolve_settings()
    fresh = PluginProfileService(TomlPluginProfileRepository(service.repository.root), service.catalog,
                                  settings_source=service.settings_source)
    assert fresh.resolve_settings() == service.resolve_settings()
    assert fresh.repository.get_profile(first.profile_id).processing_input == edited.processing_input
    assert fresh.repository.get_profile(second.profile_id).processing_input == second.processing_input


def test_legacy_decoder_input_is_read_unchanged_until_explicitly_configured(service):
    legacy_setup = {"source_id": "file_reader", "outputs": ["Occipital"],
                    "assignments": [{"source_index": 0, "electrode": "Occipital", "expected_name": "O1"}]}
    service.settings_source = lambda: {"SIGNAL.setup": legacy_setup}
    selections = complete_selections(service)
    decoder_id = next(item["profile_id"] for item in selections if item["slot"] == "processing.decoder")
    decoder = service.repository.get_profile(decoder_id)
    legacy = service.repository.save_profile(replace(decoder, processing_input=None),
                                              expected_revision=decoder.revision)
    accepted = service.accept(service.propose(selections), expected_revision=None)
    renamed = service.update(legacy.profile_id, name="Legacy renamed", parameters=legacy.parameters,
                              expected_revision=legacy.revision)
    assert renamed.processing_input is None
    assert service.resolve_settings()["SIGNAL.setup"] == legacy_setup
    assert service.propose(selections).revision == accepted.revision

    configured = service.update(renamed.profile_id, name=renamed.name, parameters=renamed.parameters,
                                expected_revision=renamed.revision, processing_input=None)
    assert configured.processing_input == ProcessingInput()
    assert service.resolve_settings()["SIGNAL.setup"] == legacy_setup
    selected = service.accept(service.propose(selections), expected_revision=accepted.revision)
    assert selected.settings["SIGNAL.setup"] == legacy_setup
    assert processing_input(service.resolve_settings()) == ProcessingInput()
    assert "SIGNAL.setup" not in service.resolve_settings()


def test_new_decoder_profile_has_explicit_input_and_ignores_global_legacy_setup(service):
    service.settings_source = lambda: {"SIGNAL.setup": {"source_id": "other-device", "outputs": ["Old"]}}
    selections = complete_selections(service)
    selected = service.accept(service.propose(selections), expected_revision=None)
    decoder = next(binding for binding in selected.bindings if binding.kind == "decoder")
    assert decoder.processing_input == ProcessingInput()
    assert processing_input(service.resolve_settings()) == ProcessingInput()
    assert "SIGNAL.setup" not in service.resolve_settings()


def test_processing_input_cannot_be_saved_to_an_acquisition_profile(service):
    from omnibci.application.plugin_profiles import PluginProfileValidationError

    profile = create_source(service)
    with pytest.raises(PluginProfileValidationError, match="decoder"):
        service.update(profile.profile_id, name=profile.name, parameters=profile.parameters,
                       expected_revision=profile.revision, processing_input=ProcessingInput())
    assert service.repository.get_profile(profile.profile_id) == profile


def test_save_never_changes_accepted_values_even_for_active_profile(service):
    profile = create_source(service)
    selections = complete_selections(service, [{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    candidate = service.propose(selections)
    service.accept(candidate, expected_revision=None)
    before = service.resolve_settings()
    changed = service.update(profile.profile_id, name="Changed", parameters={"sample_rate": 250},
                             expected_revision=profile.revision)
    assert changed.revision == profile.revision + 1
    assert service.resolve_settings() == before
    assert before["ACQUISITION.sample_rate"] == 1000
    assert before["PLUGINS.bindings"][0]["parameters"]["sample_rate"] == 1000

    next_snapshot = service.propose(selections)
    service.accept(next_snapshot, expected_revision=candidate.revision)
    assert service.resolve_settings()["ACQUISITION.sample_rate"] == 250
    assert service.resolve_settings()["CONFIG.revision"] != before["CONFIG.revision"]


def test_profile_and_activation_conflicts_do_not_overwrite(service):
    profile = create_source(service)
    service.update(profile.profile_id, name="First", parameters={}, expected_revision=profile.revision)
    with pytest.raises(ProfileRevisionConflict):
        service.update(profile.profile_id, name="Stale", parameters={}, expected_revision=profile.revision)
    candidate = service.propose(complete_selections(service, [
        {"slot": "acquisition.source", "profile_id": profile.profile_id},
    ]))
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


def test_new_activation_and_worker_settings_exclude_backend_logging(service):
    host = {"host": "original", "LOGGING.level": "INFO", "LOGGING.file_path": "backend.log"}
    service.settings_source = lambda: host
    selections = complete_selections(service)

    proposed = service.propose(selections)
    assert proposed.settings == {"host": "original"}

    previous = ActivationSnapshot(host, proposed.bindings)
    restored = ActivationSnapshot.from_dict(previous.to_dict())
    assert restored.revision == previous.revision
    assert restored.settings == host
    worker_settings = service.materialize(restored)
    assert not any(key.startswith("LOGGING.") for key in worker_settings)
    assert worker_settings["CONFIG.revision"] == previous.revision
    assert host["LOGGING.level"] == "INFO"


LEGACY_PLUGIN_SETTINGS = {
    "ACQUISITION.signal_source": "old_source",
    "ACQUISITION.sample_rate": 500,
    "ACQUISITION.channel_number": 99,
    "ACQUISITION.DATA_PROVIDER_IP": "192.0.2.1",
    "ACQUISITION.DATA_PROVIDER_PORT": 9000,
    "ACQUISITION.data_file_path": "old.cnt",
    "ACQUISITION.file_reader_chunk_size": 999,
    "ACQUISITION.trigger": "old_trigger",
    "ACQUISITION.parallel_port_address": "0x378",
    "ALGORITHM.decoder": "old_decoder",
    "ALGORITHM.epoch_detector": "old_detector",
    "ALGORITHM.filters": ["old_filter"],
    "ALGORITHM.window_duration": 99.0,
    "ALGORITHM.offset_time": 9.0,
    "STIMULATION.paradigm": "old_paradigm",
    "ACTUATION.effector": "old_effector",
    "ssvep.duration": 88.0,
    "ssvep.offset_time": 8.0,
    "ssvep.sample_rate": 800,
}


def test_new_activation_excludes_plugin_settings_but_keeps_host_and_context(service):
    host = {
        "ALGORITHM.baseline_duration": 0.4,
        "NEUROENGINE.queue_size": 29,
        "ACQUISITION.channel_names": ["C3", "C4"],
        "custom.values": ["retained"],
    }
    inputs = {**host, **LEGACY_PLUGIN_SETTINGS}
    service.settings_source = lambda: inputs
    selections = complete_selections(service)

    candidate = service.propose(selections)

    assert candidate.settings == host
    assert service.propose(selections, include_host_settings=True).settings == host
    assert inputs == {**host, **LEGACY_PLUGIN_SETTINGS}
    assert service.repository.get_activation() is None


def test_old_plugin_setting_copies_are_inert_and_cannot_change_accepted_revision(service):
    selections = complete_selections(service)
    proposed = service.propose(selections)
    previous = ActivationSnapshot({
        "host": "original", **LEGACY_PLUGIN_SETTINGS, "LOGGING.level": "INFO",
    }, proposed.bindings)
    service.accept(previous, expected_revision=None)
    original_files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}
    inputs = {"host": "original", **{key: "changed" for key in LEGACY_PLUGIN_SETTINGS},
              "LOGGING.level": "INFO"}
    service.settings_source = lambda: inputs

    candidate = service.propose(selections, include_host_settings=True, refresh_slots=[])
    restored = ActivationSnapshot.from_dict(previous.to_dict())

    assert candidate == previous
    assert candidate.revision == restored.revision == previous.revision
    assert candidate.settings == previous.settings
    assert service.repository.get_activation() == previous
    assert {path: path.read_bytes() for path in original_files} == original_files


def test_materialization_uses_bindings_instead_of_old_plugin_setting_copies(service):
    source = create_source(service, sample_rate=256, channel_number=5)
    selections = complete_selections(service, [{"slot": "acquisition.source", "profile_id": source.profile_id}])
    proposed = service.propose(selections)
    host = {"ALGORITHM.baseline_duration": 0.4, "ACQUISITION.channel_names": ["C3", "C4"]}
    previous = ActivationSnapshot({**host, **LEGACY_PLUGIN_SETTINGS}, proposed.bindings)

    runtime = service.materialize(previous)

    derived = {
        "ACQUISITION.signal_source", "ACQUISITION.sample_rate", "ACQUISITION.channel_number",
        "ACQUISITION.data_file_path", "ACQUISITION.file_reader_chunk_size", "ALGORITHM.decoder",
        "ALGORITHM.epoch_detector", "ALGORITHM.filters", "STIMULATION.paradigm",
    }
    assert not (set(LEGACY_PLUGIN_SETTINGS) - derived) & runtime.keys()
    assert runtime["ACQUISITION.sample_rate"] == 256
    assert runtime["ACQUISITION.channel_number"] == 5
    assert runtime["ACQUISITION.signal_source"] == "file_reader"
    assert runtime["ALGORITHM.decoder"] == "THUBCI_SSVEP_classic40CCA"
    assert runtime["ALGORITHM.filters"] == []
    assert runtime["CONFIG.revision"] == previous.revision
    assert {key: runtime[key] for key in host} == host
    assert previous.settings == {**host, **LEGACY_PLUGIN_SETTINGS}
    assert runtime["PLUGINS.bindings"] == [binding.to_dict() for binding in previous.bindings]


def test_flat_plugin_edits_do_not_change_a_clean_activation(service):
    selections = complete_selections(service)
    before = service.propose(selections)
    service.accept(before, expected_revision=None)

    service.settings_source = lambda: {"host": "original", **LEGACY_PLUGIN_SETTINGS}
    candidate = service.propose(selections, include_host_settings=True, refresh_slots=[])

    assert candidate == before
    assert candidate.revision == before.revision
    assert service.materialize(candidate)["ACQUISITION.sample_rate"] == 1000


INERT_HOST_SETTINGS = {
    "CONFIG.initialization_warnings": ["Original warning"],
    "CONFIG.revision": "old-revision-copy",
    "CONFIG.generation": "old-generation-copy",
    "PROFILES.active": "old-profile",
    "PLUGINS.bindings": [],
    "LOGGING.custom": "old-backend-copy",
    "ACQUISITION.file_reader_play_speed": 1.0,
    "project_name": "Old name",
    "version": "0.0.1",
    "CONSTANT.ASYNC_SLEEP_TIME": 0.01,
    "NEUROENGINE.COMMAND_SERVER_IP": "192.0.2.1",
    "NEUROENGINE.COMMAND_SERVER_PORT": 8002,
}


def test_diagnostics_and_retired_fields_are_inert_without_rewriting_accepted_storage(service):
    selections = complete_selections(service)
    proposed = service.propose(selections)
    previous = ActivationSnapshot({**proposed.settings, **INERT_HOST_SETTINGS}, proposed.bindings)
    service.accept(previous, expected_revision=None)
    original_files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}
    service.settings_source = lambda: {"host": "original", **{key: "changed" for key in INERT_HOST_SETTINGS}}

    candidate = service.propose(selections, include_host_settings=True)
    runtime = service.materialize(candidate)

    assert candidate == previous
    assert candidate.revision == previous.revision
    assert not (INERT_HOST_SETTINGS.keys() - {"CONFIG.revision", "PLUGINS.bindings"}) & runtime.keys()
    assert runtime["CONFIG.revision"] == previous.revision
    assert runtime["PLUGINS.bindings"] == [binding.to_dict() for binding in previous.bindings]
    assert {path: path.read_bytes() for path in original_files} == original_files


def test_new_candidates_do_not_include_retired_diagnostic_or_backend_settings(service):
    service.settings_source = lambda: {
        "NEUROENGINE.queue_size": 64, **INERT_HOST_SETTINGS,
        "LOGGING.level": "INFO", "LOGGING.file_path": "backend.log",
        "NEUROENGINE.command_timeout_s": 20.0,
        "PERSISTENCE.session_repository": "in_memory", "PLUGINS.user_dir": "/plugins",
    }
    candidate = service.propose(complete_selections(service))
    assert candidate.settings == {"NEUROENGINE.queue_size": 64}


def test_host_settings_are_included_only_when_explicitly_requested(service):
    selections = complete_selections(service)
    previous = service.accept(service.propose(selections), expected_revision=None)
    service.settings_source = lambda: {"host": "original", "NEUROENGINE.queue_size": 64}

    assert service.propose(selections) == previous
    candidate = service.propose(selections, include_host_settings=True)
    assert candidate.settings == {"host": "original", "NEUROENGINE.queue_size": 64}
    assert candidate.revision != previous.revision
    assert service.repository.get_activation() == previous


def test_saved_global_signal_setup_cannot_be_added_to_an_existing_activation(service):
    selections = complete_selections(service)
    service.settings_source = lambda: {"NEUROENGINE.queue_size": 64}
    previous = service.accept(service.propose(selections), expected_revision=None)
    signal = {"source_id": "test-source", "outputs": ["O1"]}
    service.settings_source = lambda: {"NEUROENGINE.queue_size": 128, "SIGNAL.setup": signal}

    assert service.propose(selections) == previous
    candidate = service.propose(selections, include_host_settings=True, refresh_slots=[])

    assert candidate.settings == {"NEUROENGINE.queue_size": 128}
    assert candidate.bindings == previous.bindings
    assert candidate.revision != previous.revision
    assert service.repository.get_activation() == previous


def test_general_host_refresh_preserves_unreviewed_signal_setup(service):
    signal = {"source_id": "test-source", "outputs": ["O1"]}
    service.settings_source = lambda: {"NEUROENGINE.queue_size": 64, "SIGNAL.setup": signal}
    selections = complete_selections(service)
    previous = service.accept(service.propose(selections), expected_revision=None)
    service.settings_source = lambda: {
        "NEUROENGINE.queue_size": 128,
        "SIGNAL.setup": {"source_id": "test-source", "outputs": ["O2"]},
    }

    candidate = service.propose(selections, include_host_settings=True, refresh_slots=[])

    assert candidate.settings == {"NEUROENGINE.queue_size": 128, "SIGNAL.setup": signal}
    assert service.repository.get_activation() == previous


def test_legacy_signal_setup_survives_removal_from_saved_host_settings(service):
    service.settings_source = lambda: {
        "NEUROENGINE.queue_size": 64, "SIGNAL.setup": {"source_id": "test-source"},
    }
    selections = complete_selections(service)
    previous = service.accept(service.propose(selections), expected_revision=None)
    service.settings_source = lambda: {"NEUROENGINE.queue_size": 128}

    candidate = service.propose(selections, include_host_settings=True, refresh_slots=[])

    assert candidate.settings == {"NEUROENGINE.queue_size": 128, "SIGNAL.setup": {"source_id": "test-source"}}
    assert "SIGNAL.setup" in previous.settings
    assert service.repository.get_activation() == previous


def test_saved_signal_setup_does_not_rewrite_legacy_activation_or_revision(service):
    selections = complete_selections(service)
    previous = service.accept(service.propose(selections), expected_revision=None)
    original_files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}
    service.settings_source = lambda: {
        "host": "original", "SIGNAL.setup": {"source_id": "test-source", "outputs": ["O1"]},
    }

    restored = ActivationSnapshot.from_dict(previous.to_dict())
    materialized = service.materialize(restored)
    candidate = service.propose(selections, include_host_settings=True, refresh_slots=[])

    assert "SIGNAL.setup" not in restored.settings
    assert processing_input(materialized) == ProcessingInput()
    assert "SIGNAL.setup" not in materialized
    assert restored.revision == previous.revision == candidate.revision
    assert service.repository.get_activation() == previous
    assert {path: path.read_bytes() for path in original_files} == original_files


def test_historical_decoder_uses_transient_identity_without_rewriting_profiles(service, monkeypatch):
    from omnibci.application.processing_source import requires_signal_preparation
    from omnibci.domain.decoder_input import DecoderChannelRequirements
    selections = complete_selections(service)
    decoder = service.repository.get_profile(selections[1]["profile_id"])
    legacy = service.repository.save_profile(replace(decoder, processing_input=None),
                                              expected_revision=decoder.revision)
    accepted = service.accept(service.propose(selections), expected_revision=None)
    original_files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}
    get = service.catalog.get

    def with_suggested_defaults(kind, selection):
        definition = get(kind, selection)
        return (replace(definition, channel_requirements=DecoderChannelRequirements(default_names=("Oz",)))
                if kind == "decoder" else definition)

    monkeypatch.setattr(service.catalog, "get", with_suggested_defaults)
    runtime = service.materialize(accepted)
    assert processing_input(runtime) == ProcessingInput()
    assert requires_signal_preparation(runtime)
    assert runtime["CONFIG.revision"] == accepted.revision
    assert service.repository.get_activation() == accepted
    assert service.repository.get_profile(legacy.profile_id) == legacy
    assert accepted.bindings[1].processing_input is None
    assert {path: path.read_bytes() for path in original_files} == original_files


@pytest.mark.parametrize("key,value", [
    ("LOGGING.level", "DEBUG"),
    ("LOGGING.file_path", "new-backend.log"),
    ("NEUROENGINE.command_timeout_s", 20.0),
    ("PERSISTENCE.session_repository", "other"),
    ("PLUGINS.user_dir", "/other-plugins"),
])
def test_backend_startup_edits_require_backend_restart(service, key, value):
    selections = complete_selections(service)
    previous = service.accept(service.propose(selections), expected_revision=None)
    original_files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}
    service.settings_source = lambda: {"host": "original", key: value}

    assert service.propose(selections) == previous
    with pytest.raises(PluginSchemaError, match=rf"Backend restart required for .*{key}.*engine restart"):
        service.propose(selections, include_host_settings=True)
    assert {path: path.read_bytes() for path in original_files} == original_files


def test_backend_restart_uses_loaded_values_instead_of_old_accepted_copies(service):
    selections = complete_selections(service)
    proposed = service.propose(selections)
    previous = ActivationSnapshot({**proposed.settings, "NEUROENGINE.command_timeout_s": 10.0},
                                  proposed.bindings)
    service.accept(previous, expected_revision=None)
    restarted = PluginProfileService(service.repository, service.catalog,
        settings_source=lambda: {"host": "original", "NEUROENGINE.command_timeout_s": 20.0})

    candidate = restarted.propose(selections, include_host_settings=True)

    assert candidate == previous
    assert "NEUROENGINE.command_timeout_s" not in restarted.materialize(candidate)


def test_backend_startup_resolver_can_identify_environment_overrides(service):
    inputs = {"host": "original", "PLUGINS.user_dir": "/ignored-before"}
    overridden = PluginProfileService(service.repository, service.catalog,
        settings_source=lambda: inputs,
        startup_settings_resolver=lambda settings: {"PLUGINS.user_dir": "/environment-plugins"})
    selections = complete_selections(overridden)
    previous = overridden.accept(overridden.propose(selections), expected_revision=None)
    inputs["PLUGINS.user_dir"] = "/ignored-after"

    assert overridden.propose(selections, include_host_settings=True) == previous


@pytest.mark.parametrize("inputs,key", [
    ({"host": "original", "vendor.setting": 2}, "vendor.setting"),
    ({"host": "changed"}, "host"),
    ({}, "host"),
])
def test_unknown_host_settings_cannot_be_added_changed_or_deleted(service, inputs, key):
    selections = complete_selections(service)
    previous = service.accept(service.propose(selections), expected_revision=None)
    service.settings_source = lambda: inputs

    assert service.propose(selections) == previous
    with pytest.raises(PluginSchemaError, match=rf"Unclassified host settings.*{key}"):
        service.propose(selections, include_host_settings=True)
    assert service.materialize(previous)["host"] == "original"
    assert service.repository.get_activation() == previous


@pytest.mark.parametrize("before,after", [
    (1, True),
    (1, 1.0),
    ({"nested": [1]}, {"nested": [True]}),
])
def test_unknown_host_settings_reject_type_changes(service, before, after):
    service.settings_source = lambda: {"vendor.setting": before}
    selections = complete_selections(service)
    previous = service.accept(service.propose(selections), expected_revision=None)
    original_files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}
    service.settings_source = lambda: {"vendor.setting": after}

    with pytest.raises(PluginSchemaError, match="Unclassified host settings.*vendor.setting"):
        service.propose(selections, include_host_settings=True)

    assert service.repository.get_activation() == previous
    assert {path: path.read_bytes() for path in original_files} == original_files


def test_unknown_host_settings_ignore_object_key_order(service):
    service.settings_source = lambda: {"vendor.setting": {"first": 1, "second": {"a": 2, "b": 3}}}
    selections = complete_selections(service)
    previous = service.accept(service.propose(selections), expected_revision=None)
    service.settings_source = lambda: {"vendor.setting": {"second": {"b": 3, "a": 2}, "first": 1},
                                      "NEUROENGINE.command_timeout_s": 10}

    assert service.propose(selections, include_host_settings=True) == previous


@pytest.mark.parametrize("value", [float("nan"), {1}])
def test_unknown_host_setting_serialization_errors_are_validation_errors(service, value):
    service.settings_source = lambda: {"vendor.setting": 1}
    selections = complete_selections(service)
    previous = service.accept(service.propose(selections), expected_revision=None)
    service.settings_source = lambda: {"vendor.setting": value}

    with pytest.raises(PluginSchemaError, match="Unclassified host settings must contain finite JSON values"):
        service.propose(selections, include_host_settings=True)

    assert service.repository.get_activation() == previous


def external_source_profile(service, monkeypatch, parameters):
    definition = service.catalog.legacy("acquisition", "file_reader")
    types = {int: "integer", float: "number", bool: "boolean", str: "string"}
    external = replace(definition,
        selection=PluginSelection("test.external-source", "source", "1.0.0"),
        plugin_type="external_source",
        schema={"type": "object", "additionalProperties": False,
                "properties": {key: {"type": types[type(value)]} for key, value in parameters.items()}},
    )
    original_get = service.catalog.get
    monkeypatch.setattr(service.catalog, "get", lambda kind, selection:
                        external if kind == "acquisition" and selection == external.selection
                        else original_get(kind, selection))
    return service.create("acquisition", external.selection, "External source", parameters)


@pytest.mark.parametrize("key", ["sample_rate", "channel_number"])
@pytest.mark.parametrize("value", [None, True, 0, -1, 1.5])
def test_source_runtime_metadata_must_be_declared_in_its_binding(service, monkeypatch, key, value):
    before = service.propose(complete_selections(service))
    service.accept(before, expected_revision=None)
    parameters = {"sample_rate": 250, "channel_number": 1}
    if value is None:
        parameters.pop(key)
    else:
        parameters[key] = value
    profile = external_source_profile(service, monkeypatch, parameters)
    selections = complete_selections(service, [{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    proposed = service.propose(selections)
    candidate = ActivationSnapshot({**proposed.settings, "ACQUISITION.sample_rate": 500,
                                    "ACQUISITION.channel_number": 16}, proposed.bindings)
    original_files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}

    with pytest.raises(PluginSchemaError, match=rf"acquisition.source.*{key}.*positive integer"):
        service.validate_activation(candidate)
    with pytest.raises(PluginSchemaError, match=rf"acquisition.source.*{key}.*positive integer"):
        service.materialize(candidate)
    with pytest.raises(PluginSchemaError, match=rf"acquisition.source.*{key}.*positive integer"):
        service.accept(candidate, expected_revision=before.revision)
    assert service.repository.get_activation() == before
    assert service.repository.get_profile(profile.profile_id) == profile
    assert {path: path.read_bytes() for path in original_files} == original_files


def test_resolving_old_source_without_metadata_fails_without_rewriting_storage(service, monkeypatch):
    profile = external_source_profile(service, monkeypatch, {})
    selections = complete_selections(service, [{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    candidate = service.propose(selections)
    previous = ActivationSnapshot({"ACQUISITION.sample_rate": 250, "ACQUISITION.channel_number": 16},
                                  candidate.bindings)
    service.repository.save_activation(previous, expected_revision=None)
    original_files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}

    with pytest.raises(PluginSchemaError, match="acquisition.source.*sample_rate.*positive integer"):
        service.resolve_settings()

    assert service.repository.get_activation() == previous
    assert {path: path.read_bytes() for path in original_files} == original_files


def test_external_source_can_declare_one_channel_without_an_event_row(service, monkeypatch):
    profile = external_source_profile(service, monkeypatch, {"sample_rate": 250, "channel_number": 1})
    selections = complete_selections(service, [{"slot": "acquisition.source", "profile_id": profile.profile_id}])
    accepted = service.accept(service.propose(selections), expected_revision=None)

    runtime = service.materialize(accepted)

    assert runtime["ACQUISITION.sample_rate"] == 250
    assert runtime["ACQUISITION.channel_number"] == 1


@pytest.mark.parametrize("channels", [2, 9, 10, 33, 65])
def test_scan45_profile_resolves_its_configured_channel_count(service, channels):
    plugin = service.catalog.legacy("acquisition", "neuroscan_scan45_reader")
    profile = service.create("acquisition", plugin.selection, "Device", {"channel_number": channels})
    candidate = service.propose(complete_selections(service, [
        {"slot": "acquisition.source", "profile_id": profile.profile_id},
    ]))
    service.accept(candidate, expected_revision=None)

    assert service.repository.get_profile(profile.profile_id).parameters["channel_number"] == channels
    assert service.resolve_settings()["ACQUISITION.channel_number"] == channels


@pytest.mark.parametrize("channels", [True, 0, 1, 3.5, "33"])
def test_scan45_profile_rejects_invalid_channel_count(service, channels):
    plugin = service.catalog.legacy("acquisition", "neuroscan_scan45_reader")
    with pytest.raises(PluginSchemaError, match="channel_number"):
        service.create("acquisition", plugin.selection, "Invalid", {"channel_number": channels})


def test_running_guard_blocks_accept_but_allows_saving(service):
    @contextmanager
    def running_guard(requires_idle):
        if requires_idle:
            raise ProfileRevisionConflict("Engine is running")
        yield
    service.change_guard = running_guard
    profile = create_source(service)
    candidate = service.propose(complete_selections(service, [
        {"slot": "acquisition.source", "profile_id": profile.profile_id},
    ]))
    with pytest.raises(ProfileRevisionConflict, match="running"):
        service.accept(candidate, expected_revision=None)
    assert service.repository.get_activation() is None
    service.update(profile.profile_id, name="Edited while running", parameters={}, expected_revision=profile.revision)


def test_delete_requires_replacing_selected_binding(service):
    profile = create_source(service)
    candidate = service.propose(complete_selections(service, [
        {"slot": "acquisition.source", "profile_id": profile.profile_id},
    ]))
    service.accept(candidate, expected_revision=None)
    with pytest.raises(ProfileRevisionConflict, match="selected"):
        service.delete(profile.profile_id, expected_revision=profile.revision)


@pytest.mark.parametrize("missing_slot", [None, *(item[0] for item in REQUIRED_SELECTIONS)])
def test_incomplete_activation_is_rejected_without_replacing_selected_values(service, missing_slot):
    before = service.propose(complete_selections(service))
    service.accept(before, expected_revision=None)
    bindings = tuple(binding for binding in before.bindings if missing_slot and binding.slot != missing_slot)
    candidate = ActivationSnapshot(before.settings, bindings)

    with pytest.raises(PluginSchemaError, match="Missing required selections"):
        service.accept(candidate, expected_revision=before.revision)

    assert service.repository.get_activation() == before


def test_enabled_actuation_requires_an_effector_without_replacing_selected_values(service):
    before = service.propose(complete_selections(service))
    service.accept(before, expected_revision=None)
    candidate = ActivationSnapshot({**before.settings, "ACTUATION.enabled": True}, before.bindings)

    with pytest.raises(PluginSchemaError, match="effector"):
        service.accept(candidate, expected_revision=before.revision)

    assert service.repository.get_activation() == before


@pytest.mark.parametrize("slot,kind,alias", [
    ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("stimulation.trigger", "trigger", "neuroscan_scan45_trigger"),
    ("actuation.effector", "effector", "logger"),
])
def test_unavailable_outputs_remain_portable_until_their_kind_is_active(service, monkeypatch, slot, kind, alias):
    original_get = service.catalog.get

    def get(requested_kind, selection):
        definition = original_get(requested_kind, selection)
        return replace(definition, available=False) if requested_kind == kind else definition

    monkeypatch.setattr(service.catalog, "get", get)
    definition = service.catalog.legacy(kind, alias)
    profile = service.create(kind, definition.selection, "Portable output", {})
    saved = service.update(profile.profile_id, name="Portable edited", parameters=profile.parameters,
                           expected_revision=profile.revision)
    assert service.repository.get_profile(saved.profile_id) == saved
    service.settings_source = lambda: {"ACTUATION.enabled": False}
    candidate = service.propose(complete_selections(service, [{"slot": slot, "profile_id": saved.profile_id}]))

    accepted = service.accept(candidate, expected_revision=None)
    assert accepted == candidate
    invalid = ActivationSnapshot(candidate.settings, tuple(
        replace(binding, parameters={**binding.parameters, "unknown": True}) if binding.kind == kind else binding
        for binding in candidate.bindings
    ))
    with pytest.raises(PluginSchemaError, match="unknown"):
        service.accept(invalid, expected_revision=candidate.revision)
    with pytest.raises(PluginSchemaError, match="unavailable on this platform"):
        service.validate_activation(candidate, active_kinds={kind})
    if kind == "effector":
        with pytest.raises(PluginSchemaError, match="unavailable on this platform"):
            service.accept(ActivationSnapshot({"ACTUATION.enabled": True}, candidate.bindings),
                           expected_revision=candidate.revision)
        service.validate_activation(ActivationSnapshot(
            {"ACTUATION.enabled": True, "ACTUATION.auto_spawn": False}, candidate.bindings))
    assert service.repository.get_activation() == candidate


def test_unavailable_source_can_be_saved_but_not_accepted(service, monkeypatch):
    before = service.propose(complete_selections(service))
    service.accept(before, expected_revision=None)
    original_get = service.catalog.get

    def get(kind, selection):
        definition = original_get(kind, selection)
        return replace(definition, available=False) if kind == "acquisition" else definition

    monkeypatch.setattr(service.catalog, "get", get)
    profile = create_source(service, "Portable source", sample_rate=500)
    saved = service.update(profile.profile_id, name="Portable edited", parameters={"sample_rate": 250},
                           expected_revision=profile.revision)
    assert service.repository.get_profile(saved.profile_id) == saved
    candidate = service.propose(complete_selections(service, [
        {"slot": "acquisition.source", "profile_id": saved.profile_id},
    ]))

    with pytest.raises(PluginSchemaError, match="unavailable on this platform"):
        service.accept(candidate, expected_revision=before.revision)
    with pytest.raises(PluginSchemaError, match="unavailable on this platform"):
        service.validate_activation(candidate, active_kinds={"paradigm"})
    assert service.repository.get_activation() == before
    service.delete(saved.profile_id, expected_revision=saved.revision)
    assert all(item.profile_id != saved.profile_id for item in service.repository.list_profiles())
