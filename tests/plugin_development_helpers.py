"""Executable, hardware-free source fixtures for the seven API v1 kinds."""
import json

from omnibci.application.configuration.definitions import MODULE_SETTING_DEFAULTS
from tests.infrastructure.test_plugin_contributions import _contribution, _write_package


KINDS = {
    "acquisition": "Source", "filter": "Gain", "decoder": "Model", "epoch_detector": "Detector",
    "paradigm": "Stimulus", "trigger": "Trigger", "effector": "Output",
}
SLOTS = {
    "acquisition": "acquisition.source", "decoder": "processing.decoder",
    "epoch_detector": "processing.epoch_detector", "filter": "processing.filter.00000000-0000-0000-0000-000000000001",
    "paradigm": "stimulation.paradigm", "trigger": "stimulation.trigger", "effector": "actuation.effector",
}


def source_project(root, *, publisher="test"):
    project = _write_package(root, publisher=publisher, contributions=[
        _contribution(kind, kind, entrypoint=f"driver:{name}", configuration={
            "schema": "parameters.schema.json", "schema_version": 1,
        }) for kind, name in KINDS.items()
    ])
    (project / "parameters.schema.json").write_text(json.dumps({
        "type": "object", "properties": {}, "additionalProperties": False,
    }))
    (project / "value.txt").write_text("first")
    (project / "driver.py").write_text('''from omnibci.sdk import (SignalSource, SourceObservation, SignalInfo, ChannelDescriptor,
    Filter, Decoder, Prediction, EpochDetector, Paradigm, TriggerEmitter, Effector)

class Base:
    def __init__(self, *, parameters, resource_root):
        self.resource_root = resource_root
        self.value = (resource_root / "value.txt").read_text()

class Input(Base):
    def __init__(self, *, parameters, resource_root, input_signal):
        super().__init__(parameters=parameters, resource_root=resource_root)
        self.input_signal = input_signal

class Source(Base, SignalSource):
    trigger_mode = "events"
    resume_mode = "restart-only"
    async def connect(self): pass
    async def observe(self):
        return SourceObservation(native_text="fixture", native_event="observe", provenance="protocol-defined",
            signal_info=SignalInfo(sample_rate=250, channels=(ChannelDescriptor(0, "Oz", channel_id="oz", signal_type="eeg"),)))
    async def start_stream(self): pass
    async def read_chunk(self): return None
    async def stop_stream(self): pass
    async def disconnect(self): pass

class Gain(Input, Filter):
    def apply(self, data, *, pre_trigger_data): return data.copy()

class Model(Input, Decoder):
    def setup(self): pass
    def begin_epoch(self): pass
    def should_decode(self, raw_window): return True
    def decode(self, filtered_window, *, latest_raw_segment): return Prediction(0)
    def teardown(self): pass

class Detector(Input, EpochDetector):
    def detect(self, raw_data): return ()

class Stimulus(Base, Paradigm):
    def setup(self, *, renderer, trigger): pass
    def teardown(self): pass
    def present_trial(self, parameters): pass
    def present_result(self, result): pass
    def render_frame(self, frame): return False
    def is_presenting(self): return False

class Trigger(Base, TriggerEmitter):
    def setup(self): pass
    def send(self, event_id): pass
    def clear(self): pass
    def teardown(self): pass

class Output(Base, Effector):
    def setup(self): pass
    def execute(self, command): pass
    def quiesce(self): pass
    def teardown(self): pass
''')
    return project


def module_configuration(kinds, *, publisher="test", settings=None):
    modules = {}
    for kind in kinds:
        slot = SLOTS[kind]
        module = slot.split(".")[0]
        configuration = modules.setdefault(module, {
            "expected_revision": None, "settings": dict(MODULE_SETTING_DEFAULTS[module]), "bindings": [],
        })
        configuration["settings"].update((settings or {}).get(module, {}))
        binding = {"slot": slot, "selection": {
            "package_id": f"{publisher}.device", "contribution_id": kind, "version": "1.0.0",
        }, "parameters": {}}
        if kind == "decoder":
            binding["processing_input"] = {"schema_version": 2, "outputs": []}
        configuration["bindings"].append(binding)
    return modules


def configuration_file(root, kinds, **kwargs):
    path = root / "development.json"
    path.write_text(json.dumps(module_configuration(kinds, **kwargs)))
    return path
