"""Plans classify actual changes and pin inputs through concurrent saved edits."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import ActivationSnapshot, ProfileRevisionConflict
from omnibci.application.processing_source import processing_input
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.domain.montage import ChannelDescriptor, ProcessingInput, SourceMetadata, compile_processing_plan


def prepared_signal_plan(snapshot):
    count = snapshot["ACQUISITION.channel_number"] - 1
    channels = tuple(ChannelDescriptor(index, f"EEG{index + 1}") for index in range(count))
    source = SourceMetadata(snapshot["ACQUISITION.signal_source"], snapshot["ACQUISITION.sample_rate"], channels, count)
    return compile_processing_plan(source, processing_input(snapshot) or ProcessingInput())


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(), settings_source=lambda: {})
    selections = []
    for slot, kind, alias in (
        ("acquisition.source", "acquisition", "file_reader"),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ):
        definition = profiles.catalog.legacy(kind, alias)
        profile = profiles.create(kind, definition.selection, alias, {})
        selections.append({"slot": slot, "profile_id": profile.profile_id})
    snapshot = profiles.propose(selections)
    profiles.accept(snapshot, expected_revision=None)
    engine = SimpleNamespace(is_engine_ready=True, applied_revision=snapshot.revision,
                             generation="generation-1", applied_snapshot=profiles.materialize(snapshot))
    return RuntimeConfigurationService(profiles, engine), selections, snapshot


def edit(runtime, slot, values):
    service, selections, _ = runtime
    profile = service.profiles.repository.get_profile(next(s["profile_id"] for s in selections if s["slot"] == slot))
    service.profiles.update(profile.profile_id, name=profile.name,
                            parameters={**profile.parameters, **values}, expected_revision=profile.revision)


def test_plan_is_pinned_and_does_not_apply_saved_values(runtime):
    service, selections, before = runtime
    edit(runtime, "processing.decoder", {"window_duration": 2.5})
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "trial_boundary"
    assert plan.session_effect == "preserve"
    assert not plan.errors
    edit(runtime, "processing.decoder", {"window_duration": 1.5})
    pinned = service.resolve_plan(plan.plan_id).snapshot
    assert pinned.bindings[1].parameters["window_duration"] == 2.5
    assert service.profiles.repository.get_activation() == before
    assert service.status()["applied_revision"] == before.revision


def test_source_change_requires_continuation(runtime):
    service, selections, before = runtime
    edit(runtime, "acquisition.source", {"sample_rate": 500})
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "rebuild"
    assert plan.session_effect == "continuation"


def test_manual_source_montage_change_requires_rebuild_and_is_pinned(runtime):
    service, selections, before = runtime
    profile = service.profiles.repository.get_profile(selections[0]["profile_id"])
    manual = SourceMetadata("file_reader", profile.parameters["sample_rate"], tuple(
        ChannelDescriptor(index, f"C{index + 1}", unit="uV")
        for index in range(profile.parameters["channel_number"] - 1)), profile.parameters["channel_number"] - 1)
    service.profiles.update(profile.profile_id, name=profile.name, parameters=profile.parameters,
                            manual_montage=manual, expected_revision=profile.revision)
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "rebuild"
    assert plan.changed_slots == ("acquisition.source",)
    assert service.resolve_plan(plan.plan_id).snapshot.bindings[0].manual_montage == manual
    assert before.bindings[0].manual_montage is None


def test_decoder_input_edit_requires_rebuild_and_review_uses_profile_input(runtime):
    service, selections, before = runtime
    profile = service.profiles.repository.get_profile(selections[1]["profile_id"])
    input_a = ProcessingInput.from_dict({
        "outputs": ["O2", "O1"], "reference": {"mode": "single", "channels": ["M1"]},
        "excluded_channels": ["EOG"],
    })
    service.profiles.update(profile.profile_id, name=profile.name, parameters=profile.parameters,
                            processing_input=input_a, expected_revision=profile.revision)

    plan = service.plan(selections, expected_revision=before.revision)

    assert plan.strategy == "rebuild"
    assert plan.session_effect == "continuation"
    assert plan.changed_slots == ("processing.decoder",)
    assert plan.changed_settings == ()
    assert plan.signal_summary == "Outputs: O2, O1; Reference: single (M1); Excluded: EOG"
    candidate = service.resolve_plan(plan.plan_id).snapshot
    assert candidate.bindings[1].processing_input == input_a
    assert "SIGNAL.setup" not in candidate.settings
    materialized = service.profiles.materialize(candidate)
    assert processing_input(materialized) == input_a
    assert "SIGNAL.setup" not in materialized
    assert before.bindings[1].processing_input == ProcessingInput()


def test_decoder_profiles_restore_independent_inputs_and_parameters_keep_trial_boundary(runtime):
    service, selections, before = runtime
    profiles = service.profiles
    selection = profiles.catalog.legacy("decoder", "THUBCI_SSVEP_classic40CCA").selection
    profile_a = profiles.create("decoder", selection, "Input A", {}, processing_input=ProcessingInput(outputs=("O1",)))
    profile_b = profiles.create("decoder", selection, "Input B", {}, processing_input=ProcessingInput(outputs=("O2", "O1")))
    expected = before.revision
    for profile in (profile_a, profile_b, profile_a):
        selections[1] = {"slot": "processing.decoder", "profile_id": profile.profile_id}
        plan = service.plan(selections, expected_revision=expected)
        assert plan.strategy == "rebuild"
        target = service.resolve_plan(plan.plan_id).snapshot
        materialized = profiles.materialize(target)
        assert processing_input(materialized) == profile.processing_input
        assert "SIGNAL.setup" not in materialized
        profiles.accept(target, expected_revision=expected)
        service.engine.applied_snapshot = materialized
        service.engine.applied_revision = target.revision
        expected = target.revision

    edit((service, selections, before), "processing.decoder", {"window_duration": 2.5})
    plan = service.plan(selections, expected_revision=expected)
    assert plan.strategy == "trial_boundary"
    assert plan.signal_summary == "Outputs: O1; Reference: original"


def test_effector_selection_uses_module_boundary(runtime):
    service, selections, before = runtime
    definition = service.profiles.catalog.legacy("effector", "logger")
    profile = service.profiles.create("effector", definition.selection, "Logger", {})
    selections.append({"slot": "actuation.effector", "profile_id": profile.profile_id})
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "module"


def test_idle_plan_does_not_claim_applied_revision(runtime):
    service, selections, before = runtime
    service.engine.is_engine_ready = False
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "select_on_start"
    assert plan.applied_revision is None
    assert service.status()["applied_revision"] is None


def test_no_semantic_change_does_not_restart(runtime):
    service, selections, before = runtime
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "no_change"
    assert plan.target_revision == before.revision


@pytest.mark.parametrize("previous_logging", [
    {},
    {"LOGGING.level": "INFO", "LOGGING.file_path": "original.log"},
])
def test_historical_logging_metadata_does_not_change_running_revision_or_rebuild(runtime, previous_logging):
    service, selections, before = runtime
    accepted = ActivationSnapshot({**before.settings, **previous_logging}, before.bindings)
    service.profiles.accept(accepted, expected_revision=before.revision)
    service.engine.applied_revision = accepted.revision
    service.engine.applied_snapshot = service.profiles.materialize(accepted)
    host = {key: value for key, value in accepted.settings.items() if not key.startswith("LOGGING.")}
    service.profiles.settings_source = lambda: host

    direct = service.profiles.propose(selections, include_host_settings=True)
    plan = service.plan(selections, expected_revision=accepted.revision, include_host_settings=True)

    assert direct.revision == accepted.revision
    assert direct.settings == accepted.settings
    assert plan.strategy == "no_change"
    assert plan.changed_settings == ()
    assert plan.target_revision == accepted.revision
    assert not plan.errors
    assert service.resolve_plan(plan.plan_id).snapshot == accepted
    assert service.profiles.repository.get_activation() == accepted


def test_runtime_host_changes_still_require_rebuild(runtime):
    service, selections, before = runtime
    service.profiles.settings_source = lambda: {
        **before.settings, "NEUROENGINE.queue_size": 256,
    }

    plan = service.plan(selections, expected_revision=before.revision, include_host_settings=True)

    assert plan.strategy == "rebuild"
    assert plan.changed_settings == ("NEUROENGINE.queue_size",)


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("key,value", [
    ("LOGGING.level", "DEBUG"),
    ("LOGGING.file_path", "changed.log"),
    ("NEUROENGINE.command_timeout_s", 42.0),
    ("PERSISTENCE.session_repository", "another-repository"),
    ("PLUGINS.user_dir", "/another/plugin-directory"),
])
def test_changed_backend_startup_settings_cannot_produce_an_apply_plan(runtime, running, key, value):
    service, selections, before = runtime
    service.engine.is_engine_ready = running
    service.profiles.settings_source = lambda: {**before.settings, key: value}

    with pytest.raises(PluginSchemaError) as error:
        service.plan(selections, expected_revision=before.revision,
                     refresh_slots=[], include_host_settings=True)

    assert key in str(error.value)
    assert not service._plans
    assert service.profiles.repository.get_activation() == before


@pytest.mark.parametrize("running", [False, True])
def test_pending_backend_restart_rejects_mixed_runtime_changes_without_partial_plan(runtime, running):
    service, selections, before = runtime
    service.engine.is_engine_ready = running
    service.profiles.settings_source = lambda: {
        **before.settings, "LOGGING.level": "DEBUG", "NEUROENGINE.queue_size": 256,
    }

    with pytest.raises(PluginSchemaError):
        service.plan(selections, expected_revision=before.revision,
                     refresh_slots=[], include_host_settings=True)

    assert not service._plans
    assert service.profiles.repository.get_activation() == before


@pytest.mark.parametrize("running", [False, True])
def test_unknown_host_changes_cannot_be_reported_as_applied(runtime, running):
    service, selections, before = runtime
    service.engine.is_engine_ready = running
    service.profiles.settings_source = lambda: {
        **before.settings, "NEUROENGINE.unknown_option": True, "NEUROENGINE.queue_size": 256,
    }

    with pytest.raises(PluginSchemaError) as error:
        service.plan(selections, expected_revision=before.revision,
                     refresh_slots=[], include_host_settings=True)

    assert "NEUROENGINE.unknown_option" in str(error.value)
    assert not service._plans
    assert service.profiles.repository.get_activation() == before


def test_saved_host_changes_are_excluded_from_default_plugin_plan(runtime):
    service, selections, before = runtime
    service.profiles.settings_source = lambda: {
        **before.settings, "LOGGING.level": "DEBUG", "NEUROENGINE.queue_size": 256,
    }
    edit(runtime, "processing.decoder", {"window_duration": 2.5})

    plan = service.plan(selections, expected_revision=before.revision,
                        refresh_slots=["processing.decoder"])

    assert plan.strategy == "trial_boundary"
    assert plan.changed_settings == ()
    assert not plan.errors
    assert service.resolve_plan(plan.plan_id).snapshot.settings == before.settings


def test_restarted_backend_uses_new_startup_baseline_without_false_pending_restart(runtime):
    service, selections, before = runtime
    host = {**before.settings, "LOGGING.level": "DEBUG", "NEUROENGINE.queue_size": 256}
    service.profiles.settings_source = lambda: host
    with pytest.raises(PluginSchemaError):
        service.plan(selections, expected_revision=before.revision, include_host_settings=True)

    profiles = PluginProfileService(service.profiles.repository, service.profiles.catalog,
                                    settings_source=lambda: host)
    idle_engine = SimpleNamespace(is_engine_ready=False, applied_revision=None,
                                  generation=None, applied_snapshot=None)
    restarted = RuntimeConfigurationService(profiles, idle_engine)
    plan = restarted.plan(selections, expected_revision=before.revision, include_host_settings=True)

    assert plan.strategy == "select_on_start"
    assert plan.changed_settings == ("NEUROENGINE.queue_size",)
    assert not plan.errors
    assert restarted.resolve_plan(plan.plan_id).snapshot.settings == {"NEUROENGINE.queue_size": 256}
    assert profiles.repository.get_activation() == before


@pytest.mark.parametrize("historical", [False, True])
def test_diagnostic_and_retired_edits_do_not_hide_actual_runtime_changes(runtime, historical):
    service, selections, before = runtime
    inert = {
        "CONFIG.revision": "old-generated-revision", "CONFIG.generation": "old-generation",
        "PLUGINS.bindings": [], "PROFILES.active": "old-profile",
        "LOGGING.dropped_records": 1, "project_name": "old-name", "version": "old-version",
        "CONSTANT.ASYNC_SLEEP_TIME": 1.0, "NEUROENGINE.COMMAND_SERVER_IP": "old-host",
        "NEUROENGINE.COMMAND_SERVER_PORT": 9000, "ACQUISITION.file_reader_play_speed": 2.0,
    }
    accepted = ActivationSnapshot(inert if historical else before.settings, before.bindings)
    service.profiles.accept(accepted, expected_revision=before.revision)
    service.engine.applied_revision = accepted.revision
    service.engine.applied_snapshot = service.profiles.materialize(accepted)
    host = {key: "changed-inert-value" for key in inert}
    service.profiles.settings_source = lambda: host

    plan = service.plan(selections, expected_revision=accepted.revision,
                        refresh_slots=[], include_host_settings=True)

    assert plan.strategy == "no_change"
    assert plan.changed_settings == ()
    candidate = service.resolve_plan(plan.plan_id).snapshot
    assert candidate == accepted
    materialized = service.profiles.materialize(candidate)
    assert not (inert.keys() - {"CONFIG.revision", "PLUGINS.bindings"}) & materialized.keys()
    assert materialized["CONFIG.revision"] == accepted.revision
    assert materialized["PLUGINS.bindings"] == [binding.to_dict() for binding in accepted.bindings]

    host["NEUROENGINE.queue_size"] = 256
    changed = service.plan(selections, expected_revision=accepted.revision,
                           refresh_slots=[], include_host_settings=True)
    assert changed.strategy == "rebuild"
    assert changed.changed_settings == ("NEUROENGINE.queue_size",)
    assert not changed.errors


@pytest.mark.parametrize("enabled", [False, True])
def test_actuation_topology_changes_remain_runtime_rebuilds(runtime, enabled):
    service, selections, before = runtime
    profile = service.profiles.create(
        "effector", service.profiles.catalog.legacy("effector", "logger").selection, "Logger", {},
    )
    selections = selections + [{"slot": "actuation.effector", "profile_id": profile.profile_id}]
    selected = service.profiles.propose(selections)
    accepted = ActivationSnapshot({"ACTUATION.enabled": enabled, "ACTUATION.auto_spawn": True}, selected.bindings)
    service.profiles.accept(accepted, expected_revision=before.revision)
    service.engine.applied_revision = accepted.revision
    service.engine.applied_snapshot = service.profiles.materialize(accepted)
    service.profiles.settings_source = lambda: {**accepted.settings, "ACTUATION.enabled": not enabled}

    plan = service.plan(selections, expected_revision=accepted.revision,
                        refresh_slots=[], include_host_settings=True)

    assert plan.strategy == "rebuild"
    assert plan.changed_settings == ("ACTUATION.enabled",)
    assert not plan.errors
    candidate = service.resolve_plan(plan.plan_id).snapshot
    assert candidate.settings["ACTUATION.enabled"] is (not enabled)
    assert service.profiles.materialize(candidate)["ACTUATION.enabled"] is (not enabled)


@pytest.mark.parametrize("module", ["STIMULATION", "ACTUATION"])
@pytest.mark.parametrize("external_before", [False, True])
def test_external_mode_transitions_check_both_current_and_target(runtime, module, external_before):
    service, selections, before = runtime
    if module == "ACTUATION":
        profile = service.profiles.create(
            "effector", service.profiles.catalog.legacy("effector", "logger").selection, "Logger", {},
        )
        selections = selections + [{"slot": "actuation.effector", "profile_id": profile.profile_id}]
    selected = service.profiles.propose(selections)
    host = {"STIMULATION.auto_spawn": True, "ACTUATION.auto_spawn": True,
            "ACTUATION.enabled": module == "ACTUATION", f"{module}.auto_spawn": not external_before}
    accepted = ActivationSnapshot(host, selected.bindings)
    service.profiles.accept(accepted, expected_revision=before.revision)
    service.engine.applied_revision = accepted.revision
    service.engine.applied_snapshot = service.profiles.materialize(accepted)
    service.profiles.settings_source = lambda: {**host, f"{module}.auto_spawn": external_before}

    plan = service.plan(selections, expected_revision=accepted.revision,
                        refresh_slots=[], include_host_settings=True)

    assert plan.strategy == "rebuild"
    assert plan.changed_settings == (f"{module}.auto_spawn",)
    assert any(f"{module.title()} is externally managed" in error for error in plan.errors)
    with pytest.raises(PluginSchemaError):
        service.resolve_plan(plan.plan_id)
    assert service.profiles.repository.get_activation() == accepted


LEGACY_PLUGIN_VALUES = {
    "ACQUISITION.sample_rate": 125,
    "ACQUISITION.channel_number": 99,
    "ACQUISITION.parallel_port_address": "0x378",
    "ALGORITHM.decoder": "obsolete_decoder",
    "ALGORITHM.window_duration": 7.0,
    "ALGORITHM.offset_time": 0.9,
    "ssvep.sample_rate": 125,
    "ssvep.duration": 8.0,
    "ssvep.offset_time": 0.8,
}


def accept_historical_settings(runtime, legacy_values):
    service, _, before = runtime
    accepted = ActivationSnapshot({"ALGORITHM.baseline_duration": 0.2, **legacy_values}, before.bindings)
    service.profiles.accept(accepted, expected_revision=before.revision)
    service.engine.applied_revision = accepted.revision
    service.engine.applied_snapshot = service.profiles.materialize(accepted)
    service.profiles.settings_source = lambda: {
        **accepted.settings,
        "ACQUISITION.sample_rate": 2000,
        "ACQUISITION.channel_number": 64,
        "ACQUISITION.parallel_port_address": "0x3100",
        "ALGORITHM.decoder": "another_obsolete_decoder",
        "ALGORITHM.window_duration": 12.0,
        "ALGORITHM.offset_time": 1.0,
        "ssvep.sample_rate": 2000,
        "ssvep.duration": 12.0,
        "ssvep.offset_time": 1.0,
    }
    return accepted


@pytest.mark.parametrize("legacy_values", [{}, LEGACY_PLUGIN_VALUES])
def test_raw_plugin_edits_do_not_change_candidate_revision_or_rebuild(runtime, legacy_values):
    service, selections, _ = runtime
    accepted = accept_historical_settings(runtime, legacy_values)

    candidate = service.profiles.propose(
        selections, include_host_settings=True, refresh_slots=[],
    )
    plan = service.plan(selections, expected_revision=accepted.revision,
                        refresh_slots=[], include_host_settings=True)

    assert candidate == accepted
    assert plan.strategy == "no_change"
    assert plan.changed_settings == ()
    assert plan.changed_slots == ()
    assert plan.target_revision == accepted.revision
    assert not plan.errors
    assert service.resolve_plan(plan.plan_id).snapshot == accepted
    assert service.profiles.repository.get_activation() == accepted
    assert service.profiles.materialize(candidate) == service.engine.applied_snapshot


@pytest.mark.parametrize("decoder_alias", ["THUBCI_SSVEP_classic40CCA", "THUBCI_SSVEP_classic40FBCCA"])
def test_source_context_and_decoder_use_pinned_values_until_explicit_refresh(runtime, decoder_alias):
    from omnibci.infrastructure.neuroengine.builder import NeuroEngineConfig
    from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin, plugin_context
    from omnibci.sdk import Decoder
    from omnibci.infrastructure.neuroengine.signal_processing.signal_configuration import signal_context

    def load_prepared(snapshot):
        return load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                                      context=signal_context(snapshot, prepared_signal_plan(snapshot)))

    service, selections, before = runtime
    edit(runtime, "acquisition.source", {"sample_rate": 500, "channel_number": 3})
    decoder_profile = service.profiles.create(
        "decoder", service.profiles.catalog.legacy("decoder", decoder_alias).selection,
        "Selected decoder", {"window_duration": 1.5, "offset_time": 0.1},
    )
    selections = [
        {"slot": item["slot"], "profile_id": decoder_profile.profile_id}
        if item["slot"] == "processing.decoder" else item
        for item in selections
    ]
    service.profiles.settings_source = lambda: {
        **LEGACY_PLUGIN_VALUES,
        "ALGORITHM.baseline_duration": 0.2,
        "ACQUISITION.channel_names": ["O1", "O2"],
    }
    accepted = service.profiles.propose(selections, include_host_settings=True)
    service.profiles.accept(accepted, expected_revision=before.revision)
    materialized = service.profiles.resolve_settings()
    context = plugin_context(materialized)
    decoder = load_prepared(materialized)
    source = next(binding for binding in accepted.bindings if binding.slot == "acquisition.source")

    assert not set(LEGACY_PLUGIN_VALUES) & accepted.settings.keys()
    assert materialized["ACQUISITION.sample_rate"] == source.parameters["sample_rate"] == 500
    assert materialized["ACQUISITION.channel_number"] == source.parameters["channel_number"] == 3
    assert NeuroEngineConfig.from_settings(materialized).sample_rate == context.sample_rate == 500
    assert context.channel_names == ("O1", "O2")
    assert context.config_revision == accepted.revision
    assert (decoder.SAMPLE_RATE, decoder.SAMPLE_COUNT, decoder.OFFSET_LENGTH) == (500, 750, 50)
    assert "ALGORITHM.window_duration" not in materialized
    assert "ssvep.duration" not in materialized
    assert "ACQUISITION.parallel_port_address" not in materialized

    edit((service, selections, accepted), "acquisition.source", {"sample_rate": 250, "channel_number": 4})
    edit((service, selections, accepted), "processing.decoder", {"window_duration": 2.5})

    assert service.profiles.propose(selections, refresh_slots=[]) == accepted
    assert service.profiles.resolve_settings() == materialized
    unchanged = load_prepared(service.profiles.resolve_settings())
    assert (unchanged.SAMPLE_RATE, unchanged.SAMPLE_COUNT) == (500, 750)
    refreshed = service.profiles.propose(
        selections, refresh_slots=["acquisition.source", "processing.decoder"],
    )
    refreshed_settings = service.profiles.materialize(refreshed)
    refreshed_decoder = load_prepared(refreshed_settings)
    assert refreshed_settings["ACQUISITION.channel_number"] == 4
    assert (refreshed_decoder.SAMPLE_RATE, refreshed_decoder.SAMPLE_COUNT) == (250, 625)
    assert service.profiles.repository.get_activation() == accepted


def test_stripped_legacy_fields_preserve_decoder_trial_application(runtime):
    from omnibci.infrastructure.neuroengine.signal_processing.pipeline_configuration import build_pipeline_update

    service, selections, _ = runtime
    accepted = accept_historical_settings(runtime, LEGACY_PLUGIN_VALUES)
    edit(runtime, "processing.decoder", {"window_duration": 2.5})
    plan = service.plan(selections, expected_revision=accepted.revision,
                        refresh_slots=["processing.decoder"], include_host_settings=True)

    assert plan.strategy == "trial_boundary"
    assert plan.changed_settings == ()
    assert not plan.errors
    candidate = service.profiles.materialize(service.resolve_plan(plan.plan_id).snapshot)
    bundle = build_pipeline_update(service.engine.applied_snapshot, candidate,
                                   sample_rate=1000, baseline_duration=0.2,
                                   signal_plan=prepared_signal_plan(service.engine.applied_snapshot))
    assert bundle.decoder.SAMPLE_COUNT == 2500
    assert "ALGORITHM.window_duration" not in bundle.settings_snapshot
    assert "ssvep.duration" not in bundle.settings_snapshot


def test_stripped_legacy_fields_preserve_output_module_application(runtime):
    from omnibci.infrastructure.neuroengine.signal_processing.pipeline_configuration import validate_output_context

    service, selections, _ = runtime
    accepted = accept_historical_settings(runtime, LEGACY_PLUGIN_VALUES)
    effector = service.profiles.create(
        "effector", service.profiles.catalog.legacy("effector", "logger").selection, "Logger", {},
    )
    selections = selections + [{"slot": "actuation.effector", "profile_id": effector.profile_id}]
    plan = service.plan(selections, expected_revision=accepted.revision,
                        refresh_slots=["actuation.effector"], include_host_settings=True)

    assert plan.strategy == "module"
    assert plan.changed_settings == ()
    assert not plan.errors
    candidate = service.profiles.materialize(service.resolve_plan(plan.plan_id).snapshot)
    updated = validate_output_context(service.engine.applied_snapshot, candidate)
    assert updated["ACTUATION.effector"] == "logger"
    assert "ACQUISITION.parallel_port_address" not in updated
    assert updated["ACQUISITION.sample_rate"] == service.engine.applied_snapshot["ACQUISITION.sample_rate"]


@pytest.mark.parametrize("changed", ["generation", "applied_revision", "is_engine_ready"])
def test_stale_runtime_plan_cannot_execute(runtime, changed):
    service, selections, before = runtime
    plan = service.plan(selections, expected_revision=before.revision)
    setattr(service.engine, changed, False if changed == "is_engine_ready" else "changed")
    with pytest.raises(ProfileRevisionConflict, match="changed"):
        service.resolve_plan(plan.plan_id)


def test_plan_compare_and_swap_and_expiry(runtime):
    service, selections, before = runtime
    with pytest.raises(ProfileRevisionConflict):
        service.plan(selections, expected_revision="stale")
    plan = service.plan(selections, expected_revision=before.revision)
    service._plans[plan.plan_id] = replace(service._plans[plan.plan_id], created_at=-1000)
    with pytest.raises(ProfileRevisionConflict, match="expired"):
        service.resolve_plan(plan.plan_id)


def test_missing_required_slot_is_reviewable_error(runtime):
    service, selections, before = runtime
    plan = service.plan(selections[1:], expected_revision=before.revision)
    assert any("acquisition.source" in error for error in plan.errors)


@pytest.mark.parametrize("slot,parameters,strategy", [
    ("acquisition.source", {"sample_rate": 500}, "rebuild"),
    ("processing.decoder", {"window_duration": 2.5}, "trial_boundary"),
])
def test_external_peer_rejected_before_revision_change(runtime, slot, parameters, strategy):
    service, selections, before = runtime
    service.engine.applied_snapshot["STIMULATION.auto_spawn"] = False
    edit(runtime, slot, parameters)
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == strategy
    assert any("externally managed" in error for error in plan.errors)


def test_external_peer_allows_no_change(runtime):
    service, selections, before = runtime
    service.engine.applied_snapshot["STIMULATION.auto_spawn"] = False
    plan = service.plan(selections, expected_revision=before.revision)
    assert plan.strategy == "no_change"
    assert not plan.errors


def test_filter_order_is_a_real_trial_boundary_change(runtime):
    service, selections, before = runtime
    definition = service.profiles.catalog.legacy("filter", "BASELINE_CORRECTION")
    profile = service.profiles.create("filter", definition.selection, "Baseline", {})
    selections += [{"slot": f"processing.filter.{uuid4()}", "profile_id": profile.profile_id} for _ in range(2)]
    selected = service.profiles.propose(selections)
    service.profiles.accept(selected, expected_revision=before.revision)
    service.engine.applied_revision = selected.revision
    reordered = selections[:-2] + list(reversed(selections[-2:]))
    plan = service.plan(reordered, expected_revision=selected.revision)
    assert plan.strategy == "trial_boundary"
    assert plan.target_revision != selected.revision


@pytest.mark.asyncio
async def test_operation_idempotency_owns_one_execution_and_pins_plan(runtime):
    import asyncio
    from unittest.mock import AsyncMock
    service, selections, before = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    async def execute(stored, progress):
        entered.set()
        progress("starting")
        await release.wait()
        return {"outcome": "applied"}
    executor = SimpleNamespace(supported_strategies={"no_change"}, execute=AsyncMock(side_effect=execute))
    service.executor = executor
    plan = service.plan(selections, expected_revision=before.revision)
    operation_id = str(uuid4())
    queued = service.submit(plan.plan_id, operation_id)
    assert queued["status"] == "queued"
    await entered.wait()
    assert service.submit(plan.plan_id, operation_id)["status"] == "applying"
    with pytest.raises(ProfileRevisionConflict, match="already"):
        service.submit("different-plan", operation_id)
    with pytest.raises(ProfileRevisionConflict, match="progress"):
        service.submit(plan.plan_id, str(uuid4()))
    release.set()
    await service.wait_for_idle()
    assert service.operation(operation_id)["status"] == "succeeded"
    assert service.submit(plan.plan_id, operation_id)["status"] == "succeeded"
    assert executor.execute.await_count == 1


@pytest.mark.asyncio
async def test_failed_operation_keeps_recovery_state_and_rejects_new_apply(runtime):
    from unittest.mock import AsyncMock
    from omnibci.application.services.runtime_apply_service import RuntimeApplyFailure
    from omnibci.application.runtime_admission import RuntimeAdmission
    service, selections, before = runtime
    service.engine.admission = RuntimeAdmission()
    service.executor = SimpleNamespace(supported_strategies={"no_change"},
        execute=AsyncMock(side_effect=RuntimeApplyFailure("Device outcome unknown", recovery_required=True)))
    plan = service.plan(selections, expected_revision=before.revision)
    operation_id = str(uuid4())
    service.submit(plan.plan_id, operation_id)
    await service.wait_for_idle()
    operation = service.operation(operation_id)
    assert operation["status"] == "recovery_required"
    assert operation["error"] == "Device outcome unknown"
    assert service.status()["operation"] == operation
    with pytest.raises(ProfileRevisionConflict, match="uncertain"):
        service.submit(plan.plan_id, str(uuid4()))


def test_nonfilter_binding_reorder_never_claims_no_change_for_a_different_revision(runtime):
    service, selections, before = runtime
    plan = service.plan(list(reversed(selections)), expected_revision=before.revision)
    assert plan.target_revision != before.revision
    assert plan.strategy == "rebuild"


@pytest.mark.parametrize("legacy_setup", [False, True])
@pytest.mark.parametrize("target_requires_channels", [False, True])
def test_historical_decoder_effective_input_changes_require_rebuild(runtime, monkeypatch, legacy_setup,
                                                                  target_requires_channels):
    service, selections, before = runtime
    profiles = service.profiles
    get = profiles.catalog.get
    target_definition = profiles.catalog.legacy("decoder", "THUBCI_SSVEP_classic40FBCCA")
    target = profiles.create("decoder", target_definition.selection, "Historical target", {}, processing_input=None)
    previous_binding = replace(before.bindings[1], processing_input=None)
    host = {"SIGNAL.setup": {"source_id": "file_reader"}} if legacy_setup else {}
    current = ActivationSnapshot(host, (before.bindings[0], previous_binding, *before.bindings[2:]))

    def requirements_for_selection(kind, selection):
        definition = get(kind, selection)
        if kind == "decoder":
            required = (target_requires_channels if selection == target.selection else not target_requires_channels)
            return definition if required else replace(definition, channel_requirements=None)
        return definition

    monkeypatch.setattr(profiles.catalog, "get", requirements_for_selection)
    profiles.accept(current, expected_revision=before.revision)
    service.engine.applied_revision = current.revision
    service.engine.applied_snapshot = profiles.materialize(current)
    selections[1] = {"slot": "processing.decoder", "profile_id": target.profile_id}
    plan = service.plan(selections, expected_revision=current.revision)
    assert not plan.errors
    assert plan.strategy == ("trial_boundary" if legacy_setup else "rebuild")
