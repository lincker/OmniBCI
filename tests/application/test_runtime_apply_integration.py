"""Exercise live pause/rebuild/restore and rollback with real spawned workers."""
from tests.plugin_configuration_helpers import section_settings
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnibci.application.commands.acquisition_commands import ConnectDataProviderCommand, StartStreamCommand
from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.bootstrap import _acquisition_binding, assemble, register_engine_listeners
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.accepted_configuration import accepted_runtime_settings
from tests.plugin_configuration_helpers import accept_profiles, builtin_selection
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository


SOURCE = '''
import asyncio
import json
from pathlib import Path
import numpy as np
from omnibci.sdk import SignalSource
from omnibci.domain.ports.acquisition import SourceObservation, SourceCheckpoint
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
class Replay(SignalSource):
    trigger_mode = "events"
    resume_mode = "checkpoint"
    def __init__(self, *, parameters, resource_root):
        super().__init__()
        self.size = parameters['size']
        self.sample_rate = parameters['sample_rate']
        if self.size == 0:
            raise ValueError('candidate construction failed')
        self.path = parameters['log']
        self.cursor = 0
        self.restored = None
    async def connect(self): pass
    async def disconnect(self): pass
    async def start_stream(self):
        self.cursor = self.restored if self.restored is not None else 0
        self.restored = None
    async def stop_stream(self): pass
    async def read_chunk(self):
        await asyncio.sleep(.01)
        self.cursor += self.size
        return np.zeros((2,self.size))
    async def observe(self):
        return SourceObservation(native_text='Test recording', signal_info=SignalInfo(self.sample_rate,
            (ChannelDescriptor(0, 'CH1', 'eeg', 'V', channel_id='channel-0'),)),
            native_event='final-row', provenance='protocol-defined')
    def checkpoint(self):
        return SourceCheckpoint(resource_id='test-recording', resource_revision='immutable', state={'cursor':self.cursor})
    def restore_checkpoint(self, checkpoint):
        self.restored = checkpoint.state['cursor']
        with Path(self.path).open('a') as output:
            output.write(json.dumps(checkpoint.state) + '\\n')
'''


def configured_profiles(tmp_path, monkeypatch, *, actuation_port=8001):
    root = tmp_path / 'plugins'
    plugin = root / 'acquisition' / 'replay'
    plugin.mkdir(parents=True)
    monkeypatch.setenv('OMNIBCI_PLUGIN_DIR', str(root))
    (plugin / 'manifest.toml').write_text('''manifest_version=2
[package]
id="replay"
domain="acquisition"
publisher="test"
version="1.0.0"
api_version="1"
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
                       'channel_count':{'type':'integer','default':2}},
        'required':['size','log','sample_rate','channel_count'],
    }))
    host = {'STIMULATION.auto_spawn':True,
            'ACTUATION.server_port':actuation_port, 'ALGORITHM.baseline_duration':0.0}
    profiles = ConfigurationService(TomlConfigurationRepository(tmp_path / 'profiles'),
                                    CatalogConfigurationAdapter())
    accepted = accept_profiles(profiles, [
        ('acquisition.source', 'acquisition', 'test_replay', {}),
        ('processing.decoder', 'decoder', 'THUBCI_SSVEP_classic40CCA', {}),
        ('processing.epoch_detector', 'epoch_detector', 'DEFAULT_EPOCH_DETECTOR', {}),
        ('stimulation.paradigm', 'paradigm', 'STIMULATION_THUBCI_SSVEP_classic40', {}),
    ], module_settings={
        "processing": section_settings(host, "processing"),
        "stimulation": section_settings(host, "stimulation"),
        "actuation": section_settings(host, "actuation"),
    })
    selections = [{"slot": item.slot, "profile_id": item.profile_id} for item in accepted.bindings()]
    return profiles, selections


def wire_signal_preparation(profiles, engine, container):
    container.acquisition_service.set_metadata_binding(lambda: _acquisition_binding(engine))
    service = SignalSetupService(
        profiles, engine,
        container.acquisition_service, container.command_results,
    )
    container.acquisition_service.set_signal_preparer(service.prepare)
    from omnibci.application.configuration.models import MODULE_KINDS
    engine.configure_modules(container.acquisition_service, container.command_results, service,
                             lambda module: accepted_runtime_settings(profiles=profiles, active_kinds=MODULE_KINDS[module]))

    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("processing_first", [False, True])
async def test_current_actions_connect_and_start_processing_with_real_workers(tmp_path, monkeypatch, processing_first):
    profiles, _ = configured_profiles(tmp_path, monkeypatch)
    engine = NeuroEngineService(lambda snapshot: NeuroEngineBuilder(settings_snapshot=snapshot).build())
    await engine.startup()
    container = assemble(engine, admission=engine.admission)
    signal = wire_signal_preparation(profiles, engine, container)
    register_engine_listeners(container)
    coordinator = RuntimeApplyCoordinator(engine, profiles, container.acquisition_service,
                                          container.stimulation_service, container.command_results,
                                          signal_preparer=signal.prepare, signal_detacher=signal.detach)
    runtime = RuntimeConfigurationService(profiles, engine, executor=coordinator)

    def current(module):
        value = profiles.repository.get_module(module)
        bindings = []
        for binding in value.bindings:
            item = {"slot": binding.slot, "selection": binding.selection.to_dict(),
                    "parameters": binding.parameters, "configuration_version": binding.configuration_version}
            if binding.processing_input is not None:
                item["processing_input"] = binding.processing_input.to_dict()
            bindings.append(item)
        return {"expected_revision": value.revision, "settings": value.settings, "bindings": bindings}

    try:
        engine.admission.quarantine("External output stop is unconfirmed", "stimulation")
        engine._recovery_snapshots["stimulation"] = {
            "STIMULATION.auto_spawn": False,
            "CONFIG.module_revisions": {"stimulation": "unconfirmed"},
            "PLUGINS.bindings": [{"slot": "stimulation.paradigm", "plugin_id": "retained.external"}],
        }
        assert engine.runtime_package_snapshots
        assert not engine.foundation_requires_cleanup
        source = current("acquisition")
        source["bindings"][0]["parameters"]["size"] = 7
        if processing_first:
            accepted_source = profiles.repository.get_module("acquisition")
            profiles.repository.remove_module("acquisition", expected_revision=accepted_source.revision)
            source["expected_revision"] = None
            standby = await runtime.execute_current("processing", "start", operation_id=str(uuid4()), configuration=current("processing"))
            assert standby["status"] == "succeeded", standby
            assert engine.is_module_running("processing") and not engine.is_module_running("acquisition")
        connected = await runtime.execute_current("acquisition", "connect", operation_id=str(uuid4()), configuration=source)
        assert connected["status"] == "succeeded", connected
        assert connected["result"]["action"] == "connected"
        assert engine.is_engine_ready and engine.is_module_running("acquisition")
        assert engine.is_module_running("processing") is processing_first
        assert engine.admission.recovery_status == {"stimulation": "External output stop is unconfirmed"}
        assert not engine.is_peer_connected("stimulation")
        accepted = profiles.repository.get_module("acquisition")
        assert accepted.bindings[0].parameters["size"] == 7
        generation = engine.generation

        processing = current("processing")
        processing["settings"]["ALGORITHM.baseline_duration"] = 0.25
        started = await runtime.execute_current("processing", "start", operation_id=str(uuid4()), configuration=processing)
        assert started["status"] == "succeeded", started
        assert started["result"]["action"] == "started"
        assert engine.generation == generation
        assert engine.applied_snapshot["ALGORITHM.baseline_duration"] == 0.25
        status = container.acquisition_service.handle_get_status(None)
        await engine.start_reading(expected_revision=accepted.revision, generation=generation,
                                   connection_id=status.connection_id)
        assert container.acquisition_service.is_streaming
        assert engine._engine.data_manager.processing_active.is_set()
    finally:
        await engine.handle_stop_engine(StopEngineCommand())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_source_parameter_change_resumes_checkpoint_and_failed_candidate_rolls_back(tmp_path, monkeypatch, unused_tcp_port):
    profiles, bindings = configured_profiles(tmp_path, monkeypatch, actuation_port=unused_tcp_port)
    engine = NeuroEngineService(lambda snapshot: NeuroEngineBuilder(settings_snapshot=snapshot).build(),
                                snapshot_provider=lambda: accepted_runtime_settings(profiles=profiles))
    await engine.startup()
    container = assemble(engine, admission=engine.admission)
    signal = wire_signal_preparation(profiles, engine, container)
    register_engine_listeners(container)
    coordinator = RuntimeApplyCoordinator(engine, profiles, container.acquisition_service,
                                          container.stimulation_service,
                                          container.command_results, signal_preparer=signal.prepare,
                                          signal_detacher=signal.detach)
    runtime = RuntimeConfigurationService(profiles, engine, executor=coordinator)
    try:
        await engine.handle_start_engine(StartEngineCommand())
        assert engine.is_engine_ready
        assert not engine.is_module_running("acquisition")
        assert not engine.is_module_running("processing")
        processing = profiles.repository.get_module("processing")
        started = await runtime.execute_current("processing", "start", operation_id=str(uuid4()), configuration={
            "expected_revision": processing.revision, "settings": processing.settings, "bindings": bindings[1:3],
        })
        assert started["status"] == "succeeded", started
        async with engine.runtime_transition("acquisition"):
            await engine.start_module_for_configuration("acquisition", engine.applied_snapshot)
        await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
        await asyncio.to_thread(container.acquisition_service.handle_start_stream, StartStreamCommand())
        await asyncio.sleep(.08)
        previous_generation = engine.generation
        source = profiles.repository.get_profile(bindings[0]['profile_id'])
        profiles.update(source.profile_id, name=source.name, parameters={**source.parameters, 'size':7},
                        expected_revision=source.revision)
        before = profiles.repository.get_module("acquisition")
        plan = runtime.plan("acquisition", [bindings[0]], expected_revision=before.revision, settings=runtime.profiles.default_module_settings("acquisition"))
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'succeeded', result
        assert result['result']['replay_resumed'] is True
        assert engine.generation == previous_generation
        assert container.acquisition_service.is_streaming
        assert profiles.repository.get_module("acquisition").revision == engine.applied_revisions["acquisition"] == plan.target_revisions["acquisition"]
        checkpoints = [json.loads(line) for line in (tmp_path / 'restored.jsonl').read_text().splitlines()]
        assert checkpoints[0]['cursor'] > 0

        # A changed decoder input replaces Processing while retaining the source.
        decoder = profiles.repository.get_profile(bindings[1]['profile_id'])
        profiles.update(decoder.profile_id, name=decoder.name, parameters=decoder.parameters,
                        processing_input=ProcessingInput(outputs=(ChannelBinding("CH1", "channel-0"),)), expected_revision=decoder.revision)
        previous_generation = engine.generation
        plan = runtime.plan("processing", bindings[1:3], expected_revision=engine.applied_revisions["processing"], settings=runtime.profiles.repository.get_module("processing").settings)
        assert plan.strategy == 'rebuild'
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'succeeded', result
        assert engine.generation == previous_generation
        assert container.acquisition_service.is_streaming
        assert signal.read()['active_input']['outputs'] == [{'source_channel_id': 'channel-0', 'name': 'CH1'}]
        assert signal.read()['readiness']['ready'] is True

        # Changing only a filter must replace the detached input identity even
        # though its source and channel mapping are unchanged.
        previous_input = signal.read()['readiness']['receipt']['input_id']
        source_pid = engine._engine.acquisition_worker.pid
        baseline = profiles.create('filter', builtin_selection('filter', 'BASELINE_CORRECTION'), 'Baseline', {})
        processing_bindings = bindings[1:3] + [{'slot': f'processing.filter.{uuid4()}', 'profile_id': baseline.profile_id}]
        plan = runtime.plan('processing', processing_bindings, expected_revision=engine.applied_revisions['processing'], settings=runtime.profiles.repository.get_module('processing').settings)
        assert plan.strategy == 'module'
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'succeeded', result
        assert signal.read()['readiness']['receipt']['input_id'] != previous_input
        assert engine._engine._processing_active.is_set()
        assert engine._engine.acquisition_worker.pid == source_pid
        assert container.acquisition_service.is_streaming

        # A dormant output selection still updates the pinned runtime context;
        # acquisition and processing processes remain alive.
        generation = engine.generation
        worker_ids = (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid)
        effector = profiles.create('effector', builtin_selection('effector', 'logger'), 'Logger', {})
        bindings.append({'slot':'actuation.effector', 'profile_id':effector.profile_id})
        plan = runtime.plan("actuation", [bindings[-1]], expected_revision=None, settings=runtime.profiles.default_module_settings("actuation"))
        assert plan.strategy == 'module'
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'succeeded', result
        assert engine.generation == generation
        assert container.acquisition_service.is_streaming
        assert (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid) == worker_ids

        decoder = profiles.repository.get_profile(bindings[1]['profile_id'])
        profiles.update(decoder.profile_id, name=decoder.name,
                        parameters={**decoder.parameters, 'window_duration':2.5}, expected_revision=decoder.revision)
        plan = runtime.plan("processing", processing_bindings, expected_revision=engine.applied_revisions["processing"], settings=runtime.profiles.repository.get_module("processing").settings)
        assert plan.strategy == 'trial_boundary'
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'succeeded', result
        assert result['result']['boundary'] == 'trial'
        assert engine.generation == generation
        assert container.acquisition_service.is_streaming
        assert (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid) == worker_ids

        # Public module controls preserve the source while switching between
        # source-only reading and processing in the same foundation instance.
        source_pid = engine._engine.acquisition_worker.pid
        await engine.stop_module("processing")
        assert engine.is_engine_ready and not engine.is_module_running("processing")
        assert container.acquisition_service.is_streaming
        await asyncio.sleep(.03)
        processing = profiles.repository.get_module("processing")
        restarted = await runtime.execute_current("processing", "start", operation_id=str(uuid4()), configuration={
            "expected_revision": processing.revision, "settings": processing.settings,
            "bindings": processing_bindings,
        })
        assert restarted["status"] == "succeeded", restarted
        assert engine.is_module_running("processing")
        assert engine._engine.data_manager.processing_active.is_set()
        assert engine._engine.acquisition_worker.pid == source_pid
        assert engine.generation == generation
        assert container.acquisition_service.is_streaming

        # Candidate plugin constructor fails after old data is safely drained.
        # Recovery restores only Acquisition from its accepted inputs and cursor.
        await asyncio.sleep(.05)
        accepted = profiles.repository.get_module("acquisition")
        source = profiles.repository.get_profile(source.profile_id)
        profiles.update(source.profile_id, name=source.name, parameters={**source.parameters, 'size':0},
                        expected_revision=source.revision)
        failed = runtime.plan("acquisition", [bindings[0]], expected_revision=accepted.revision, settings=runtime.profiles.default_module_settings("acquisition"))
        operation = runtime.submit(failed.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation['operation_id'])
        assert result['status'] == 'failed', result
        assert result['rolled_back'] is True, result
        assert engine.is_engine_ready
        assert engine.applied_revisions["acquisition"] == accepted.revision
        assert profiles.repository.get_module("acquisition").revision == accepted.revision
        assert container.acquisition_service.is_streaming
        checkpoints = [json.loads(line) for line in (tmp_path / 'restored.jsonl').read_text().splitlines()]
        assert checkpoints[-1]['cursor'] >= checkpoints[0]['cursor']
    finally:
        await runtime.wait_for_idle()
        await engine.handle_stop_engine(StopEngineCommand())
