"""Simulated CURRY TCP data reaches the real CCA pipeline without a session."""

import asyncio
import json
from queue import Queue
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from tests.processing_helpers import make_processor
from omnibci.application.events import CommandRequest
from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, compile_processing_plan
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.domain.signal_info import SignalInfo
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessorCommand
from omnibci.shared.events import EventType


_HEADER = struct.Struct(">4sHHIII")


def _packet(code, request, body, *, start_sample=0):
    return _HEADER.pack(b"DATA", code, request, start_sample, len(body), 0) + body


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_high_bits", [0, 0xFF00])
async def test_simulated_curry_decodes_selected_eeg_without_lifecycle_commands(monkeypatch, trigger_high_bits):
    sample_rate = 500
    labels = ("Fz", "O1", "Oz", "O2", "VEOG", "Trigger")
    times = np.arange(700) / sample_rate
    raw = np.asarray([
        20 * np.sin(2 * np.pi * 14 * times),
        15 * np.sin(2 * np.pi * 8 * times),
        18 * np.sin(2 * np.pi * 8 * times + 0.3),
        12 * np.sin(2 * np.pi * 8 * times + 0.6),
        np.linspace(-100, 100, len(times)),
        np.zeros(len(times)),
    ], dtype="<f4")
    raw[-1, 25] = 1
    blocks = np.split(raw, [100, 300], axis=1)
    requests = []
    server_done = asyncio.get_running_loop().create_future()

    async def handle_client(reader, writer):
        try:
            while True:
                identifier, code, request, _, body_size, _ = _HEADER.unpack(
                    await reader.readexactly(_HEADER.size))
                assert (identifier, code, body_size) == (b"CTRL", 2, 0)
                requests.append(request)
                if request == 1:
                    writer.write(_packet(1, 1, struct.pack("<i", 803)))
                elif request == 6:
                    writer.write(_packet(1, 2, struct.pack("<IiiiII", 24, len(labels), sample_rate, 4, 0, 0)))
                elif request == 3:
                    records = []
                    for index, label in enumerate(labels):
                        record = bytearray(144)
                        struct.pack_into("<i", record, 0, index)
                        encoded = label.encode("utf-16-le")
                        record[4:4 + len(encoded)] = encoded
                        records.append(record)
                    writer.write(_packet(1, 4, b"".join(records)))
                elif request == 8:
                    start_sample = 1000
                    for block in blocks:
                        wire_block = block.copy()
                        wire_block[-1] += trigger_high_bits
                        writer.write(_packet(2, 1, wire_block.T.tobytes(), start_sample=start_sample))
                        start_sample += block.shape[1]
                elif request == 9:
                    break
                else:
                    raise AssertionError(f"Unexpected simulated CURRY request: {request}")
                await writer.drain()
        except BaseException as exc:
            server_done.set_exception(exc)
        else:
            server_done.set_result(None)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    parameters = {"DATA_PROVIDER_IP": "127.0.0.1", "DATA_PROVIDER_PORT": server.sockets[0].getsockname()[1],
                  "channel_labels": [f"Saved row {index}" for index in range(len(labels))]}
    catalog = PluginCatalog()
    inputs = ProcessingInput(outputs=(
        ChannelBinding("O2", "channel-3", "eeg"),
        ChannelBinding("O1", "channel-1", "eeg"),
        ChannelBinding("Oz", "channel-2", "eeg"),
    ))
    snapshot = {
        "CONFIG.revision": "curry-revision",
        "ACQUISITION.signal_source": "neuroscan_curry8_reader",
        "PLUGINS.bindings": [
            ProfileBinding("acquisition.source", "acquisition", None,
                           catalog.resolve("acquisition", "neuroscan_curry8_reader").selection,
                           parameters).to_dict(),
            ProfileBinding("processing.decoder", "decoder", None,
                           catalog.resolve("decoder", "THUBCI_SSVEP_classic40CCA").selection,
                           {"window_duration": 1.0, "offset_time": 0}, processing_input=inputs).to_dict(),
            ProfileBinding("processing.epoch_detector", "epoch_detector", None,
                           catalog.resolve("epoch_detector", "DEFAULT_EPOCH_DETECTOR").selection,
                           {}).to_dict(),
        ],
    }
    source = load_configured_plugin(snapshot, "acquisition.source", "acquisition", SignalSource)
    events = []
    bus = SimpleNamespace(put_event=events.append)
    data = DataManager(Queue())
    received = []
    put_data = data.put_data

    def capture(chunk):
        received.append(chunk)
        put_data(chunk)

    monkeypatch.setattr(data, "put_data", capture)
    acquisition = AcquisitionRuntime(
        source, Queue(), data, bus, source_id=source.plugin_type, preparation_required=True,
        generation="curry-generation", config_revision="curry-revision",
    )
    processor = make_processor(
        None, None, Queue(), data, bus,
        generation="curry-generation", config_revision="curry-revision", settings_snapshot=snapshot,
    )

    def result(command_id):
        value = next(event.data for event in reversed(events)
                     if event.type is EventType.CommandResult and event.data.command_id == command_id)
        assert value.ok, value.message
        return value.data

    async def acquisition_command(command, command_id, payload=None):
        await acquisition.execute((command, CommandRequest(command_id, payload)))
        return result(command_id)

    def processing_command(command, command_id, payload):
        processor._execute((command, CommandRequest(command_id, payload)))
        return result(command_id)

    try:
        await acquisition_command(AcquisitionCommand.connect, "connect")
        metadata = await acquisition_command(AcquisitionCommand.get_metadata, "metadata")
        descriptor = SignalInfo.from_dict(metadata["descriptor"])
        assert descriptor.sample_rate == sample_rate
        assert descriptor.output_row_count == len(labels)
        assert [channel.name for channel in descriptor.channels] == list(labels[:-1])
        assert all(channel.signal_type == "unknown" for channel in descriptor.channels)
        assert json.loads(metadata["metadata"])["channel_labels"] == list(labels)
        assert "channel_types" not in json.loads(metadata["metadata"])
        plan = compile_processing_plan(descriptor, inputs, config_revision="curry-revision")
        receipt = {
            "generation": "curry-generation", "config_revision": "curry-revision",
            "connection_id": descriptor.connection_id,
            "source_fingerprint": descriptor.fingerprint, "plan_fingerprint": plan.fingerprint,
        }
        processing_command(SignalProcessorCommand.prepare_signal, "prepare", {
            "plan": plan.to_dict(), "receipt": receipt,
        })
        await acquisition_command(AcquisitionCommand.start_stream, "stream", receipt)
        async with asyncio.timeout(5):
            while processor._last_sequence < len(blocks) - 1:
                await processor._current_state.run()

        trials = [event.data for event in events if event.type is EventType.TrialCompleted]
        assert len(trials) == 1
        assert trials[0].generation == "curry-generation"
        assert trials[0].trial_number == 1
        assert trials[0].classification.target_id == 1
        assert trials[0].classification.event_id == 1
        assert processor._signal_plan.source_indices == (3, 1, 2)
        assert [channel.signal_type for channel in processor._signal_plan.output_channels] == ["eeg"] * 3

        await acquisition_command(AcquisitionCommand.stop_stream, "stop")
        assert [chunk.sequence_id for chunk in received] == list(range(len(blocks)))
        for chunk, expected in zip(received, blocks, strict=True):
            np.testing.assert_array_equal(chunk.raw_data, expected)
            np.testing.assert_array_equal(chunk.triggers, expected[-1])
            assert chunk.raw_data.dtype == expected.dtype
        assert data.empty()
        await asyncio.wait_for(server_done, timeout=2)
        assert requests == [1, 6, 3, 8, 9]
    finally:
        await acquisition.shutdown()
        server.close()
        await server.wait_closed()
