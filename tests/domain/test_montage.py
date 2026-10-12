"""Analytical checks for channel selection, rereferencing, and metadata binding."""
from dataclasses import FrozenInstanceError, replace
import json
import pickle

import numpy as np
import pytest

from omnibci.domain.montage import (
    ChannelBinding,
    ChannelDescriptor,
    ProcessingInput,
    ResolvedSignalPlan,
    SignalSetup,
    SourceMetadata,
    compile_processing_plan,
    compile_signal_plan,
    merge_source_montage,
)
from omnibci.domain.decoder_input import DecoderChannelRequirements
from omnibci.domain.signal import SignalChunk


def source(names=("O1", "Oz", "O2", "M1", "M2"), **overrides):
    data = dict(
        source_id="test-source", sample_rate=1000,
        channels=tuple(ChannelDescriptor(i, name, unit="uV") for i, name in enumerate(names)),
        trigger_row_index=len(names), connection_id="connection-one",
        original_reference="Cz", provenance="device-reported",
    )
    data.update(overrides)
    return SourceMetadata(**data)


def setup(**values):
    return SignalSetup.from_dict({"source_id": "test-source", **values})


def chunk(values=(10, 20, 30, 2, 4), triggers=(0, 3, 0)):
    samples = len(triggers)
    return SignalChunk(123456, 17, np.array([[v] * samples for v in values] + [list(triggers)]))


def test_processing_input_resolves_named_channels_against_each_source():
    channel_input = ProcessingInput.from_dict({"outputs": ["O2", "O1"],
        "reference": {"mode": "single", "channels": ["M1"]}})
    assert ProcessingInput.from_dict(channel_input.to_dict()) == channel_input
    first = compile_processing_plan(source(), channel_input)
    second_source = source(("M1", "O2", "O1"), source_id="second-source")
    second = compile_processing_plan(second_source, channel_input)
    assert [channel.name for channel in first.output_channels] == ["O2", "O1"]
    assert [channel.name for channel in second.output_channels] == ["O2", "O1"]
    np.testing.assert_allclose(first.apply(chunk()).raw_data[:2, 0], [28e-6, 8e-6])
    np.testing.assert_allclose(second.apply(chunk(values=(2, 30, 10))).raw_data[:2, 0], [28e-6, 8e-6])
    np.testing.assert_array_equal(second.apply(chunk(values=(2, 30, 10))).raw_data[-1], [0, 3, 0])
    assert first.source == source()
    assert second.source == second_source
    assert first.processing_input == second.processing_input == channel_input
    assert first.setup is second.setup is None
    with pytest.raises(ValueError, match="O2"):
        compile_processing_plan(source(("O1", "M1")), channel_input)


def test_explicit_bindings_order_source_samples_under_algorithm_names():
    observed = source(("CH1", "CH2", "REF"))
    first_channel = replace(observed.channels[0], name_provenance="generated",
        position=(0.01, -0.08, 0.04), coordinate_frame="head", position_provenance="file-reported")
    observed = replace(observed, channels=(first_channel, *observed.channels[1:]))
    value = {"outputs": [{"name": "O2", "source": "CH2"}, {"name": "Oz", "source": "CH1"}]}
    channel_input = ProcessingInput.from_dict(value)
    plan = compile_processing_plan(observed, channel_input)
    assert channel_input.outputs == (ChannelBinding("O2", "CH2"), ChannelBinding("Oz", "CH1"))
    assert channel_input.to_dict()["outputs"] == value["outputs"]
    assert plan.coefficients == ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0))
    assert [channel.name for channel in plan.output_channels] == ["O2", "Oz"]
    assert plan.output_channels[1] == replace(first_channel, index=1, name="Oz",
                                            name_provenance=None, reference="Cz")
    assert observed.channels[0] == first_channel
    DecoderChannelRequirements(signal_type="eeg", min_count=2,
        required_names=("Oz", "O2")).validate_channels(plan.output_channels)
    raw = chunk((10, 30, 2))
    result = plan.apply(raw)
    np.testing.assert_array_equal(result.data[:, 0], [30, 10])
    np.testing.assert_array_equal(result.triggers, raw.triggers)
    assert not plan.is_identity
    assert not np.shares_memory(result.raw_data, raw.raw_data)
    assert ResolvedSignalPlan.from_dict(json.loads(json.dumps(plan.to_dict()))) == plan
    legacy = channel_input.as_signal_setup(observed.source_id)
    assert SignalSetup.from_dict(legacy.to_dict()) == legacy
    assert compile_signal_plan(observed, legacy).output_channels == plan.output_channels


def test_bindings_can_mix_with_same_name_shorthand_and_follow_source_reordering():
    channel_input = ProcessingInput.from_dict({"outputs": ["O1", {"name": "Oz", "source": "CH2"}]})
    first = compile_processing_plan(source(("CH2", "O1")), channel_input)
    second = compile_processing_plan(source(("O1", "CH2")), channel_input)
    np.testing.assert_array_equal(first.apply(chunk((30, 10))).data[:, 0], [10, 30])
    np.testing.assert_array_equal(second.apply(chunk((10, 30))).data[:, 0], [10, 30])
    assert channel_input.to_dict()["outputs"] == ["O1", {"name": "Oz", "source": "CH2"}]
    generated = source(("CH1",))
    generated = replace(generated, channels=(replace(generated.channels[0], name_provenance="generated"),))
    plan = compile_processing_plan(generated, ProcessingInput(outputs=(ChannelBinding("CH1", "CH1"),)))
    assert plan.is_identity
    assert plan.output_channels[0].name_provenance == "generated"


@pytest.mark.parametrize("reference,expected", [
    ({"mode": "single", "channels": ["REF"]}, [28e-6, 8e-6]),
    ({"mode": "average", "channels": ["CH1", "REF"]}, [24e-6, 4e-6]),
])
def test_bound_outputs_rereference_using_original_source_names(reference, expected):
    channel_input = ProcessingInput.from_dict({
        "outputs": [{"name": "O2", "source": "CH2"}, {"name": "Oz", "source": "CH1"}],
        "reference": reference,
    })
    plan = compile_processing_plan(source(("CH1", "CH2", "REF")), channel_input)
    np.testing.assert_allclose(plan.apply(chunk((10, 30, 2))).data[:, 0], expected)
    assert [channel.name for channel in plan.output_channels] == ["O2", "Oz"]
    assert all(channel.unit == "V" for channel in plan.output_channels)


@pytest.mark.parametrize("value,match", [
    ({"outputs": [{"name": "Oz", "source": "CH1"}, {"name": "Oz", "source": "CH2"}]}, "unique"),
    ({"outputs": [{"name": "Oz", "source": "CH1"}, "Oz"]}, "unique"),
    ({"outputs": [{"name": "Oz", "source": "CH1"}, {"name": "O2", "source": "CH1"}]}, "more than once"),
    ({"outputs": [{"name": "Oz", "source": "CH1"}, "CH1"]}, "more than once"),
    ({"outputs": [{"name": "Oz", "source": "CH1"}], "reference": {"mode": "bipolar"}}, "bipolar"),
    ({"outputs": [{"name": "", "source": "CH1"}]}, "nonempty"),
    ({"outputs": [{"name": "Oz", "source": " "}]}, "nonempty"),
    ({"outputs": [{"source": "CH1"}]}, "requires name"),
    ({"outputs": [{"name": "Oz", "source": "CH1", "positive": "CH1", "negative": "CH2"}]}, "Unknown"),
])
def test_invalid_explicit_bindings_are_rejected(value, match):
    with pytest.raises(ValueError, match=match):
        ProcessingInput.from_dict(value)


@pytest.mark.parametrize("value,match", [
    ({"outputs": [{"name": "O1", "source": "Missing"}]}, "Missing signal dependency: Missing"),
    ({"outputs": [{"name": "O1", "source": "CH1"}], "excluded_channels": ["CH1"]}, "Excluded"),
    ({"outputs": [{"name": "O1", "source": "CH1"}],
      "reference": {"mode": "single", "channels": ["O1"]}}, "Missing signal dependency: O1"),
])
def test_explicit_binding_dependencies_are_resolved_against_acquisition(value, match):
    with pytest.raises(ValueError, match=match):
        compile_processing_plan(source(("CH1", "CH2")), ProcessingInput.from_dict(value))


@pytest.mark.parametrize("signal_type", ["trigger", "stim", "ground"])
def test_binding_cannot_disguise_non_signal_rows(signal_type):
    observed = source(("CH1",))
    observed = replace(observed, channels=(replace(observed.channels[0], signal_type=signal_type),))
    with pytest.raises(ValueError, match="cannot be a signal dependency"):
        compile_processing_plan(observed, ProcessingInput(outputs=(ChannelBinding("Oz", "CH1"),)))


def test_alias_preserves_signal_type_for_decoder_requirements():
    observed = source(("CH1",))
    observed = replace(observed, channels=(replace(observed.channels[0], signal_type="eog"),))
    plan = compile_processing_plan(observed, ProcessingInput(outputs=(ChannelBinding("Oz", "CH1"),)))
    with pytest.raises(ValueError, match="eeg"):
        DecoderChannelRequirements(signal_type="eeg", required_names=("Oz",)).validate_channels(plan.output_channels)


@pytest.mark.parametrize("value", [
    {},
    {"outputs": ["O2", "O1"], "excluded_channels": ["Oz"]},
    {"outputs": ["O1"], "reference": {"mode": "single", "channels": ["M1"]}},
    {"outputs": ["O1", "O2"], "reference": {"mode": "average", "channels": ["M1", "M2"]}},
    {"outputs": [{"name": "Left", "source": "O1"}, "O2"],
     "reference": {"mode": "single", "channels": ["M1"]}},
    {"outputs": [{"name": "O1-O2", "positive": "O1", "negative": "O2"}],
     "reference": {"mode": "bipolar"}},
])
def test_processing_input_preserves_channel_transformations(value):
    channel_input = ProcessingInput.from_dict(value)
    plan = compile_processing_plan(source(), channel_input, "revision-one")
    legacy = compile_signal_plan(source(), setup(**value), "revision-one")
    assert plan.coefficients == legacy.coefficients
    assert plan.output_channels == legacy.output_channels
    assert plan.warnings == legacy.warnings
    np.testing.assert_array_equal(plan.apply(chunk()).raw_data, legacy.apply(chunk()).raw_data)
    serialized = json.loads(json.dumps(plan.to_dict()))
    assert serialized["processing_input"] == channel_input.to_dict()
    assert "setup" not in serialized
    assert ResolvedSignalPlan.from_dict(serialized) == plan
    assert pickle.loads(pickle.dumps(plan)) == plan


def test_processing_input_plan_rejects_legacy_setup_and_tampering():
    channel_input = ProcessingInput(outputs=("O2", "O1"))
    plan = compile_processing_plan(source(), channel_input)
    with pytest.raises(ValueError, match="both legacy setup and processing input"):
        replace(plan, setup=setup())
    serialized = plan.to_dict()
    serialized["setup"] = setup().to_dict()
    with pytest.raises(ValueError, match="both legacy setup and processing input"):
        ResolvedSignalPlan.from_dict(serialized)
    serialized = plan.to_dict(include_fingerprint=False)
    serialized["processing_input"]["outputs"].reverse()
    with pytest.raises(ValueError, match="compiled"):
        ResolvedSignalPlan.from_dict(serialized)


def test_processing_input_fingerprint_excludes_runtime_identity_but_tracks_source_binding():
    channel_input = ProcessingInput(outputs=("O2", "O1"))
    first = compile_processing_plan(source(), channel_input, "revision-one")
    second = compile_processing_plan(source(connection_id="reconnected"), channel_input, "revision-two")
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != compile_processing_plan(source(source_id="second-source"), channel_input).fingerprint


def test_legacy_plan_keeps_its_serialized_shape_and_fingerprint():
    plan = compile_signal_plan(source(), setup(outputs=["O2", "O1"]), "legacy-revision")
    serialized = plan.to_dict()
    assert set(serialized) == {
        "source", "output_channels", "coefficients", "config_revision", "warnings", "setup", "fingerprint",
    }
    assert serialized["fingerprint"] == "3f59248ec9f959514c448aa4694fbabeceb992083d690f92536ed20063155691"
    assert ResolvedSignalPlan.from_dict(serialized) == plan
    assert plan.processing_input is None


@pytest.mark.parametrize("value", [
    {"source_id": "device"}, {"assignments": []}, {"manual_metadata": {}},
    {"original_reference": "Cz"}, {"template": "standard_1020"},
    {"schema_version": 2}, {"outputs": ["O1", "O1"]},
    {"reference": {"mode": "single", "channels": []}},
    {"reference": {"mode": "bipolar"}, "outputs": ["O1"]},
])
def test_processing_input_rejects_source_facts_and_invalid_mapping(value):
    with pytest.raises(ValueError):
        ProcessingInput.from_dict(value)


def test_profile_manual_montage_fills_missing_facts_without_mutating_observation():
    observed = source(("CH1", "C4"), original_reference=None)
    observed = replace(observed, channels=(
        ChannelDescriptor(0, "CH1", signal_type="unknown", name_provenance="generated"),
        replace(observed.channels[1], unit=None),
    ))
    manual = source(("C3", "C4"), provenance="user-declared")
    manual = replace(manual, channels=(replace(manual.channels[0], position=(0.0, 0.01, 0.02),
        coordinate_frame="head", position_provenance="user-declared"), manual.channels[1]))
    result = merge_source_montage(observed, manual)
    assert [c.name for c in result.channels] == ["C3", "C4"]
    assert result.channels[0].unit == "uV"
    assert result.channels[0].position == (0.0, 0.01, 0.02)
    assert result.original_reference == "Cz"
    assert result.connection_id == observed.connection_id
    assert observed.channels[0].name == "CH1"
    assert observed.original_reference is None
    assert SourceMetadata.from_dict(observed.to_dict()) == observed


@pytest.mark.parametrize("change, message", [
    ({"name": "C4"}, "Source row 0 changed"),
    ({"unit": "V"}, "Manual unit conflicts"),
    ({"signal_type": "eog"}, "Manual signal_type conflicts"),
    ({"reference": "M1"}, "Manual reference conflicts"),
    ({"position": (0.03, 0.04, 0.05)}, "Manual position conflicts"),
    ({"coordinate_frame": "device"}, "Manual coordinate frame conflicts"),
])
def test_profile_manual_montage_rejects_changes_to_known_facts(change, message):
    observed = source(("C3",))
    observed = replace(observed, channels=(replace(observed.channels[0], reference="Cz",
        position=(0.0, 0.01, 0.02), coordinate_frame="head", position_provenance="device-reported"),))
    manual = replace(observed, channels=(replace(observed.channels[0], **change),))
    with pytest.raises(ValueError, match=message):
        merge_source_montage(observed, manual)


def test_known_channel_named_ch1_is_not_treated_as_a_generated_placeholder():
    observed = source(("CH1",))
    with pytest.raises(ValueError, match="Source row 0 changed"):
        merge_source_montage(observed, source(("C3",)))


@pytest.mark.parametrize("change", [{"source_id": "another"}, {"sample_rate": 500}, {"original_reference": "M1"}])
def test_profile_manual_montage_rejects_source_identity_or_reference_changes(change):
    observed = source()
    with pytest.raises(ValueError):
        merge_source_montage(observed, replace(observed, **change))


def test_identity_preserves_every_value_and_does_not_alias_input():
    original = chunk()
    frozen = original.raw_data.copy()
    plan = compile_signal_plan(source())
    transformed = plan.apply(original)
    assert plan.is_identity
    assert transformed.timestamp == original.timestamp
    assert transformed.sequence_id == original.sequence_id
    np.testing.assert_array_equal(transformed.raw_data, original.raw_data)
    assert not np.shares_memory(transformed.raw_data, original.raw_data)
    transformed.raw_data[0, 0] = -123
    np.testing.assert_array_equal(original.raw_data, frozen)


def test_selection_reorders_without_unit_conversion():
    plan = compile_signal_plan(source(), setup(outputs=["O2", "O1"]))
    result = plan.apply(chunk())
    np.testing.assert_array_equal(result.data[:, 0], [30, 10])
    np.testing.assert_array_equal(result.triggers, [0, 3, 0])
    assert [c.name for c in plan.output_channels] == ["O2", "O1"]
    assert [c.unit for c in plan.output_channels] == ["uV", "uV"]
    assert not plan.is_identity


def test_average_reference_retains_dependencies_outside_output_subset():
    plan = compile_signal_plan(source(), setup(
        outputs=["O1", "Oz", "O2"],
        reference={"mode": "average", "channels": ["M1", "M2"]},
    ))
    np.testing.assert_allclose(plan.apply(chunk()).data[:, 0], np.array([7, 17, 27]) * 1e-6)
    assert all(c.unit == "V" and c.reference == "average(M1,M2)" for c in plan.output_channels)


def test_single_reference_and_car():
    single = compile_signal_plan(source(), setup(
        outputs=["O1"], reference={"mode": "single", "channels": ["M1"]},
    ))
    np.testing.assert_allclose(single.apply(chunk()).data[:, 0], [8e-6])
    car = compile_signal_plan(source(), setup(
        outputs=["O1", "Oz", "O2"], excluded_channels=["M1", "M2"],
        reference={"mode": "average", "channels": ["O1", "Oz", "O2"]},
    ))
    np.testing.assert_allclose(car.apply(chunk()).data[:, 0], [-10e-6, 0, 10e-6], atol=1e-20)


def test_bipolar_retains_derivation_and_drops_physical_position():
    observed = source()
    positioned = replace(observed.channels[0], position=(0.0, 0.02, 0.05), coordinate_frame="head", position_provenance="file-reported")
    observed = replace(observed, channels=(positioned, *observed.channels[1:]))
    plan = compile_signal_plan(observed, setup(
        outputs=[{"name": "O1-O2", "positive": "O1", "negative": "O2"}],
        reference={"mode": "bipolar"},
    ))
    np.testing.assert_allclose(plan.apply(chunk()).data[:, 0], [-20e-6])
    assert plan.output_channels[0].derivation == ("O1", "O2")
    assert plan.output_channels[0].position is None
    assert plan.output_channels[0].coordinate_frame is None


def test_known_voltage_units_are_normalized_before_subtraction():
    observed = source(("O1", "M1"))
    observed = replace(observed, channels=(replace(observed.channels[0], unit="mV"), observed.channels[1]))
    plan = compile_signal_plan(observed, setup(
        outputs=["O1"], reference={"mode": "single", "channels": ["M1"]},
    ))
    np.testing.assert_allclose(plan.apply(chunk((0.01, 2))).data[:, 0], [8e-6])


def test_unknown_units_allowed_for_selection_but_not_subtraction():
    observed = source(("O1", "M1"))
    observed = replace(observed, channels=tuple(replace(c, unit=None) for c in observed.channels))
    assert compile_signal_plan(observed).warnings
    with pytest.raises(ValueError, match="established voltage units"):
        compile_signal_plan(observed, setup(outputs=["O1"], reference={"mode": "single", "channels": ["M1"]}))


def test_manual_unknown_unit_and_type_declarations_are_explicit():
    observed = source(("CH1", "CH2"), original_reference=None)
    observed = replace(observed, channels=tuple(replace(c, unit=None, signal_type="unknown") for c in observed.channels))
    plan = compile_signal_plan(observed, setup(
        assignments=[
            {"source_index": 0, "expected_name": "CH1", "electrode": "O1", "unit": "uV", "signal_type": "eeg"},
            {"source_index": 1, "expected_name": "CH2", "electrode": "M1", "unit": "uV", "signal_type": "reference"},
        ], original_reference="Cz", outputs=["O1"],
        reference={"mode": "single", "channels": ["M1"]},
    ))
    np.testing.assert_allclose(plan.apply(chunk((10, 2))).data[:, 0], [8e-6])


def test_excluded_nan_channel_cannot_contaminate_rereferencing():
    plan = compile_signal_plan(source(), setup(
        outputs=["O1"], excluded_channels=["Oz"],
        reference={"mode": "single", "channels": ["M1"]},
    ))
    result = plan.apply(chunk((10, float("nan"), 30, 2, 4)))
    np.testing.assert_allclose(result.data, [[8e-6] * 3])


def test_nan_in_one_bipolar_pair_does_not_contaminate_another():
    plan = compile_signal_plan(source(), setup(
        outputs=[
            {"name": "first", "positive": "O1", "negative": "M1"},
            {"name": "second", "positive": "O2", "negative": "M2"},
        ], reference={"mode": "bipolar"},
    ))
    result = plan.apply(chunk((float("nan"), 20, 30, 2, 4)))
    assert np.isnan(result.data[0]).all()
    np.testing.assert_allclose(result.data[1], [26e-6] * 3)


@pytest.mark.parametrize("rate", [0, -1, 999.5, float("nan"), float("inf"), True])
def test_invalid_sample_rates_are_rejected(rate):
    with pytest.raises(ValueError, match="sample_rate"):
        source(sample_rate=rate)


def test_trigger_index_names_counts_and_provenance_are_validated():
    with pytest.raises(ValueError, match="final row"):
        source(trigger_row_index=0)
    with pytest.raises(ValueError, match="unique"):
        source(("Cz", "Cz"))
    with pytest.raises(ValueError, match="provenance"):
        source(provenance="guessed")
    invalid = source().to_dict()
    invalid["output_row_count"] = 100
    with pytest.raises(ValueError, match="output_row_count"):
        SourceMetadata.from_dict(invalid)
    invalid = source().to_dict()
    invalid["channels"][0]["index"] = 1
    with pytest.raises(ValueError, match="contiguous"):
        SourceMetadata.from_dict(invalid)


@pytest.mark.parametrize("values,match", [
    ({"outputs": ["O1", "O1"]}, "unique"),
    ({"outputs": ["Missing"]}, "Missing signal dependency"),
    ({"outputs": ["O1"], "excluded_channels": ["O1"]}, "Excluded"),
    ({"excluded_channels": ["Missing"]}, "Unknown excluded"),
    ({"reference": {"mode": "average", "channels": []}}, "nonempty"),
    ({"reference": {"mode": "single", "channels": ["M1", "M2"]}}, "exactly one"),
    ({"assignments": [{"source_index": 5, "electrode": "trigger", "expected_name": "trigger"}]}, "trigger row"),
    ({"assignments": [{"source_index": 0, "electrode": "O1", "expected_name": "Oz"}]}, "changed"),
    ({"assignments": [{"source_index": 0, "electrode": "O1", "expected_name": "O1", "unit": "mV"}]}, "unit conflicts"),
    ({"assignments": [{"source_index": 0, "electrode": "Oz", "expected_name": "O1"}]}, "unique"),
    ({"assignments": [{"source_index": 0, "electrode": "O1"}]}, "expected_name"),
    ({"reference": {"mode": "bipolar"}}, "explicit bipolar"),
    ({"original_reference": "M1"}, "conflicts"),
])
def test_invalid_setups_fail_before_samples_are_read(values, match):
    with pytest.raises(ValueError, match=match):
        compile_signal_plan(source(), setup(**values))


def test_source_binding_and_input_shape_are_checked():
    with pytest.raises(ValueError, match="does not match"):
        compile_signal_plan(source(), SignalSetup(source_id="other-source"))
    with pytest.raises(ValueError, match="Signal rows changed"):
        compile_signal_plan(source()).apply(chunk((1, 2)))


def test_unknown_or_incompatible_original_references_block_subtraction():
    config = setup(outputs=["O1"], reference={"mode": "single", "channels": ["M1"]})
    with pytest.raises(ValueError, match="original reference"):
        compile_signal_plan(source(original_reference=None), config)
    observed = source()
    observed = replace(observed, channels=(*observed.channels[:3], replace(observed.channels[3], reference="other-reference"), observed.channels[4]))
    with pytest.raises(ValueError, match="original reference"):
        compile_signal_plan(observed, config)


@pytest.mark.parametrize("signal_type", ["eog", "emg", "unknown", "ground", "stim"])
def test_auxiliary_channels_are_not_implicitly_used_as_eeg_references(signal_type):
    observed = source()
    observed = replace(observed, channels=(*observed.channels[:3], replace(observed.channels[3], signal_type=signal_type), observed.channels[4]))
    with pytest.raises(ValueError):
        compile_signal_plan(observed, setup(outputs=["O1"], reference={"mode": "single", "channels": ["M1"]}))


def test_metadata_setup_and_plan_round_trip_through_json_and_pickle():
    observed = source()
    config = setup(outputs=["O2", "O1"], manual_metadata=observed.to_dict(), template="standard_1020")
    plan = compile_signal_plan(observed, config, config_revision="revision-one")
    assert SourceMetadata.from_dict(json.loads(json.dumps(observed.to_dict()))) == observed
    assert SignalSetup.from_dict(json.loads(json.dumps(config.to_dict()))) == config
    restored = ResolvedSignalPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert restored == plan
    restored = pickle.loads(pickle.dumps(plan))
    np.testing.assert_array_equal(restored.apply(chunk()).raw_data, plan.apply(chunk()).raw_data)
    assert restored.fingerprint == plan.fingerprint
    with pytest.raises(FrozenInstanceError):
        observed.source_id = "changed"


def test_semantic_fingerprint_excludes_connection_and_configuration_identity():
    observed = source()
    reconnected = replace(observed, connection_id="connection-two")
    assert observed.fingerprint == reconnected.fingerprint
    assert compile_signal_plan(observed, config_revision="one").fingerprint == compile_signal_plan(reconnected, config_revision="two").fingerprint
    assert observed.fingerprint != replace(observed, sample_rate=500).fingerprint


def test_tampered_serialized_plan_cannot_bypass_compiler():
    plan = compile_signal_plan(source())
    data = plan.to_dict()
    data["coefficients"][0][0] = 2
    with pytest.raises(ValueError, match="fingerprint"):
        ResolvedSignalPlan.from_dict(data)
    data.pop("fingerprint")
    with pytest.raises(ValueError, match="compiled"):
        ResolvedSignalPlan.from_dict(data)
    data["coefficients"][0][0] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        ResolvedSignalPlan.from_dict(data)


def test_position_requires_finite_coordinates_and_explicit_frame():
    with pytest.raises(ValueError, match="coordinate_frame"):
        ChannelDescriptor(0, "Cz", position=(0, 0, 0))
    with pytest.raises(ValueError, match="finite"):
        ChannelDescriptor(0, "Cz", position=(float("nan"), 0, 0), coordinate_frame="head", position_provenance="file-reported")


def test_persisted_row_assignment_cannot_follow_a_reordered_source_after_restart():
    config = setup(assignments=[{"source_index": 0, "electrode": "Left", "expected_name": "O1"}], outputs=["Left"])
    saved = SignalSetup.from_dict(json.loads(json.dumps(config.to_dict())))
    with pytest.raises(ValueError, match="changed"):
        compile_signal_plan(source(("O2", "O1", "Oz", "M1", "M2")), saved)
