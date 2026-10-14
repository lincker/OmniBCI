"""Only compatible exact selections can be prepared for a trial boundary."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.domain.channel_mapping import ChannelDescriptor, ProcessingInput, SignalInfo, compile_processing_plan
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.neuroengine.processing import pipeline_configuration as configuration


def selected_snapshot(revision="old"):
    catalog = PluginCatalog()
    bindings = [ProfileBinding(slot, kind, None, catalog.resolve_legacy(kind, alias).selection, parameters,
                               processing_input=ProcessingInput() if kind == "decoder" else None).to_dict()
                for slot, kind, alias, parameters in (
                    ("acquisition.source", "acquisition", "file_reader", {"sample_rate": 250}),
                    ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA",
                     {"window_duration": 1.0, "offset_time": 0.0}),
                    ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
                )]
    return {"CONFIG.revision": revision, "CONFIG.generation": "run",
            "ACQUISITION.signal_source": "file_reader",
            "ACQUISITION.sample_rate": 250, "ALGORITHM.baseline_duration": 0.2,
            "ACQUISITION.channel_names": ["O1", "O2"], "PLUGINS.bindings": bindings}


def _build(before, after):
    channels = tuple(ChannelDescriptor(index, name, signal_type="eeg", unit="V")
                     for index, name in enumerate(before["ACQUISITION.channel_names"]))
    source = SignalInfo(before["ACQUISITION.signal_source"], before["ACQUISITION.sample_rate"],
                            channels, len(channels), connection_id="test-connection")
    plan = compile_processing_plan(source, ProcessingInput.from_dict(
        before["PLUGINS.bindings"][1]["processing_input"]), config_revision=before["CONFIG.revision"])
    return configuration.build_pipeline_update(before, after, sample_rate=250, baseline_duration=0.2,
                                               signal_plan=plan)


def test_bundle_uses_new_parameters_and_ordered_distinct_filter_instances():
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.revision"] = "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    selection = PluginCatalog().resolve_legacy("filter", "BASELINE_CORRECTION").selection
    after["PLUGINS.bindings"].extend(ProfileBinding(
        f"processing.filter.{uuid4()}", "filter", None, selection, {},
    ).to_dict() for _ in range(2))
    bundle = _build(before, after)
    assert bundle.decoder.SAMPLE_COUNT == 375
    assert len(bundle.filters) == 2
    assert bundle.filters[0] is not bundle.filters[1]
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 9
    assert bundle.settings_snapshot["PLUGINS.bindings"][1]["parameters"]["window_duration"] == 1.5
    assert before["PLUGINS.bindings"][1]["parameters"]["window_duration"] == 1


@pytest.mark.parametrize("provenance", ["device-reported", "file-reported"])
def test_trial_update_uses_prepared_source_rate_instead_of_configured_default(provenance):
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.revision"] = "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    source = SignalInfo("file_reader", 500, (
        ChannelDescriptor(0, "O1", signal_type="eeg", unit="V"),
        ChannelDescriptor(1, "O2", signal_type="eeg", unit="V"),
    ), 2, connection_id="test-connection", provenance=provenance)
    plan = compile_processing_plan(source, ProcessingInput(), config_revision="old")

    bundle = configuration.build_pipeline_update(
        before, after, sample_rate=500, baseline_duration=0.2, signal_plan=plan,
    )

    assert bundle.decoder.SAMPLE_COUNT == 750
    assert bundle.settings_snapshot["ACQUISITION.sample_rate"] == 250


def test_without_a_prepared_plan_running_rate_must_match_configured_rate():
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.revision"] = "new"

    with pytest.raises(ValueError, match="running sample rate"):
        configuration.build_pipeline_update(before, after, sample_rate=500, baseline_duration=0.2)


@pytest.mark.parametrize("key,value", [
    ("ACQUISITION.sample_rate", 500), ("ACQUISITION.channel_names", ["Cz"]),
    ("ALGORITHM.baseline_duration", 0.3), ("CONFIG.generation", "another-run"),
])
def test_host_context_changes_require_restart(key, value):
    before = selected_snapshot()
    after = deepcopy(before)
    after[key] = value
    with pytest.raises(ValueError, match="restart"):
        _build(before, after)


@pytest.mark.parametrize("index", [0, 2])
def test_source_and_detector_binding_changes_require_restart(index):
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][index]["parameters"]["changed"] = True
    with pytest.raises(ValueError, match="topology"):
        _build(before, after)


@pytest.mark.parametrize("policy", [None, "module", "restart"])
def test_same_selection_parameter_changes_require_explicit_trial_policy(monkeypatch, policy):
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    original_get = configuration.CatalogConfigurationAdapter.get

    def get(catalog, kind, selection):
        definition = original_get(catalog, kind, selection)
        schema = deepcopy(definition.schema)
        declaration = schema["properties"]["window_duration"]
        declaration.pop("x-apply", None)
        if policy is not None:
            declaration["x-apply"] = policy
        return replace(definition, schema=schema)

    monkeypatch.setattr(configuration.CatalogConfigurationAdapter, "get", get)
    with pytest.raises(ValueError, match="restart"):
        _build(before, after)


def test_decoder_replacement_and_filter_removal_build_complete_bundle():
    before = selected_snapshot()
    filter_selection = PluginCatalog().resolve_legacy("filter", "BASELINE_CORRECTION").selection
    before["PLUGINS.bindings"].append(ProfileBinding(
        f"processing.filter.{uuid4()}", "filter", None, filter_selection, {},
    ).to_dict())
    after = deepcopy(before)
    after["PLUGINS.bindings"].pop()
    after["CONFIG.revision"] = "new"
    decoder = PluginCatalog().resolve_legacy("decoder", "THUBCI_SSVEP_classic40FBCCA")
    after["PLUGINS.bindings"][1] = ProfileBinding(
        "processing.decoder", "decoder", None, decoder.selection,
        {"window_duration": 1.0, "offset_time": 0.0}, processing_input=ProcessingInput(),
    ).to_dict()
    bundle = _build(before, after)
    assert bundle.decoder.plugin_type == "THUBCI_SSVEP_classic40FBCCA"
    assert bundle.filters == ()


def test_invalid_candidate_does_not_change_input():
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = -1
    with pytest.raises(ValueError):
        _build(before, after)
    assert before["PLUGINS.bindings"][1]["parameters"]["window_duration"] == 1


@pytest.mark.parametrize("changed_selection", [False, True])
def test_decoder_input_change_cannot_reuse_existing_prepared_plan(changed_selection):
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][1]["processing_input"] = {"schema_version": 2, "outputs": [{"source_channel_id": "channel-1", "name": "O2"}]}
    if changed_selection:
        selection = PluginCatalog().resolve_legacy("decoder", "THUBCI_SSVEP_classic40FBCCA").selection
        after["PLUGINS.bindings"][1]["selection"] = selection.to_dict()
    with pytest.raises(ValueError, match="Processing input changes"):
        _build(before, after)


def test_output_context_cannot_change_decoder_input():
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][1]["processing_input"] = {"schema_version": 2, "outputs": [{"source_channel_id": "channel-1", "name": "O2"}]}
    with pytest.raises(ValueError, match="Processing or source changes"):
        configuration.validate_output_context(before, after)
