"""Plugin API v1 construction, built-in contribution loading, and decoder input runtime."""
from dataclasses import replace
import inspect
from pathlib import Path
import sys
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
import tomlkit

from omnibci import sdk
from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.plugin_management.schema import validate_parameters
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, compile_processing_plan
from omnibci.domain.decoder_input import DecoderChannelRequirements
from omnibci.domain.ports.acquisition import SignalSource, TriggerEmitter
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter
from omnibci.domain.ports.effector import Effector
from omnibci.domain.ports.paradigm import Paradigm
from omnibci.domain.signal import SignalChunk
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog, PluginRoot, vendor_plugins_dir
from omnibci.infrastructure.plugin_management.loader import PluginLoadError, PluginLoader
from omnibci.infrastructure.plugin_management.manifest import PluginManifestError
from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin
from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import Curry8DataReader, Curry8Metadata
from omnibci.infrastructure.signal_processing.signal_configuration import mapped_input
from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot

CAPABILITIES = [
    ("acquisition", "file_reader", SignalSource),
    ("acquisition", "neuroscan_scan45_reader", SignalSource),
    ("acquisition", "neuroscan_curry8_reader", SignalSource),
    ("trigger", "neuroscan_scan45_trigger", TriggerEmitter),
    ("trigger", "neuroscan_curry8_trigger", TriggerEmitter),
    ("decoder", "THUBCI_SSVEP_classic40CCA", Decoder),
    ("decoder", "THUBCI_SSVEP_classic40FBCCA", Decoder),
    ("epoch_detector", "DEFAULT_EPOCH_DETECTOR", EpochDetector),
    ("filter", "BASELINE_CORRECTION", Filter),
    ("paradigm", "STIMULATION_THUBCI_SSVEP_classic40", Paradigm),
    ("effector", "logger", Effector),
]

PROCESSING = {"filter", "decoder", "epoch_detector"}


def input_signal():
    return SignalInfo(sample_rate=250, channels=(
        ChannelDescriptor(0, "Oz", channel_id="oz", signal_type="eeg"),
    ))


@pytest.fixture
def catalog():
    return PluginCatalog({
        "acquisition": lambda: [PluginRoot(vendor_plugins_dir(), "builtin", 100)],
    })


def test_sdk_has_only_the_eighteen_v1_exports():
    assert set(sdk.__all__) == {
        "SignalSource", "Filter", "Decoder", "EpochDetector", "Paradigm", "TriggerEmitter",
        "Effector", "SignalInfo", "ChannelDescriptor", "SourceObservation", "SourceCheckpoint",
        "Prediction", "EpochBoundary", "RenderSurface", "Color", "Rect", "TextureHandle",
        "TriggerOutput",
    }
    for name in ("PluginContext", "PluginParameters", "SignalChunk", "Renderer", "FilterContext"):
        assert not hasattr(sdk, name)


@pytest.mark.parametrize("kind,alias,port", CAPABILITIES)
def test_builtin_exact_selection_loads_its_domain_port_and_constructor_signature(catalog, monkeypatch, tmp_path, kind, alias, port):
    descriptor = catalog.resolve_selection(kind, builtin_selection(kind, alias))
    assert descriptor.package.manifest_version == 1
    assert descriptor.api_version == "1"
    assert descriptor in catalog.list_contributions(kind)
    assert catalog.resolve_selection(kind, descriptor.selection) == descriptor
    if kind == "trigger":
        monkeypatch.setattr(sys, "platform", "win32")
    implementation = PluginLoader.load_entrypoint(descriptor, port)
    assert issubclass(implementation, port)
    params = inspect.signature(implementation).parameters
    expected = {"parameters", "resource_root"} | ({"input_signal"} if kind in PROCESSING else set())
    assert set(params) == expected
    assert all(item.kind is inspect.Parameter.KEYWORD_ONLY and item.default is inspect.Parameter.empty
               for item in params.values())

    parameters = validate_parameters(descriptor.configuration_schema, {})
    if alias == "file_reader":
        parameters["data_file_path"] = str(tmp_path / "recording.cnt")
    kwargs = {"parameters": parameters}
    if kind in PROCESSING:
        kwargs["input_signal"] = input_signal()
    instance = PluginLoader.instantiate(descriptor, port, **kwargs)
    assert isinstance(instance, port)


@pytest.mark.parametrize("kind,alias,port", CAPABILITIES)
def test_host_does_not_pass_its_revision_or_upstream_signal(monkeypatch, kind, alias, port):
    descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
    slot = {"acquisition": "acquisition.source", "trigger": "stimulation.trigger",
            "paradigm": "stimulation.paradigm", "effector": "actuation.effector",
            "decoder": "processing.decoder", "epoch_detector": "processing.epoch_detector",
            "filter": "processing.filter.00000000-0000-0000-0000-000000000001"}[kind]
    binding = ProfileBinding(slot, kind, None, descriptor.selection, {},
                             configuration_version=descriptor.configuration_version)
    snapshot = {"PLUGINS.bindings": [binding.to_dict()], "ACQUISITION.sample_rate": 1000,
                "CONFIG.module_revisions": {"role": "private-revision"}}
    loader = Mock()
    if kind == "trigger":
        monkeypatch.setattr(sys, "platform", "win32")
    snapshot = prepared_snapshot(snapshot)
    kwargs = {"input_signal": input_signal()} if kind in PROCESSING else {}
    load_configured_plugin(snapshot, slot, kind, port, loader=loader, **kwargs)
    assert set(loader.instantiate.call_args.kwargs) == {"parameters"} | set(kwargs)
    if kwargs:
        assert loader.instantiate.call_args.kwargs["input_signal"] is kwargs["input_signal"]


def test_loader_provides_the_selected_resource_path_and_copies_only_parameters(monkeypatch):
    descriptor = PluginCatalog().resolve_selection("effector", builtin_selection("effector", "logger"))
    descriptor = replace(descriptor, package=replace(descriptor.package, api_version="1"),
                         configuration_schema=None)
    constructor = Mock()
    monkeypatch.setattr(PluginLoader, "load_entrypoint", Mock(return_value=constructor))
    parameters = {"items": [1, 2]}
    PluginLoader.instantiate(descriptor, sdk.Effector, parameters=parameters)
    supplied = constructor.call_args.kwargs
    assert set(supplied) == {"parameters", "resource_root"}
    assert isinstance(supplied["resource_root"], Path)
    assert supplied["resource_root"] == descriptor.location
    supplied["parameters"]["items"].append(3)
    assert parameters == {"items": [1, 2]}


@pytest.mark.parametrize("kind,kwargs", [
    ("decoder", {"parameters": {}}),
    ("decoder", {"parameters": {}, "input_signal": None}),
    ("effector", {"parameters": {}, "input_signal": input_signal()}),
    ("effector", {"parameters": {}, "context": object()}),
    ("effector", {"parameters": {}, "resource_root": Path("/tmp")}),
])
def test_invalid_role_inputs_are_rejected_before_import(monkeypatch, kind, kwargs):
    alias = "logger" if kind == "effector" else "THUBCI_SSVEP_classic40CCA"
    descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
    descriptor = replace(descriptor, package=replace(descriptor.package, api_version="1"))
    load = Mock(side_effect=AssertionError("Invalid inputs must fail before import"))
    monkeypatch.setattr(PluginLoader, "load_entrypoint", load)
    with pytest.raises(PluginLoadError):
        PluginLoader.instantiate(descriptor, sdk.Effector if kind == "effector" else sdk.Decoder, **kwargs)
    load.assert_not_called()


@pytest.mark.parametrize("value", [None, {}])
def test_runtime_requires_prepared_input_even_without_channel_requirements(value):
    descriptor = PluginCatalog().resolve_selection("filter", builtin_selection("filter", "BASELINE_CORRECTION"))
    slot = "processing.filter.00000000-0000-0000-0000-000000000001"
    binding = ProfileBinding(slot, "filter", None, descriptor.selection, {})
    loader = Mock()
    with pytest.raises(PluginLoadError, match="prepared input"):
        load_configured_plugin({"PLUGINS.bindings": [binding.to_dict()]}, slot, "filter", sdk.Filter,
                               input_signal=value, loader=loader)
    loader.instantiate.assert_not_called()


def test_constructor_size_limit_includes_schema_defaults(monkeypatch):
    descriptor = PluginCatalog().resolve_selection("effector", builtin_selection("effector", "logger"))
    descriptor = replace(descriptor, configuration_schema={
        "type": "object", "additionalProperties": False,
        "properties": {"huge": {"type": "string", "default": "x" * (1024 * 1024)}},
    })
    load = Mock(side_effect=AssertionError("Oversize inputs must fail before import"))
    monkeypatch.setattr(PluginLoader, "load_entrypoint", load)
    with pytest.raises(PluginLoadError, match="1 MiB"):
        PluginLoader.instantiate(descriptor, sdk.Effector, parameters={})
    load.assert_not_called()


def _custom_descriptor(tmp_path, body="self.parameters = parameters; self.resource_root = resource_root"):
    (tmp_path / "manifest.toml").write_text(dedent('''
        manifest_version = 1
        [package]
        domain = "actuation"
        id = "example"
        publisher = "test"
        version = "1.0.0"
        api_version = "1"
        [[contributions]]
        id = "output"
        kind = "effector"
        entrypoint = "output:Output"
        [contributions.payload_schemas]
        command = "command.schema.json"
    '''), encoding="utf-8")
    (tmp_path / "command.schema.json").write_text('{"type":"object","properties":{},"additionalProperties":false}')
    (tmp_path / "output.py").write_text(dedent(f'''
        from omnibci.sdk import Effector
        class Output(Effector):
            def __init__(self, *, parameters, resource_root):
                {body}
            def setup(self): pass
            def teardown(self): pass
            def execute(self, command): pass
            def quiesce(self): pass
    '''), encoding="utf-8")
    cat = PluginCatalog({"effector": lambda: [PluginRoot(tmp_path, "user", 0)]})
    return cat.resolve_selection("effector", PluginSelection("test.example", "output", "1.0.0"))


def test_api_one_needs_no_registry_alias_and_copies_parameters(tmp_path):
    selected = _custom_descriptor(tmp_path)
    parameters = {"mapping": [1, 2]}
    plugin = PluginLoader.instantiate(selected, Effector, parameters=parameters)
    plugin.parameters["mapping"].append(3)
    assert parameters == {"mapping": [1, 2]}
    assert plugin.resource_root == tmp_path


@pytest.mark.parametrize("kwargs", [
    {}, {"parameters": {}, "context": {}},
    {"parameters": []},
    {"parameters": {"value": float("nan")}},
    {"parameters": {}, "unexpected": True},
    {"parameters": {"values": (1, 2)}}, {"parameters": {1: "value"}},
    {"parameters": {"huge": "x" * (1024 * 1024)}},
])
def test_invalid_constructor_inputs_fail_before_import(tmp_path, monkeypatch, kwargs):
    selected = _custom_descriptor(tmp_path)
    monkeypatch.setattr(PluginLoader, "load_entrypoint", lambda *a: pytest.fail("imported"))
    with pytest.raises(PluginLoadError):
        PluginLoader.instantiate(selected, Effector, **kwargs)


def test_constructor_error_is_not_retried(tmp_path):
    marker = tmp_path / "attempts"
    selected = _custom_descriptor(tmp_path, body=(
        f"from pathlib import Path; p = Path({str(marker)!r}); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x'); "
        "raise TypeError('plugin bug')"
    ))
    with pytest.raises(PluginLoadError, match="plugin bug"):
        PluginLoader.instantiate(selected, Effector, parameters={})
    assert marker.read_text() == "x"


@pytest.mark.parametrize("device", ["scan45", "curry8"])
def test_trigger_platform_is_independent_from_acquisition(catalog, monkeypatch, device):
    trigger = catalog.resolve_selection("trigger", builtin_selection("trigger", f"neuroscan_{device}_trigger"))
    reader = catalog.resolve_selection("acquisition", builtin_selection("acquisition", f"neuroscan_{device}_reader"))
    assert trigger.package == reader.package
    assert trigger.supports_platform("win32")
    for host in ("darwin", "linux"):
        assert reader.supports_platform(host)
        assert not trigger.supports_platform(host)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(PluginLoader, "_prepare_module", lambda *a: pytest.fail("imported"))
    with pytest.raises(PluginLoadError, match="not supported on linux"):
        PluginLoader.load_entrypoint(trigger, TriggerEmitter)


def test_package_and_contribution_platforms_both_apply(catalog):
    trigger = catalog.resolve_selection("trigger", builtin_selection("trigger", "neuroscan_scan45_trigger"))
    restricted = replace(trigger, package=replace(trigger.package, platforms=("linux",)))
    assert not any(restricted.supports_platform(host) for host in ("darwin", "linux", "win32"))


@pytest.mark.parametrize("platforms", [[], "win32", ["windows"], ["linux", "linux"], [1]])
@pytest.mark.parametrize("section", ["package", "contribution"])
def test_malformed_platforms_reject_package(tmp_path, platforms, section):
    package = {"id": "example", "publisher": "test", "version": "1", "domain": "acquisition", "api_version": "1"}
    contribution = {"id": "reader", "kind": "acquisition", "entrypoint": "reader:Reader"}
    (package if section == "package" else contribution)["platforms"] = platforms
    (tmp_path / "manifest.toml").write_text(tomlkit.dumps({
        "manifest_version": 1, "package": package, "contributions": [contribution],
    }), encoding="utf-8")
    cat = PluginCatalog({"acquisition": lambda: [PluginRoot(tmp_path, "user", 0)]})
    with pytest.raises(PluginManifestError, match="platforms"):
        cat.resolve_selection("acquisition", PluginSelection("test.example", "reader", "1"))


def test_selected_paradigm_can_read_its_pack_without_executing_package_initializers(catalog):
    descriptor = catalog.resolve_selection("paradigm", builtin_selection("paradigm", "STIMULATION_THUBCI_SSVEP_classic40"))
    implementation = PluginLoader.load_entrypoint(descriptor, Paradigm)
    path = sys.modules[implementation.__module__]._frame_pack_path(descriptor.location)
    assert Path(path) == descriptor.location / "resource" / "classic40_ssvep.ofp"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("kind,subpath", [
    ("filter", "algorithms/filters"), ("epoch_detector", "algorithms/epoch"),
])
def test_category_root_can_itself_be_a_package(kind, subpath):
    root = vendor_plugins_dir() / subpath
    cat = PluginCatalog({kind: lambda: [PluginRoot(root, "builtin", 100)]})
    assert len(cat.list_contributions(kind)) == 1


def _configured_decoder_input(monkeypatch, requirements, outputs=("O1",), *, signal_type="eeg"):
    source = SignalInfo(sample_rate=250, channels=(
        ChannelDescriptor(0, "O1", signal_type=signal_type, unit="uV", channel_id="channel-0"),
        ChannelDescriptor(1, "Oz", signal_type="eeg", unit="uV", channel_id="channel-1"),
        ChannelDescriptor(2, "M1", signal_type="eeg", unit="uV", channel_id="channel-2"),
    ))
    ids = {channel.name: channel.channel_id for channel in source.channels}
    inputs = ProcessingInput(outputs=tuple(ChannelBinding(name, ids[name]) for name in outputs))
    selection = PluginSelection("test.decoder", "default", "1")
    binding = ProfileBinding("processing.decoder", "decoder", None, selection, {}, processing_input=inputs)
    snapshot = {"PLUGINS.bindings": [binding.to_dict()]}
    plan = compile_processing_plan(source, inputs)
    descriptor = SimpleNamespace(channel_requirements=requirements,
                                 api_version="1", configuration_version=1)
    monkeypatch.setattr("omnibci.infrastructure.plugin_management.runtime.resolve_configured_plugin",
                        lambda snapshot, slot, kind: (descriptor, {}))
    return snapshot, mapped_input(plan)


@pytest.mark.parametrize("requirements, fields", [
    (DecoderChannelRequirements(required_names=("Oz",)), {}),
    (DecoderChannelRequirements(min_count=2), {}),
    (DecoderChannelRequirements(signal_type="eeg"), {"signal_type": "eog"}),
    (DecoderChannelRequirements(required_names=("M1",)), {}),
])
def test_invalid_final_input_never_constructs_decoder(monkeypatch, requirements, fields):
    snapshot, context = _configured_decoder_input(monkeypatch, requirements, **fields)
    loader = Mock()
    with pytest.raises(PluginLoadError):
        load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                               input_signal=context, loader=loader)
    loader.instantiate.assert_not_called()


def test_required_names_allow_extra_channels_and_profile_order(monkeypatch):
    requirements = DecoderChannelRequirements(signal_type="eeg", min_count=2, required_names=("O1",))
    snapshot, context = _configured_decoder_input(monkeypatch, requirements, outputs=("Oz", "O1"))
    loader = Mock()
    result = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                                    input_signal=context, loader=loader)
    assert result is loader.instantiate.return_value
    assert [channel.name for channel in loader.instantiate.call_args.kwargs["input_signal"].channels] == ["Oz", "O1"]


def test_declared_requirements_cannot_run_without_verified_channel_metadata(monkeypatch):
    snapshot, _ = _configured_decoder_input(monkeypatch, DecoderChannelRequirements(signal_type="eeg"))
    loader = Mock()
    with pytest.raises(PluginLoadError, match="prepared input"):
        load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                               input_signal=None, loader=loader)
    loader.instantiate.assert_not_called()


def test_processing_plugin_receives_its_prepared_signal(monkeypatch):
    snapshot, signal = _configured_decoder_input(monkeypatch, None)
    loader = Mock()
    load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                           input_signal=signal, loader=loader)
    loader.instantiate.assert_called_once()


@pytest.mark.asyncio
async def test_curry_unknown_types_require_processing_declaration_before_builtin_cca():
    reader = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)
    reader._is_connected = True
    reader._metadata = Curry8Metadata(803, 3, 500, 4, False, False, ("C3", "C4", "Trigger"))
    reader._load_metadata = AsyncMock(side_effect=lambda: setattr(reader, "_metadata", Curry8Metadata(803, 3, 500, 4, False, False, ("C3", "C4", "Trigger"))))
    observation = await reader.observe()
    raw_metadata = observation.native_text
    source = observation.signal_info
    assert "channel_types" not in raw_metadata
    assert all(channel.signal_type == "unknown" for channel in source.channels)
    cat = PluginCatalog()
    definition = cat.resolve_selection("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40CCA"))
    undeclared = ProcessingInput()
    binding = ProfileBinding("processing.decoder", "decoder", None, definition.selection,
                             {"window_duration": 1.0, "offset_time": 0}, processing_input=undeclared)
    snapshot = prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()]})
    with pytest.raises(PluginLoadError, match="eeg"):
        load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                               input_signal=mapped_input(compile_processing_plan(source, undeclared)))

    declared = ProcessingInput(outputs=(ChannelBinding("Right", "channel-1", "eeg"),))
    binding = ProfileBinding("processing.decoder", "decoder", None, definition.selection,
                             binding.parameters, processing_input=declared)
    snapshot = prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()]})
    plan = compile_processing_plan(source, declared)
    decoder = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                                     input_signal=mapped_input(plan))
    decoder.setup()
    decoder.begin_epoch()
    times = np.arange(501) / source.sample_rate
    signal = np.sin(2 * np.pi * 8 * times)
    chunk = SignalChunk(123, 1, np.vstack((np.zeros(501), signal, np.zeros(501))))
    mapped = plan.apply(chunk)
    np.testing.assert_array_equal(mapped.data[0], signal)
    np.testing.assert_array_equal(mapped.triggers, chunk.triggers)
    assert decoder.should_decode(mapped.data)
    assert decoder.decode(mapped.data, latest_raw_segment=mapped.data[:, -1:]).target_id == 1
    decoder.teardown()
    assert all(channel.signal_type == "unknown" for channel in source.channels)
    assert "channel_types" not in raw_metadata
