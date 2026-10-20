"""Exercise live pause/rebuild/restore and rollback with real spawned workers."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnibci.application.commands.acquisition_commands import ConnectDataProviderCommand
from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.bootstrap import _acquisition_binding, assemble, register_engine_listeners
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository


SOURCE = '''
import asyncio
import json
from pathlib import Path
import numpy as np
from omnibci.sdk import SignalSource
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
class Replay(SignalSource):
    def __init__(self, *, parameters, context):
        super().__init__()
        self.size = parameters['size']
        self.sample_rate = parameters['sample_rate']
        if self.size == 0:
            raise ValueError('candidate construction failed')
        self.path = parameters['log']
        self.cursor = 0
        self.restored = None
    async def connect(self): self._is_connected = True; return True
    async def disconnect(self): self._is_connected = False; return True
    async def start_stream(self):
        self.cursor = self.restored if self.restored is not None else 0
        self.restored = None
        self._is_streaming = True
        return True
    async def stop_stream(self): self._is_streaming = False; return True
    async def read_chunk(self):
        await asyncio.sleep(.01)
        self.cursor += self.size
        return np.zeros((2,self.size))
    async def get_metadata(self) -> str:
        return 'Test recording'
    def describe_output(self, *, source_id, connection_id):
        return SignalInfo(source_id, self.sample_rate,
            (ChannelDescriptor(0, 'CH1', 'eeg', 'V'),), 1,
            connection_id=connection_id, provenance='protocol-defined')
    @property
    def supports_checkpoint(self): return True
    def checkpoint(self):
        return {'source_identity': {'resource_id':'test-recording', 'revision':'immutable'}, 'cursor':self.cursor}
    def restore_checkpoint(self, checkpoint):
        self.restored = checkpoint['cursor']
        with Path(self.path).open('a') as output:
            output.write(json.dumps(checkpoint) + '\\n')
'''


def configured_profiles(tmp_path, monkeypatch):
    root = tmp_path / 'plugins'
    plugin = root / 'acquisition' / 'replay'
    plugin.mkdir(parents=True)
    monkeypatch.setenv('OMNIBCI_PLUGIN_DIR', str(root))
    (plugin / 'manifest.toml').write_text('''manifest_version=2
[package]
id="replay"
publisher="test"
version="1.0.0"
api_version="2"
[[contributions]]
id="default"
kind="acquisition"
plugin_type="test_replay"
entrypoint="source:Replay"
[contributions.configuration]
schema="config.schema.json"
schema_version=1
''')
    (plugin / 'source.py').write_text(SOURCE)
    (plugin / 'config.schema.json').write_text(json.dumps({
        'type':'object', 'additionalProperties':False,
        'properties': {'size':{'type':'integer','default':5,'minimum':0,'x-apply':'module'},
                       'log':{'type':'string','default':str(tmp_path / 'restored.jsonl')},
                       'sample_rate':{'type':'integer','default':100},
                       'channel_number':{'type':'integer','default':2}},
        'required':['size','log','sample_rate','channel_number'],
    }))
    host = {'NEUROENGINE.message_server_port':0, 'STIMULATION.auto_spawn':True,
            'ACTUATION.enabled':False, 'ALGORITHM.baseline_duration':0.0}
    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / 'profiles'),
                                    CatalogConfigurationAdapter(), settings_source=lambda: host)
    selections = []
    for slot, kind, alias in (
        ('acquisition.source','acquisition','test_replay'),
        ('processing.decoder','decoder','THUBCI_SSVEP_classic40CCA'),
        ('processing.epoch_detector','epoch_detector','DEFAULT_EPOCH_DETECTOR'),
        ('stimulation.paradigm','paradigm','STIMULATION_THUBCI_SSVEP_classic40'),
    ):
        profile = profiles.create(kind, profiles.catalog.legacy(kind,alias).selection, alias, {})
        selections.append({'slot':slot, 'profile_id':profile.profile_id})
    profiles.accept(profiles.propose(selections), expected_revision=None)
    return profiles, selections


def wire_signal_preparation(profiles, engine, container):
    container.acquisition_service.set_metadata_binding(lambda: _acquisition_binding(engine))
    service = SignalSetupService(
        SimpleNamespace(read_saved=lambda: (profiles.settings_source(), "saved")), profiles, engine,
        container.acquisition_service, container.command_results,
    )
    container.acquisition_service.set_signal_preparer(service.prepare)

    def prepare_session():
        plan = service.prepare_session()
        engine.prepare_actuation()
        return plan

    container.session_service.set_signal_preparer(prepare_session, container.acquisition_service.lifecycle_lock)
    return service


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_source_parameter_change_resumes_checkpoint_and_failed_candidate_rolls_back(tmp_path, monkeypatch):
    profiles, bindings = configured_profiles(tmp_path, monkeypatch)
    engine = NeuroEngineService(lambda snapshot: NeuroEngineBuilder(settings_snapshot=snapshot).build(),
                                snapshot_provider=profiles.resolve_settings)
    await engine.startup()
    container = assemble(engine, InMemorySessionRepository(), admission=engine.admission)
    signal = wire_signal_preparation(profiles, engine, container)
    register_engine_listeners(container)
    coordinator = RuntimeApplyCoordinator(engine, profiles, container.acquisition_service,
                                          container.session_service, container.stimulation_service,
                                          container.command_results, signal_preparer=signal.prepare)
    runtime = RuntimeConfigurationService(profiles, engine, executor=coordinator)
    try:
        await engine.handle_start_engine(StartEngineCommand())
        await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
        original_session = await asyncio.to_thread(container.recording_service.start)
        await asyncio.sleep(.08)
        previous_generation = engine.generation
        source = profiles.repository.get_profile(bindings[0]['profile_id'])
        profiles.update(source.profile_id, name=source.name, parameters={**source.parameters, 'size':7},
                        expected_revision=source.revision)
        before = profiles.repository.get_activation()
        plan = runtime.plan(bindings, expected_revision=before.revision)
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'succeeded', result
        assert result['result']['replay_resumed'] is True
        assert engine.generation != previous_generation
        continuation = container.session_service.active_session_id
        assert continuation != original_session
        assert container.session_repository.find_by_id(continuation).continuation_of == original_session
        assert profiles.repository.get_activation().revision == engine.applied_revision == plan.target_revision
        checkpoints = [json.loads(line) for line in (tmp_path / 'restored.jsonl').read_text().splitlines()]
        assert checkpoints[0]['cursor'] > 0

        # A changed decoder input prepares a new graph and records its own plan.
        decoder = profiles.repository.get_profile(bindings[1]['profile_id'])
        profiles.update(decoder.profile_id, name=decoder.name, parameters=decoder.parameters,
                        processing_input=ProcessingInput(outputs=(ChannelBinding("CH1", "channel-0"),)), expected_revision=decoder.revision)
        previous_generation, previous_session = engine.generation, continuation
        plan = runtime.plan(bindings, expected_revision=engine.applied_revision)
        assert plan.strategy == 'rebuild'
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'succeeded', result
        assert engine.generation != previous_generation
        continuation = container.session_service.active_session_id
        continued = container.session_repository.find_by_id(continuation)
        assert continued.continuation_of == previous_session
        assert continued.signal_plan['processing_input']['outputs'] == [{'source_channel_id': 'channel-0', 'name': 'CH1'}]
        assert container.session_repository.find_by_id(previous_session).signal_plan['processing_input']['outputs'] == []
        assert signal.read()['readiness']['ready'] is True

        # A dormant output selection still updates the pinned runtime context;
        # acquisition/processing processes and the recording identity survive.
        generation = engine.generation
        worker_ids = (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid)
        effector = profiles.create('effector', profiles.catalog.legacy('effector', 'logger').selection, 'Logger', {})
        bindings.append({'slot':'actuation.effector', 'profile_id':effector.profile_id})
        plan = runtime.plan(bindings, expected_revision=engine.applied_revision)
        assert plan.strategy == 'module'
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'succeeded', result
        assert result['result']['session_preserved'] is True
        assert engine.generation == generation
        assert container.session_service.active_session_id == continuation
        assert (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid) == worker_ids

        decoder = profiles.repository.get_profile(bindings[1]['profile_id'])
        profiles.update(decoder.profile_id, name=decoder.name,
                        parameters={**decoder.parameters, 'window_duration':2.5}, expected_revision=decoder.revision)
        plan = runtime.plan(bindings, expected_revision=engine.applied_revision)
        assert plan.strategy == 'trial_boundary'
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'succeeded', result
        assert result['result']['boundary'] == 'trial'
        assert engine.generation == generation
        assert container.session_service.active_session_id == continuation
        assert (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid) == worker_ids

        # Candidate plugin constructor fails after old data is safely drained.
        # Recovery starts a fresh graph from the old accepted inputs and cursor.
        await asyncio.sleep(.05)
        accepted = profiles.repository.get_activation()
        source = profiles.repository.get_profile(source.profile_id)
        profiles.update(source.profile_id, name=source.name, parameters={**source.parameters, 'size':0},
                        expected_revision=source.revision)
        failed = runtime.plan(bindings, expected_revision=accepted.revision)
        operation = runtime.submit(failed.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'failed', result
        assert result['rolled_back'] is True, result
        assert engine.is_engine_ready
        assert engine.applied_revision == accepted.revision
        assert profiles.repository.get_activation().revision == accepted.revision
        assert container.session_service.active_session_id not in (continuation, original_session)
        checkpoints = [json.loads(line) for line in (tmp_path / 'restored.jsonl').read_text().splitlines()]
        assert checkpoints[-1]['cursor'] >= checkpoints[0]['cursor']
    finally:
        await runtime.wait_for_idle()
        await engine.handle_stop_engine(StopEngineCommand())
