"""The public plugin boundary shares types and stays safe across spawn."""
from __future__ import annotations

import importlib
import multiprocessing as mp
import pickle
import subprocess
import sys
from dataclasses import FrozenInstanceError
from textwrap import dedent

import pytest


@pytest.mark.parametrize(("name", "module"), [
    ("SignalSource", "acquisition"), ("TriggerEmitter", "acquisition"),
    ("Filter", "algorithm"), ("FilterContext", "algorithm"),
    ("Decoder", "algorithm"), ("Prediction", "algorithm"),
    ("EpochDetector", "algorithm"), ("Paradigm", "paradigm"),
    ("Effector", "effector"), ("Renderer", "renderer"),
    ("TextureHandle", "renderer"), ("Rect", "renderer"), ("Color", "renderer"),
])
def test_sdk_reexports_existing_contract_types(name, module):
    from omnibci import sdk

    domain_module = importlib.import_module(f"omnibci.domain.ports.{module}")
    assert getattr(sdk, name) is getattr(domain_module, name)
    if module == "renderer":
        from omnibci.infrastructure.stimulation.ports import renderer
        assert getattr(sdk, name) is getattr(renderer, name)


def test_sdk_reexports_signal_chunk():
    from omnibci import sdk
    from omnibci.domain.signal import SignalChunk

    assert sdk.SignalChunk is SignalChunk


def test_sdk_import_does_not_load_host_runtime_or_optional_dependencies():
    result = subprocess.run([sys.executable, "-c", dedent("""
        import importlib.abc
        import sys

        blocked = (
            "omnibci.conf", "omnibci.application", "omnibci.infrastructure",
            "omnibci.presentation", "OpenGL", "glfw", "moderngl", "PIL", "mne",
        )
        class RejectRuntimeImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == prefix or fullname.startswith(prefix + ".")
                       for prefix in blocked):
                    raise AssertionError("SDK imported runtime dependency: " + fullname)

        sys.meta_path.insert(0, RejectRuntimeImports())
        import omnibci.sdk
        assert omnibci.sdk.Decoder.__module__ == "omnibci.domain.ports.algorithm"
    """)], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr


def test_plugin_context_copies_channels_and_round_trips_through_pickle(tmp_path):
    from omnibci.sdk import PluginContext

    channels = ["Oz", "O1"]
    context = PluginContext(sample_rate=250, channel_names=channels,
                            config_revision="revision-7", resource_root=str(tmp_path))
    channels.append("O2")

    assert context.channel_names == ("Oz", "O1")
    assert pickle.loads(pickle.dumps(context)) == context
    with pytest.raises(FrozenInstanceError):
        context.sample_rate = 1000


@pytest.mark.parametrize("fields", [
    {"sample_rate": 0}, {"sample_rate": True}, {"sample_rate": float("nan")},
    {"channel_names": "Oz"}, {"channel_names": [1]},
    {"config_revision": ""}, {"resource_root": ""},
])
def test_plugin_context_rejects_invalid_metadata(fields):
    from omnibci.sdk import PluginContext

    with pytest.raises(ValueError):
        PluginContext(**fields)


def _run_sdk_filter(parameters, context, output):
    import numpy as np

    from omnibci.sdk import Filter, FilterContext, PluginContext, PluginParameters

    class GainFilter(Filter):
        def __init__(self, *, parameters: PluginParameters, context: PluginContext):
            self.gain = parameters["gain"]
            self.context = context

        def apply(self, data, ctx):
            return data * self.gain

    plugin = GainFilter(parameters=parameters, context=context)
    data = plugin.apply(np.ones((2, 2)), FilterContext(sample_rate=250))
    output.send((data.tolist(), plugin.context))
    output.close()


@pytest.mark.integration
def test_context_and_configuration_are_usable_in_spawned_plugin(tmp_path):
    from omnibci.sdk import PluginContext

    context = PluginContext(sample_rate=250, channel_names=("Oz", "O1"),
                            config_revision="revision-7", resource_root=str(tmp_path))
    spawn = mp.get_context("spawn")
    incoming, outgoing = spawn.Pipe(duplex=False)
    process = spawn.Process(target=_run_sdk_filter, args=({"gain": 2}, context, outgoing))
    try:
        process.start()
        outgoing.close()
        assert incoming.poll(15), "Spawned plugin did not return a result"
        data, received = incoming.recv()
        process.join(timeout=15)
        assert process.exitcode == 0
        assert data == [[2.0, 2.0], [2.0, 2.0]]
        assert received == context
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.pid is not None:
            process.close()
        incoming.close()
        outgoing.close()


def test_paradigm_renderer_argument_is_optional_for_existing_plugins():
    import inspect

    from omnibci.sdk import Paradigm

    for method in (Paradigm.setup, Paradigm.teardown):
        assert inspect.signature(method).parameters["renderer"].default is None
