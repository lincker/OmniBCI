"""Scan45 frame width follows each saved profile, including its event channel."""
import struct

import numpy as np
import pytest

from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45 import scan45
from omnibci.sdk import PluginContext


def packet(channels, events):
    samples = []
    expected = []
    for sample_index, event in enumerate(events):
        eeg = [sample_index * 100 + channel for channel in range(1, channels)]
        # The event code occupies the low byte of the final 32-bit word.
        samples.extend([*eeg, 0xABCD0000 | event])
        expected.append(eeg)
    return struct.pack(f"<{len(samples)}I", *samples), np.array(expected).T


@pytest.mark.parametrize("channels", [2, 9, 10, 33, 65])
def test_decode_uses_configured_frame_width_and_final_event_row(channels):
    reader = scan45.Scan45DataReader(parameters={"channel_number": channels})
    body, eeg = packet(channels, [0, 7, 7, 0, 9])
    header = scan45.Scan45Message.Header("DATA", 2, 2, len(body))

    decoded = reader._parse_body_data(header, body)

    assert decoded.shape == (channels, 5)
    np.testing.assert_array_equal(decoded[:-1], eeg)
    np.testing.assert_array_equal(decoded[-1], [0, 7, 0, 0, 9])


@pytest.mark.parametrize("channels", [2, 10, 33])
def test_event_debounce_spans_packets(channels):
    reader = scan45.Scan45DataReader(parameters={"channel_number": channels})
    first, _ = packet(channels, [7, 7])
    second, _ = packet(channels, [7, 0, 7, 8])

    np.testing.assert_array_equal(reader._unpack_int32_eeg(first)[-1], [7, 0])
    np.testing.assert_array_equal(reader._unpack_int32_eeg(second)[-1], [0, 0, 7, 8])


@pytest.mark.parametrize("kwargs", [{"parameters": {}}, {"context": PluginContext()}])
def test_explicit_default_retains_ten_channel_layout(kwargs):
    reader = scan45.Scan45DataReader(**kwargs)
    body, eeg = packet(10, [0, 7])
    decoded = reader._unpack_int32_eeg(body)

    assert reader.channel_number == 10
    np.testing.assert_array_equal(decoded[:-1], eeg)
    np.testing.assert_array_equal(decoded[-1], [0, 7])


@pytest.mark.parametrize("configured,expected", [(None, 10), (33, 33)])
def test_legacy_constructor_reads_channel_count_with_default(monkeypatch, configured, expected):
    settings = {"ACQUISITION.DATA_PROVIDER_IP": "127.0.0.1", "ACQUISITION.DATA_PROVIDER_PORT": 4000}
    if configured is not None:
        settings["ACQUISITION.channel_number"] = configured
    monkeypatch.setattr(scan45, "settings", settings)
    assert scan45.Scan45DataReader().channel_number == expected


@pytest.mark.parametrize("channels", [None, True, False, 0, 1, -1, 3.5, "33"])
def test_invalid_channel_count_rejected_before_connecting(channels):
    with pytest.raises(ValueError, match="channel_number"):
        scan45.Scan45DataReader(parameters={"channel_number": channels})


@pytest.mark.parametrize("body", [b"", bytes(3), bytes(8), bytes(15), bytes(17)])
def test_malformed_frame_does_not_change_event_state(body):
    reader = scan45.Scan45DataReader(parameters={"channel_number": 4})
    reader._previous_event = 7
    with pytest.raises(ValueError, match="complete samples of 4 channels"):
        reader._unpack_int32_eeg(body)
    assert reader._previous_event == 7


def test_packet_header_size_must_match_body():
    reader = scan45.Scan45DataReader(parameters={"channel_number": 4})
    header = scan45.Scan45Message.Header("DATA", 2, 2, 32)
    with pytest.raises(ValueError, match="body length"):
        reader._parse_body_data(header, bytes(16))


@pytest.mark.parametrize("format_code,error", [(1, NotImplementedError), (3, ValueError)])
def test_unsupported_data_format_fails_explicitly(format_code, error):
    reader = scan45.Scan45DataReader(parameters={"channel_number": 4})
    header = scan45.Scan45Message.Header("DATA", 2, format_code, 16)
    with pytest.raises(error):
        reader._parse_body_data(header, bytes(16))
