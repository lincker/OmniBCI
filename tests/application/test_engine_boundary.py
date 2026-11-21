"""Application orchestration works through operations without graph access."""
from threading import Event as ThreadEvent
from unittest.mock import Mock

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.results_service import ResultsService
from omnibci.shared.events import Event, EventType
from tests.application.test_runtime_snapshot import _graph, thread_factory


class OperationOnlyGraph:
    __slots__ = ('started', 'stopped', 'shutdown_complete', 'subscriptions', 'events')

    def __init__(self):
        self.started = ThreadEvent()
        self.stopped = ThreadEvent()
        self.shutdown_complete = False
        self.subscriptions = []
        self.events = []

    def run(self):
        self.started.set()
        self.stopped.wait(5)
        self.shutdown_complete = True

    def wait_until_started(self, timeout):
        assert self.started.wait(timeout)

    def request_stop(self):
        self.stopped.set()

    def subscribe(self, event_type, handler):
        self.subscriptions.append((event_type, handler))

    def dispatch(self, event):
        self.events.append(event)

    def is_peer_connected(self, peer):
        return peer == 'stimulation'

    def is_current_peer_channel(self, peer, channel):
        return peer == 'stimulation' and channel == 'current'

    def is_module_running(self, module):
        return False

    async def set_actuation_active(self, active):
        pass


@pytest.mark.asyncio
async def test_empty_foundation_passes_one_explicit_detached_snapshot():
    graph = OperationOnlyGraph()
    inputs = []
    def build(snapshot):
        inputs.append(dict(snapshot))
        snapshot['factory-only'] = True
        return graph
    service = NeuroEngineService(build)
    await service.startup()
    try:
        await service.handle_start_engine(StartEngineCommand())
        assert len(inputs) == 1 and inputs[0]['CONFIG.generation'] == service.generation
        assert inputs[0]['PLUGINS.bindings'] == []
        assert 'factory-only' not in service.applied_snapshot
        await service.handle_stop_engine(StopEngineCommand())
        assert graph.shutdown_complete
    finally:
        graph.stopped.set()
        if service._engine_thread is not None:
            service._engine_thread.join(1)


@pytest.mark.asyncio
async def test_subscriptions_dispatch_and_peer_faults_use_only_declared_operations():
    graph = OperationOnlyGraph()
    service = NeuroEngineService(lambda snapshot: graph, snapshot_provider=lambda: {'PLUGINS.bindings': []})
    listener = Mock()
    await service.startup()
    service.subscribe(EventType.TrialCompleted, listener)
    try:
        await service.handle_start_engine(StartEngineCommand())
        assert graph.subscriptions == [(EventType.TrialCompleted, listener)]
        command = Event(EventType.StartStimTrial, {"n_trials": 1, "trial_params": [{"target_id": 1}]})
        service.dispatch(command)
        assert len(graph.events) == 1 and graph.events[0].type == command.type
        assert graph.events[0].data == command.data
        command.data["trial_params"][0]["target_id"] = 2
        assert graph.events[0].data["trial_params"] == [{"target_id": 1}]
        assert service.is_peer_connected('stimulation')
        for channel in ('old', 'current'):
            service.on_peer_delivery_fault(Event(EventType.PeerDeliveryFault, {
                'peer': 'stimulation', 'channel_id': channel, 'reason': 'lost delivery',
            }))
            assert service.admission.requires_recovery('stimulation') is (channel == 'current')
        await service.handle_stop_engine(StopEngineCommand())
        assert graph.shutdown_complete
    finally:
        graph.stopped.set()
        if service._engine_thread is not None:
            service._engine_thread.join(1)


@pytest.mark.asyncio
async def test_unknown_shutdown_confirmation_blocks_replacement(thread_factory):
    graph = _graph()
    graph.shutdown_complete = None
    service = NeuroEngineService(lambda snapshot: graph, snapshot_provider=lambda: {'PLUGINS.bindings': []})
    await service.startup()
    await service.handle_start_engine(StartEngineCommand())
    with pytest.raises(RuntimeError, match='resources'):
        await service.handle_stop_engine(StopEngineCommand())
    assert service.restart_blocked
    with pytest.raises(RuntimeError, match='recovery'):
        await service.handle_start_engine(StartEngineCommand())


def test_results_use_only_a_generation_provider_and_reset_on_change():
    generation = Mock(return_value='first')
    service = ResultsService(generation)
    assert service.snapshot() == {'generation': 'first', 'trials': []}
    generation.return_value = 'second'
    assert service.epochs()['generation'] == 'second'
    assert generation.call_count == 2
