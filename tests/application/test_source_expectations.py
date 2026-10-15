"""Saved experiment values are compared with independently observed signal facts."""
from copy import deepcopy

import pytest

from omnibci.application.source_expectations import (
    compare_source_expectations, validate_source_expectations,
)
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo


def source_info(provenance="device-reported"):
    return SignalInfo("source", 500, (ChannelDescriptor(0, "C3"), ChannelDescriptor(1, "C4")),
                      2, connection_id="connection", provenance=provenance)


def source_binding(**parameters):
    return {"slot": "acquisition.source", "parameters": parameters}


@pytest.mark.parametrize("provenance", ["device-reported", "file-reported"])
def test_matching_expectations_include_the_native_event_row(provenance):
    binding = source_binding(sample_rate=500, channel_number=3)
    original = deepcopy(binding)
    info = source_info(provenance)
    assert compare_source_expectations(binding, info) == {
        "status": "matched",
        "sample_rate": {"expected": 500, "actual": 500},
        "channel_number": {"expected": 3, "actual": 3},
    }
    validate_source_expectations({"PLUGINS.bindings": [binding]}, info)
    assert binding == original


@pytest.mark.parametrize("parameters", [{}, {"sample_rate": 500}, {"channel_number": 3}])
def test_missing_experiment_baseline_is_unconfirmed(parameters):
    binding = source_binding(**parameters)
    comparison = compare_source_expectations(binding, source_info())
    assert comparison["status"] == "unconfirmed"
    with pytest.raises(ValueError, match="unconfirmed.*save and apply"):
        validate_source_expectations({"PLUGINS.bindings": [binding]}, source_info())


def test_flat_settings_and_other_plugins_cannot_establish_the_source_baseline():
    snapshot = {
        "ACQUISITION.sample_rate": 500, "ACQUISITION.channel_number": 3,
        "PLUGINS.bindings": [{"slot": "processing.decoder", "parameters": {
            "sample_rate": 500, "channel_number": 3,
        }}],
    }
    assert compare_source_expectations(None, source_info())["status"] == "unconfirmed"
    with pytest.raises(ValueError, match="unconfirmed"):
        validate_source_expectations(snapshot, source_info())


@pytest.mark.parametrize(("parameters", "difference"), [
    ({"sample_rate": 250, "channel_number": 3}, "sample rate \\(Hz\\): expected 250, observed 500"),
    ({"sample_rate": 500, "channel_number": 2}, "output rows \\(including event row\\): expected 2, observed 3"),
])
def test_mismatch_reports_expected_and_observed_values_without_modifying_them(parameters, difference):
    binding = source_binding(**parameters)
    info = source_info()
    original = deepcopy(binding)
    assert compare_source_expectations(binding, info)["status"] == "mismatched"
    with pytest.raises(ValueError, match=difference + ".*Adjust the source.*save and apply"):
        validate_source_expectations({"PLUGINS.bindings": [binding]}, info)
    assert binding == original
    assert info.sample_rate == 500 and info.output_row_count == 3


@pytest.mark.parametrize("provenance", ["user-declared", "protocol-defined"])
@pytest.mark.parametrize("parameters", [{}, {"sample_rate": 500, "channel_number": 3},
                                        {"sample_rate": 1000, "channel_number": 10}])
def test_declared_information_cannot_independently_verify_a_profile(provenance, parameters):
    binding = source_binding(**parameters)
    info = source_info(provenance)
    assert compare_source_expectations(binding, info)["status"] == "unverified"
    validate_source_expectations({"PLUGINS.bindings": [binding]}, info)
