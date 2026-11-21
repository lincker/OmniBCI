"""Only compatible exact selections can be prepared for a trial boundary."""
from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.configuration.models import ProfileBinding
from omnibci.domain.channel_mapping import ChannelDescriptor, ProcessingInput, SignalInfo, compile_processing_plan
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.plugins.loader import PluginLoadError
from omnibci.infrastructure.signal_processing import pipeline_configuration as configuration


def selected_snapshot(revision="old"):
    catalog = PluginCatalog()
    bindings = [ProfileBinding(slot, kind, None, builtin_selection(kind, alias), parameters,
                               processing_input=ProcessingInput() if kind == "decoder" else None).to_dict()
                for slot, kind, alias, parameters in (
                    ("acquisition.source", "acquisition", "file_reader", {"sample_rate": 250}),
                    ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA",
                     {"window_duration": 1.0, "offset_time": 0.0}),
                    ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
                )]
    return {"CONFIG.module_revisions": {"acquisition": "source-revision", "processing": revision}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "run",
            "ACQUISITION.signal_source": "file_reader",
            "ACQUISITION.sample_rate": 250, "ALGORITHM.baseline_duration": 0.2,
            "PLUGINS.bindings": bindings}


def _build(before, after, boundary="module"):
    channels = tuple(ChannelDescriptor(index, name, signal_type="eeg", unit="V", channel_id=f"channel-{index}")
                     for index, name in enumerate(("O1", "O2")))
    source = SignalInfo(sample_rate=before["ACQUISITION.sample_rate"], channels=channels)
    plan = compile_processing_plan(source, ProcessingInput.from_dict(
        before["PLUGINS.bindings"][1]["processing_input"]), acquisition_revision=before["CONFIG.module_revisions"]["acquisition"])
    return configuration.build_pipeline_update(before, prepared_snapshot(after, ("processing",)), sample_rate=250, baseline_duration=0.2,
                                               signal_plan=plan, boundary=boundary)


def test_bundle_uses_new_parameters_and_ordered_distinct_filter_instances():
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    selection = builtin_selection("filter", "BASELINE_CORRECTION")
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


def test_source_binding_position_does_not_change_processing_topology():
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    source = after["PLUGINS.bindings"].pop(0)
    after["PLUGINS.bindings"].append(source)
    after["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 1.5
    bundle = _build(before, after, boundary="trial")
    assert bundle.decoder.SAMPLE_COUNT == 375


@pytest.mark.parametrize("provenance", ["device-reported", "file-reported"])
def test_trial_update_uses_prepared_source_rate_instead_of_legacy_flat_mirror(provenance):
    before = selected_snapshot()
    before["PLUGINS.bindings"][0]["parameters"].update(sample_rate=500, channel_count=3)
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    source = SignalInfo(sample_rate=500, channels=(
        ChannelDescriptor(0, "O1", signal_type="eeg", unit="V", channel_id="channel-0"),
        ChannelDescriptor(1, "O2", signal_type="eeg", unit="V", channel_id="channel-1"),
    ))
    plan = compile_processing_plan(source, ProcessingInput(), acquisition_revision="source-revision")

    bundle = configuration.build_pipeline_update(
        before, prepared_snapshot(after, ("processing",)), sample_rate=500, baseline_duration=0.2, signal_plan=plan,
    )

    assert bundle.decoder.SAMPLE_COUNT == 750
    assert bundle.settings_snapshot["ACQUISITION.sample_rate"] == 250


def test_unprepared_update_validates_settings_and_defers_plugin_construction(monkeypatch):
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    monkeypatch.setattr(configuration, "load_configured_plugin", lambda *a, **kw: pytest.fail("premature construction"))
    bundle = configuration.build_pipeline_update(before, prepared_snapshot(after, ("processing",)), sample_rate=None, baseline_duration=0.2, boundary="module")
    assert bundle.decoder is None and bundle.filters == ()
    assert bundle.settings_snapshot["CONFIG.module_revisions"]["processing"] == "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = -1
    with pytest.raises(PluginLoadError):
        configuration.build_pipeline_update(before, prepared_snapshot(after, ("processing",)), sample_rate=None, baseline_duration=0.2, boundary="module")


def test_filter_can_be_applied_before_selecting_a_decoder(monkeypatch):
    before = selected_snapshot()
    before["PLUGINS.bindings"] = [before["PLUGINS.bindings"][0]]
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    selection = builtin_selection("filter", "BASELINE_CORRECTION")
    after["PLUGINS.bindings"].append(ProfileBinding(
        f"processing.filter.{uuid4()}", "filter", None, selection, {},
    ).to_dict())
    monkeypatch.setattr(configuration, "load_configured_plugin", lambda *a, **kw: pytest.fail("premature construction"))

    bundle = configuration.build_pipeline_update(before, prepared_snapshot(after, ("processing",)), sample_rate=None, baseline_duration=0.2, boundary="module")

    assert bundle.decoder is None and bundle.filters == ()
    assert bundle.settings_snapshot == prepared_snapshot(after, ("processing",))


@pytest.mark.parametrize("key,value", [
    ("ACQUISITION.sample_rate", 500), ("ACQUISITION.channel_labels", ["Cz", "Trigger"]),
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
        configuration.build_pipeline_update(before, after, sample_rate=None, baseline_duration=0.2)


@pytest.mark.parametrize("policy", [None, "module", "restart"])
def test_same_selection_parameter_changes_require_explicit_trial_policy(monkeypatch, policy):
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    original_get = configuration.resolve_configured_plugin

    def get(snapshot, slot, kind):
        descriptor, parameters = original_get(snapshot, slot, kind)
        schema = deepcopy(descriptor.configuration_schema)
        declaration = schema["properties"]["window_duration"]
        declaration.pop("x-apply", None)
        if policy is not None:
            declaration["x-apply"] = policy
        return replace(descriptor, configuration_schema=schema), parameters

    monkeypatch.setattr(configuration, "resolve_configured_plugin", get)
    with pytest.raises(ValueError, match="restart"):
        _build(before, after, boundary="trial")


def test_decoder_replacement_and_filter_removal_build_complete_bundle():
    before = selected_snapshot()
    filter_selection = builtin_selection("filter", "BASELINE_CORRECTION")
    before["PLUGINS.bindings"].append(ProfileBinding(
        f"processing.filter.{uuid4()}", "filter", None, filter_selection, {},
    ).to_dict())
    after = deepcopy(before)
    after["PLUGINS.bindings"].pop()
    after["CONFIG.module_revisions"]["processing"] = "new"
    decoder = PluginCatalog().resolve_selection("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40FBCCA"))
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
    with pytest.raises(PluginLoadError):
        _build(before, after)
    assert before["PLUGINS.bindings"][1]["parameters"]["window_duration"] == 1


@pytest.mark.parametrize("changed_selection", [False, True])
def test_decoder_input_change_cannot_reuse_existing_prepared_plan(changed_selection):
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][1]["processing_input"] = {"schema_version": 2, "outputs": [{"source_channel_id": "channel-1", "name": "O2"}]}
    if changed_selection:
        selection = builtin_selection("decoder", "THUBCI_SSVEP_classic40FBCCA")
        after["PLUGINS.bindings"][1]["selection"] = selection.to_dict()
    with pytest.raises(ValueError, match="Processing input changes"):
        _build(before, after)




@pytest.mark.parametrize("change,message", [
    ("environment", "environment"), ("schema_version", "binding"),
])
def test_replacement_checks_prepared_contract_before_input_acknowledgement(monkeypatch, change, message):
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    replacement = PluginCatalog().resolve_selection("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40FBCCA"))
    after["PLUGINS.bindings"][1]["selection"] = replacement.selection.to_dict()
    after = prepared_snapshot(after, ("processing",))
    if change == "environment":
        after["PLUGINS.artifacts"]["processing.decoder"]["environment_sha256"] = "0" * 64
    else:
        after["PLUGINS.bindings"][1]["configuration_version"] = 99
    monkeypatch.setattr(configuration, "load_configured_plugin", lambda *a, **kw: pytest.fail("premature construction"))
    with pytest.raises(PluginLoadError, match=message):
        configuration.build_pipeline_update(before, after, sample_rate=None, baseline_duration=0.2, boundary="module")
