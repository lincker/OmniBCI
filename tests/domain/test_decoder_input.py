"""Decoder requirements describe and validate the final mapped channel set."""
from dataclasses import FrozenInstanceError

import pytest

from omnibci.domain.decoder_input import DecoderChannelRequirements
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, compile_processing_plan


def channels(*names, signal_type="eeg"):
    return tuple(ChannelDescriptor(index=index, name=name, signal_type=signal_type, unit="uV")
                 for index, name in enumerate(names))


def test_requirements_normalize_names_preserve_default_order_and_round_trip():
    requirements = DecoderChannelRequirements.from_dict({
        "description": " Occipital input ", "signal_type": " eeg ", "min_count": 2,
        "required_names": [" Oz "], "default_names": [" O2 ", "Oz", "O1"],
    })
    assert requirements.default_names == ("O2", "Oz", "O1")
    assert requirements.required_names == ("Oz",)
    assert requirements.description == "Occipital input"
    assert requirements.signal_type == "eeg"
    assert DecoderChannelRequirements.from_dict(requirements.to_dict()) == requirements
    with pytest.raises(FrozenInstanceError):
        requirements.min_count = 3


@pytest.mark.parametrize("value", [
    [], {"unknown": 1}, {"description": 1}, {"description": " "}, {"signal_type": []},
    {"signal_type": ""}, {"min_count": True}, {"min_count": 0}, {"min_count": 1.5},
    {"min_count": "2"}, {"required_names": "Oz"}, {"default_names": "Oz"},
    {"required_names": ["Oz", " Oz "]}, {"default_names": ["Oz", "Oz"]},
    {"required_names": [""]}, {"default_names": [1]}, {"required_names": None},
    {"min_count": 2, "default_names": ["Oz"]},
    {"required_names": ["Oz"], "default_names": ["O1", "O2"]},
])
def test_invalid_requirements_are_rejected(value):
    with pytest.raises(ValueError):
        DecoderChannelRequirements.from_dict(value)


def test_required_names_are_a_subset_and_do_not_prescribe_order():
    requirements = DecoderChannelRequirements(signal_type="eeg", min_count=2, required_names=("Oz", "O1"))
    requirements.validate_channels(channels("O2", "O1", "Oz"))
    requirements.validate_channels(channels("Oz", "O1"))
    with pytest.raises(ValueError, match="missing required channels: Oz"):
        requirements.validate_channels(channels("O2", "O1"))
    with pytest.raises(ValueError, match="at least 2"):
        requirements.validate_channels(channels("Oz"))
    with pytest.raises(ValueError, match="incompatible channels: Oz, O1"):
        requirements.validate_channels(channels("Oz", "O1", signal_type="eog"))


def test_defaults_are_suggestions_and_empty_defaults_allow_required_names():
    DecoderChannelRequirements(default_names=("O1", "O2")).validate_channels(channels("Oz"))
    requirements = DecoderChannelRequirements(required_names=("Oz",))
    assert requirements.default_names == ()
    requirements.validate_channels(channels("Oz"))


def test_unselected_channels_and_trigger_do_not_satisfy_output_requirements():
    source = SignalInfo(source_id="reader", sample_rate=250,
                                   channels=channels("O1", "Oz", "M1"), trigger_row_index=3)
    plan = compile_processing_plan(source, ProcessingInput(outputs=(ChannelBinding("O1", "channel-0"),)))
    with pytest.raises(ValueError, match="at least 2"):
        DecoderChannelRequirements(min_count=2).validate_channels(plan.output_channels)
    with pytest.raises(ValueError, match="missing required channels: Oz"):
        DecoderChannelRequirements(required_names=("Oz",)).validate_channels(plan.output_channels)
