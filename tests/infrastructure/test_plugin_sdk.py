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
    ("SourceObservation", "acquisition"), ("SourceCheckpoint", "acquisition"),
    ("Filter", "algorithm"), ("EpochBoundary", "algorithm"),
    ("Decoder", "algorithm"), ("Prediction", "algorithm"),
    ("EpochDetector", "algorithm"), ("Paradigm", "paradigm"),
    ("Effector", "effector"), ("RenderSurface", "renderer"), ("TriggerOutput", "acquisition"),
    ("TextureHandle", "renderer"), ("Rect", "renderer"), ("Color", "renderer"),
])
def test_sdk_reexports_existing_contract_types(name, module):
    from omnibci import sdk

    domain_module = importlib.import_module(f"omnibci.domain.ports.{module}")
    assert getattr(sdk, name) is getattr(domain_module, name)


@pytest.mark.parametrize("name", ["SignalInfo", "ChannelDescriptor"])
def test_sdk_reexports_signal_layout(name):
    from omnibci import sdk
    from omnibci.domain import signal_info
    assert getattr(sdk, name) is getattr(signal_info, name)


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


def test_signal_metadata_preserves_identity_and_immutability_through_pickle():
    from omnibci.sdk import ChannelDescriptor, SignalInfo
    channels = [ChannelDescriptor(0, "Oz", channel_id="channel-0"),
                ChannelDescriptor(1, "O1", channel_id="channel-1")]
    signal = SignalInfo(sample_rate=250, channels=channels)
    channels.append(ChannelDescriptor(2, "O2", channel_id="channel-2"))
    assert tuple(channel.name for channel in signal.channels) == ("Oz", "O1")
    assert pickle.loads(pickle.dumps(signal)) == signal
    with pytest.raises(FrozenInstanceError):
        signal.sample_rate = 1000


def _run_sdk_filter(parameters, resource_root, input_signal, output):
    from collections.abc import Mapping
    from pathlib import Path
    import numpy as np
    from omnibci.sdk import Filter, SignalInfo

    class GainFilter(Filter):
        def __init__(self, *, parameters: Mapping[str, object], resource_root: Path,
                     input_signal: SignalInfo):
            self.gain = parameters["gain"]
            self.input_signal = input_signal

        def apply(self, data, *, pre_trigger_data):
            return data * self.gain

    plugin = GainFilter(parameters=parameters, resource_root=resource_root, input_signal=input_signal)
    data = plugin.apply(np.ones((2, 2)), pre_trigger_data=None)
    output.send((data.tolist(), plugin.input_signal))
    output.close()


@pytest.mark.integration
def test_sdk_inputs_work_across_spawn(tmp_path):
    from omnibci.sdk import ChannelDescriptor, SignalInfo
    signal = SignalInfo(sample_rate=250, channels=(
        ChannelDescriptor(0, "Oz", channel_id="oz"),
        ChannelDescriptor(1, "O1", channel_id="o1"),
    ))
    context = mp.get_context("spawn")
    incoming, outgoing = context.Pipe(duplex=False)
    process = context.Process(target=_run_sdk_filter, args=({"gain": 2}, tmp_path, signal, outgoing))
    try:
        process.start()
        outgoing.close()
        assert incoming.poll(15)
        data, received = incoming.recv()
        process.join(timeout=15)
        assert process.exitcode == 0
        assert data == [[2.0, 2.0], [2.0, 2.0]]
        assert received == signal
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.pid is not None:
            process.close()
        incoming.close()
        outgoing.close()


def test_paradigm_borrows_required_capabilities_only_during_setup():
    import inspect
    from omnibci import sdk
    from omnibci.sdk import Paradigm

    parameters = inspect.signature(Paradigm.setup).parameters
    for name in ("renderer", "trigger"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default is inspect.Parameter.empty
    assert tuple(inspect.signature(Paradigm.teardown).parameters) == ("self",)
    assert tuple(inspect.signature(Paradigm.render_frame).parameters) == ("self", "elapsed_seconds")
    assert len(sdk.__all__) == 18
    assert not hasattr(sdk, "FrameTiming")
