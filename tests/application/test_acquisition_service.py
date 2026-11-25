"""
Unit tests for AcquisitionAppService.
"""
from copy import deepcopy
from omnibci.application.source_metadata import source_descriptor
from omnibci.domain.ports.acquisition import SourceObservation
import pytest
from unittest.mock import MagicMock, call

from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand,
    DisconnectDataProviderCommand,
    StartStreamCommand,
    StopStreamCommand,
)
from omnibci.application.events import AcquisitionSnapshot, CommandResult, StreamDrainReceipt
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.shared.events import Event, EventType
from omnibci.application.services.acquisition_service import AcquisitionAppService


@pytest.fixture(autouse=True)
def _running_engine(engine_service):
    """Command-focused tests exercise a runtime-ready EnginePort."""
    engine_service._running = True
    engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock()
    engine_service._engine_thread.is_alive.return_value = True


def test_command_executor_is_required():
    with pytest.raises(TypeError):
        AcquisitionAppService()


class TestAcquisitionConnect:
    def test_connect_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_connect(ConnectDataProviderCommand())
        events = [call.args[0].type for call in mock_engine.dispatch.call_args_list]
        assert events == [EventType.ConnectDataProvider, EventType.GetSourceMetadata]

    def test_connect_does_not_guess_state(self, acquisition_service):
        # Projection semantics: commands dispatch intent; only worker-reported
        # AcquisitionState events (on_acquisition_state) move the flags.
        acquisition_service.handle_connect(ConnectDataProviderCommand())
        assert acquisition_service._connected is False

    def test_disconnect_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_disconnect(DisconnectDataProviderCommand())
        event = mock_engine.dispatch.call_args[0][0]
        assert event.type == EventType.DisconnectDataProvider


class TestAcquisitionStream:
    def test_start_stream_dispatches_event(self, acquisition_service, mock_engine):
        acquisition_service.handle_start_stream(StartStreamCommand())
        assert [item.args[0].type for item in mock_engine.dispatch.call_args_list] == [
            EventType.GetSourceMetadata, EventType.StartStream,
        ]

    def test_start_stream_does_not_guess_state(self, acquisition_service):
        acquisition_service.handle_start_stream(StartStreamCommand())
        assert acquisition_service._streaming is False

    def test_stop_stream_waits_for_processing_drain(self):
        executor = MagicMock()
        receipt = StreamDrainReceipt("generation", "revision", 3, 2, False)
        executor.request.return_value = CommandResult("stop", "StopAndDrain", True, data=receipt.to_dict())
        service = AcquisitionAppService(executor)
        service.handle_stop_stream(StopStreamCommand())
        executor.request.assert_called_once_with(EventType.StopAndDrain, timeout_s=None)

    def test_stop_stream_rejects_missing_drain_evidence(self):
        executor = MagicMock()
        executor.request.return_value = CommandResult("stop", "StopAndDrain", True)
        service = AcquisitionAppService(executor)
        with pytest.raises(RuntimeError, match="valid stream drain evidence"):
            service.handle_stop_stream(StopStreamCommand())

    def test_source_start_does_not_require_processing_receipt(self):
        executor = MagicMock()
        executor.request.return_value = CommandResult("drain", "StopAndDrain", True,
            data=StreamDrainReceipt(None, None, -1, 0, False).to_dict())
        service = AcquisitionAppService(executor)
        receipt = {"connection_id": "connection"}
        service.set_signal_preparer(lambda: receipt)
        service.handle_start_stream(StartStreamCommand())
        assert executor.request.call_args_list == [
            call(EventType.GetSourceMetadata, {"refresh": True}, timeout_s=5.0), call(EventType.StartStream, None),
        ]

    def test_start_during_streaming_keeps_the_current_epoch(self):
        executor = MagicMock()
        service = AcquisitionAppService(executor)
        service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(True, True)))
        service.handle_start_stream(StartStreamCommand())
        assert executor.request.call_args_list[-1] == call(EventType.StartStream, None)
        assert all(item.args[0] != EventType.StopAndDrain for item in executor.request.call_args_list)

    def test_source_read_failure_is_still_reported(self):
        executor = MagicMock()
        executor.request.side_effect = RuntimeError("source unavailable")
        service = AcquisitionAppService(executor)
        with pytest.raises(RuntimeError, match="source unavailable"):
            service.handle_start_stream(StartStreamCommand())
        assert executor.request.call_args_list[-1] == call(EventType.StartStream, None)

    def test_failed_processing_preparation_does_not_block_source(self):
        executor = MagicMock()
        service = AcquisitionAppService(executor)
        service.set_signal_preparer(MagicMock(side_effect=ValueError("Missing C3")))
        service.handle_start_stream(StartStreamCommand())
        assert executor.request.call_args_list[-1] == call(EventType.StartStream, None)
        assert all(item.args[0] != EventType.StopAndDrain for item in executor.request.call_args_list)



class TestAcquisitionStatus:
    def test_initial_status(self, acquisition_service):
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is False
        assert result.streaming is False

    def test_status_reflects_worker_snapshots(self, acquisition_service):
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is True
        assert result.streaming is True

    def test_snapshot_can_clear_state(self, acquisition_service):
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        # e.g. file playback EOF: worker reports stream closed
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=False)))
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is True
        assert result.streaming is False

    def test_bad_payload_is_rejected(self, acquisition_service):
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState, {"streaming": False}))  # wrong type
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.streaming is True   # projection untouched

    def test_reset_clears_projection(self, acquisition_service):
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        acquisition_service.reset()
        result = acquisition_service.handle_get_status(GetAcquisitionStatusQuery())
        assert result.connected is False
        assert result.streaming is False

    def test_metadata_is_confirmed_then_invalidated_on_disconnect(self):
        from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
        metadata = source_descriptor(SourceObservation(native_text="native", signal_info=SignalInfo(sample_rate=250, channels=(ChannelDescriptor(0, "C3", channel_id="channel-0"),)), native_event="final-row", provenance="device-reported"), source_id="test", connection_id="connection")
        executor = MagicMock()
        executor.request.return_value.data = _metadata_response(metadata)
        service = AcquisitionAppService(executor)
        result = service.get_metadata(refresh=True)
        assert {key: result[key] for key in ("source_id", "connection_id", "metadata", "descriptor")} == {
            key: _metadata_response(metadata)[key]
            for key in ("source_id", "connection_id", "metadata", "descriptor")}
        assert result["comparison"]["status"] == "unconfirmed"
        executor.request.assert_called_once_with(EventType.GetSourceMetadata, {"refresh": True}, timeout_s=5.0)
        assert service.cached_descriptor == deepcopy(metadata)
        status = service.handle_get_status(GetAcquisitionStatusQuery())
        assert status.sample_rate == 250 and status.channel_count == 1
        assert status.metadata_available and status.metadata_provenance == "device-reported"
        service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(False, False)))
        assert service.cached_descriptor is None

    def test_configured_dimensions_are_not_reported_as_source_metadata(self):
        service = AcquisitionAppService(MagicMock(), settings={
            "PLUGINS.bindings": [{
                "slot": "acquisition.source", "kind": "acquisition",
                "selection": {"package_id": "test.source", "contribution_id": "default", "version": "1.0.0"},
                "parameters": {},
            }],
            "ACQUISITION.channel_count": 9,
            "ACQUISITION.sample_rate": 1000,
        })
        status = service.handle_get_status(GetAcquisitionStatusQuery())
        assert status.source == "test.source/default@1.0.0"
        assert status.channel_count is None
        assert status.sample_rate is None
        assert status.metadata_available is False


def _metadata_response(metadata):
    return {"source_id": metadata["source_id"], "connection_id": metadata["connection_id"],
            "metadata": "Test amplifier\nVendor status: recording enabled\n<unknown />\n",
            "descriptor": deepcopy(metadata), "descriptor_error": None}


def _metadata_service():
    from omnibci.application.events import CommandResult
    from omnibci.application.plugin_management.identity import PluginSelection
    from omnibci.application.configuration.models import ProfileBinding
    from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
    metadata = source_descriptor(SourceObservation(native_text="native", signal_info=SignalInfo(sample_rate=250, channels=(ChannelDescriptor(0, "C3", unit="uV", channel_id="channel-0"),)), native_event="final-row", provenance="device-reported"), source_id="test", connection_id="connection")
    response = _metadata_response(metadata)
    executor = MagicMock()
    executor.request.return_value = CommandResult("read", "GetSourceMetadata", True, data=response)
    service = AcquisitionAppService(executor)
    binding = ProfileBinding("acquisition.source", "acquisition",
        "00000000-0000-0000-0000-000000000001", PluginSelection("test.source", "default", "1"),
        {"sample_rate": 250, "channel_count": 2}, profile_revision=1)
    provider = MagicMock(return_value=binding.to_dict())
    service.set_metadata_binding(provider)
    return service, executor, provider, binding, metadata, response


@pytest.mark.parametrize("raw", [
    "", "Test amplifier\r\n  Vendor field: <unknown /> & value\n",
    '{"sample_rate": 999, "channel_count": 99, "provenance": "vendor-text"}',
])
def test_metadata_preserves_opaque_plugin_text_and_exposes_acquisition_descriptor(raw):
    service, executor, provider, _, metadata, response = _metadata_service()
    response["metadata"] = raw
    value = service.get_metadata(refresh=True)
    assert value == {
        **{key: response[key] for key in ("source_id", "connection_id", "metadata", "descriptor")},
        "comparison": {"status": "matched", "sample_rate": {"expected": 250, "actual": 250},
                       "channel_count": {"expected": 2, "actual": 2}},
    }
    assert value["metadata"] == raw
    value["metadata"] = "changed by caller"
    value["descriptor"]["channels"][0]["name"] = "changed by caller"
    assert service._raw_metadata["metadata"] == raw
    assert service._raw_metadata["descriptor"]["channels"][0]["name"] == "C3"
    assert service.cached_descriptor == deepcopy(metadata)
    status = service.handle_get_status(GetAcquisitionStatusQuery())
    assert status.sample_rate == 250 and status.channel_count == 1
    assert status.metadata_provenance == "device-reported"
    provider.assert_called_once_with()
    executor.request.assert_called_once_with(EventType.GetSourceMetadata, {"refresh": True}, timeout_s=5.0)


def test_cached_signal_metadata_belongs_to_the_running_acquisition_binding():
    from dataclasses import replace
    service, _, _, binding, metadata, _ = _metadata_service()
    service.get_metadata()
    assert service.cached_descriptor_for(binding.to_dict()) == deepcopy(metadata)
    other_profile = replace(binding, profile_id="00000000-0000-0000-0000-000000000002")
    assert service.cached_descriptor_for(other_profile.to_dict()) == deepcopy(metadata)
    current = replace(binding, profile_id=None, profile_revision=0)
    assert service.cached_descriptor_for(current.to_dict()) == deepcopy(metadata)
    assert service.cached_descriptor_for(replace(binding, parameters={"sample_rate": 500}).to_dict()) is None
    assert service.cached_descriptor_for(None) is None
    assert service.cached_descriptor_for(replace(binding, profile_revision=2).to_dict()) == deepcopy(metadata)


@pytest.mark.parametrize("invalidation", ["disconnect", "reset", "worker-disconnect", "failed-read"])
def test_raw_and_signal_metadata_are_cleared_with_the_connection(invalidation):
    service, executor, _, binding, _, _ = _metadata_service()
    service.get_metadata()
    if invalidation == "disconnect":
        service.handle_disconnect(DisconnectDataProviderCommand())
    elif invalidation == "reset":
        service.reset()
    elif invalidation == "worker-disconnect":
        service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(False, False)))
    else:
        executor.request.side_effect = RuntimeError("Metadata unavailable")
        with pytest.raises(RuntimeError, match="unavailable"):
            service.get_metadata()
    assert service.cached_descriptor_for(binding.to_dict()) is None
    assert service._raw_metadata is None
    assert service.handle_get_status(GetAcquisitionStatusQuery()).metadata_available is False


def test_raw_metadata_remains_available_when_processing_cannot_normalize_it():
    from omnibci.application.services.command_result_service import CommandRejectedError
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"] = None
    response["descriptor_error"] = "Source does not report a sample rate"
    result = service.get_metadata()
    assert result["metadata"] == response["metadata"]
    assert result["comparison"] is None
    assert service.handle_get_status(GetAcquisitionStatusQuery()).metadata_available is True
    with pytest.raises(CommandRejectedError, match="sample rate") as rejected:
        service.get_descriptor()
    assert rejected.value.result.error_code == "INVALID_ACQUISITION_DESCRIPTOR"
    assert service._raw_metadata["metadata"] == response["metadata"]
    assert service.cached_descriptor is None


@pytest.mark.parametrize("raw", [
    "sample_rate: 1000\nchannel_count: 64\nprovenance: device-reported",
    '{"sample_rate": 1000, "channel_count": 64, "provenance": "device-reported"}',
])
def test_status_does_not_extract_dimensions_or_provenance_from_metadata_text(raw):
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"] = None
    response["descriptor_error"] = "Source does not describe signal rows"
    response["metadata"] = raw
    service.get_metadata()
    status = service.handle_get_status(GetAcquisitionStatusQuery())
    assert status.sample_rate is None
    assert status.channel_count is None
    assert status.metadata_provenance is None
    assert status.metadata_available is True


def test_status_does_not_present_configuration_metadata_as_device_dimensions():
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"]["provenance"] = "user-declared"
    response["metadata"] = "This source does not support querying metadata."
    service.get_metadata()
    status = service.handle_get_status(GetAcquisitionStatusQuery())
    assert status.sample_rate is None
    assert status.channel_count is None
    assert status.metadata_available is True
    assert status.metadata_provenance == "user-declared"


def test_processing_receives_only_normalized_signal_metadata():
    service, _, _, _, metadata, _ = _metadata_service()
    assert service.get_descriptor() == deepcopy(metadata)


def test_reconnect_changes_status_identity_and_invalidates_previous_connection_metadata():
    service, _, _, binding, _, _ = _metadata_service()
    service.get_metadata()
    assert service.handle_get_status(GetAcquisitionStatusQuery()).connection_id == "connection"
    service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(True, False, "connection")))
    assert service.cached_descriptor_for(binding.to_dict()) is not None
    service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(True, False, "reconnected")))
    status = service.handle_get_status(GetAcquisitionStatusQuery())
    assert status.connected is True
    assert status.connection_id == "reconnected"
    assert status.metadata_available is False
    assert service.cached_descriptor_for(binding.to_dict()) is None
    assert service._raw_metadata is None
    service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(False, False)))
    assert service.handle_get_status(GetAcquisitionStatusQuery()).connection_id is None


@pytest.mark.parametrize("change", [
    {"metadata": []}, {"metadata": {"sample_rate": 250}}, {"metadata": None},
    {"connection_id": ""}, {"source_id": None}, {"descriptor_error": []},
    {"descriptor": {"invalid": True}},
])
def test_invalid_worker_metadata_is_rejected(change):
    service, _, _, _, _, response = _metadata_service()
    response.update(change)
    with pytest.raises(RuntimeError, match="invalid acquisition"):
        service.get_metadata()
    assert service.cached_descriptor is None
    assert service._raw_metadata is None


def test_signal_metadata_must_match_the_raw_metadata_connection():
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"]["connection_id"] = "stale"
    with pytest.raises(RuntimeError, match="different source connection"):
        service.get_metadata()
    assert service.cached_descriptor is None


def test_connect_captures_metadata_for_later_processing_without_starting_stream():
    service, executor, provider, *_ = _metadata_service()
    service.handle_connect(ConnectDataProviderCommand())
    assert executor.request.call_args_list == [
        call(EventType.ConnectDataProvider, None),
        call(EventType.GetSourceMetadata, {"refresh": True}, timeout_s=5.0),
    ]
    provider.assert_called_once()
    assert service.cached_descriptor is not None
    assert not service.is_streaming


def test_metadata_comparison_uses_the_pinned_profile_without_replacing_its_expectations():
    from dataclasses import replace
    service, _, provider, binding, _, response = _metadata_service()
    changed = replace(binding, parameters={"sample_rate": 1000, "channel_count": 65}).to_dict()
    provider.return_value = changed
    response["metadata"] = '{"sample_rate": 1000, "channel_count": 65}'
    result = service.get_metadata()
    assert result["comparison"] == {
        "status": "mismatched",
        "sample_rate": {"expected": 1000, "actual": 250},
        "channel_count": {"expected": 65, "actual": 2},
    }
    result["comparison"]["sample_rate"]["expected"] = 250
    assert changed["parameters"] == {"sample_rate": 1000, "channel_count": 65}
    assert service._raw_metadata["comparison"]["sample_rate"]["expected"] == 1000


def test_scan45_declared_dimensions_are_not_presented_as_a_verified_match():
    service, _, _, _, _, response = _metadata_service()
    response["descriptor"]["provenance"] = "user-declared"
    assert service.get_metadata()["comparison"]["status"] == "unverified"


def _checkpoint(resource="input", revision="content-1", cursor=100):
    from omnibci.domain.ports.acquisition import SourceCheckpoint

    return SourceCheckpoint(resource_id=resource, resource_revision=revision, state={"cursor": cursor})


def test_same_resource_and_revision_restores_old_cursor_with_independent_copy():
    from omnibci.application.source_checkpoint import checkpoint_for_restore

    previous = _checkpoint(cursor=120)
    restored = checkpoint_for_restore(previous, _checkpoint(cursor=0))
    assert restored == previous
    restored.state["cursor"] = 0
    assert previous.state["cursor"] == 120


def test_different_resource_begins_new_timeline():
    from omnibci.application.source_checkpoint import checkpoint_for_restore

    assert checkpoint_for_restore(_checkpoint(), _checkpoint(resource="different-input")) is None


def test_changed_content_at_same_resource_is_not_silently_rewound():
    from omnibci.application.source_checkpoint import checkpoint_for_restore

    with pytest.raises(ValueError, match="changed"):
        checkpoint_for_restore(_checkpoint(), _checkpoint(revision="content-2"))


@pytest.mark.parametrize("previous,current", [
    ({"cursor": 120}, "current"), ("previous", {"cursor": 0}),
    ({"cursor": 120}, None),
])
def test_missing_identity_is_rejected(previous, current):
    from omnibci.application.source_checkpoint import checkpoint_for_restore

    prev_val = _checkpoint(cursor=120) if previous == "previous" else previous
    curr_val = _checkpoint(cursor=0) if current == "current" else current
    with pytest.raises(ValueError):
        checkpoint_for_restore(prev_val, curr_val)


def test_no_previous_checkpoint_needs_no_restore():
    from omnibci.application.source_checkpoint import checkpoint_for_restore

    assert checkpoint_for_restore(None, _checkpoint()) is None


@pytest.mark.parametrize("identity", ["input", {}, {"resource_id": "input", "revision": 1}])
def test_invalid_identity_fails_closed(identity):
    from omnibci.application.source_checkpoint import checkpoint_for_restore

    with pytest.raises(ValueError, match="SourceCheckpoint"):
        checkpoint_for_restore(_checkpoint(), {"source_identity": identity})


def _expectation_source_info(provenance="device-reported"):
    from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo

    return source_descriptor(
        SourceObservation(
            native_text="native",
            signal_info=SignalInfo(
                sample_rate=500,
                channels=(
                    ChannelDescriptor(0, "C3", channel_id="channel-0"),
                    ChannelDescriptor(1, "C4", channel_id="channel-1"),
                ),
            ),
            native_event="final-row",
            provenance=provenance,
        ),
        source_id="source",
        connection_id="connection",
    )


def _expectation_source_binding(**parameters):
    return {"slot": "acquisition.source", "parameters": parameters}


@pytest.mark.parametrize("provenance", ["device-reported", "file-reported"])
def test_matching_expectations_include_the_native_event_row(provenance):
    from omnibci.application.source_expectations import compare_source_expectations, validate_source_expectations

    binding = _expectation_source_binding(sample_rate=500, channel_count=3)
    original = deepcopy(binding)
    info = _expectation_source_info(provenance)
    assert compare_source_expectations(binding, info) == {
        "status": "matched",
        "sample_rate": {"expected": 500, "actual": 500},
        "channel_count": {"expected": 3, "actual": 3},
    }
    validate_source_expectations({"PLUGINS.bindings": [binding]}, info)
    assert binding == original


@pytest.mark.parametrize("parameters", [{}, {"sample_rate": 500}, {"channel_count": 3}])
def test_unspecified_expectations_use_observed_values_without_changing_the_profile(parameters):
    from omnibci.application.source_expectations import compare_source_expectations, validate_source_expectations

    binding = _expectation_source_binding(**parameters)
    original = deepcopy(binding)
    comparison = compare_source_expectations(binding, _expectation_source_info())
    assert comparison["status"] == "unconfirmed"
    validate_source_expectations({"PLUGINS.bindings": [binding]}, _expectation_source_info())
    assert binding == original


def test_flat_settings_and_other_plugins_do_not_override_observed_source_values():
    from omnibci.application.source_expectations import compare_source_expectations, validate_source_expectations

    snapshot = {
        "ACQUISITION.sample_rate": 1000, "ACQUISITION.channel_count": 65,
        "PLUGINS.bindings": [{"slot": "processing.decoder", "parameters": {
            "sample_rate": 500, "channel_count": 3,
        }}],
    }
    assert compare_source_expectations(None, _expectation_source_info())["status"] == "unconfirmed"
    validate_source_expectations(snapshot, _expectation_source_info())


@pytest.mark.parametrize(("parameters", "difference"), [
    ({"sample_rate": 250, "channel_count": 3}, "sample rate \\(Hz\\): expected 250, observed 500"),
    ({"sample_rate": 500, "channel_count": 2}, "output rows \\(including event row\\): expected 2, observed 3"),
    ({"sample_rate": 250}, "sample rate \\(Hz\\): expected 250, observed 500"),
    ({"channel_count": 2}, "output rows \\(including event row\\): expected 2, observed 3"),
])
def test_mismatch_reports_expected_and_observed_values_without_modifying_them(parameters, difference):
    from omnibci.application.source_expectations import compare_source_expectations, validate_source_expectations

    binding = _expectation_source_binding(**parameters)
    info = _expectation_source_info()
    original = deepcopy(binding)
    assert compare_source_expectations(binding, info)["status"] == "mismatched"
    with pytest.raises(ValueError, match=difference + ".*Adjust the source.*save and apply") as error:
        validate_source_expectations({"PLUGINS.bindings": [binding]}, info)
    assert "expected None" not in str(error.value)
    assert binding == original
    assert info["sample_rate"] == 500 and info["output_row_count"] == 3


@pytest.mark.parametrize("provenance", ["user-declared", "protocol-defined"])
@pytest.mark.parametrize("parameters", [{}, {"sample_rate": 500, "channel_count": 3},
                                        {"sample_rate": 1000, "channel_count": 10}])
def test_declared_information_cannot_independently_verify_a_profile(provenance, parameters):
    from omnibci.application.source_expectations import compare_source_expectations, validate_source_expectations

    binding = _expectation_source_binding(**parameters)
    info = _expectation_source_info(provenance)
    assert compare_source_expectations(binding, info)["status"] == "unverified"
    validate_source_expectations({"PLUGINS.bindings": [binding]}, info)
