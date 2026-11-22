"""Run with python -I after installing a built wheel, without a checkout import."""
from importlib import metadata, resources
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import numpy as np
import omnibci
from omnibci import sdk

from omnibci.application.plugin_schema import validate_parameters
from omnibci.application.plugin_identity import PluginSelection
from omnibci.infrastructure.plugins.loader import PluginLoader
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginRoot, vendor_plugins_dir
from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.frame_pack import FramePack


def main():
    checkout = Path(__file__).resolve().parents[2]
    installed_paths = [Path(path).resolve() for path in omnibci.__path__]
    assert installed_paths and all(
        not path.is_relative_to(checkout / "omnibci") for path in installed_paths
    ), ("Wheel smoke test can import the source checkout instead of the installed package", installed_paths)
    vendor = vendor_plugins_dir()
    kinds = ("acquisition", "decoder", "epoch_detector", "filter", "paradigm", "trigger", "effector")
    catalog = PluginCatalog({kind: lambda: [PluginRoot(vendor, "builtin", 100)] for kind in kinds})
    contributions = []
    for kind in kinds:
        selected = catalog.list_contributions(kind)
        assert selected, f"Installed wheel contains no {kind} contributions"
        for contribution in selected:
            assert contribution.manifest_path.is_file()
            assert contribution.api_version == "1"
            assert contribution.configuration_version == 1
            assert contribution.configuration_schema is not None
            validate_parameters(contribution.configuration_schema, {})
        contributions.extend(selected)
    assert len(contributions) == 13, len(contributions)
    assert len(sdk.__all__) == 19
    assert all(getattr(sdk, name).__module__.startswith("omnibci.domain.") for name in sdk.__all__)
    assert not any(hasattr(sdk, name) for name in ("PluginContext", "PluginParameters", "Renderer"))
    entrypoint, = [item for item in metadata.distribution("omnibci").entry_points
                   if item.group == "console_scripts" and item.name == "omnibci-plugin"]
    from omnibci.plugin_cli import main as plugin_main
    assert entrypoint.load() is plugin_main
    # Source files are inputs only; isolated Python must execute the installed CLI.
    with TemporaryDirectory(prefix="omnibci-wheel-") as temporary:
        starter = str(checkout / "examples/decoder_plugin")
        command = [sys.executable, "-I", "-B", "-m", "omnibci.plugin_cli"]
        validated = subprocess.run(command + ["validate", starter], cwd=temporary,
                                   check=True, capture_output=True, text=True, timeout=60)
        packaged = subprocess.run(command + ["package", starter, "--output", str(Path(temporary) / "starter.zip")],
                                  cwd=temporary, check=True, capture_output=True, text=True, timeout=60)
        assert json.loads(validated.stdout) == json.loads(packaged.stdout)
    from omnibci.application.configuration.definitions import ENGINE_SETTING_DEFAULTS, MODULE_SETTING_DEFAULTS
    from omnibci.application.configuration.models import EngineSettings, ModuleActivation
    EngineSettings(ENGINE_SETTING_DEFAULTS)
    for module, settings in MODULE_SETTING_DEFAULTS.items():
        ModuleActivation(module, settings, ())
    resource = resources.files("omnibci.infrastructure.plugins.paradigms.classic40SSVEP").joinpath(
        "resource", "classic40_ssvep.ofp",
    )
    # FramePack checks the complete archive manifest and verifies hashes while
    # reading every frame; no renderer or OpenGL context is created.
    with resources.as_file(resource) as path, FramePack(path) as pack:
        assert pack.idle_frame.shape == (990, 1920, 1)
        assert sum(1 for _ in pack.iter_stimulation_frames()) == 240
        assert sum(1 for _ in pack.iter_cue_frames()) == 40
    command10 = catalog.resolve_selection(
        "paradigm", PluginSelection("OmniBCI.command10SSVEP", "default", "0.1.0"),
    )
    paradigm = PluginLoader.instantiate(command10, sdk.Paradigm, parameters={})
    renderer, trigger = MagicMock(spec=sdk.RenderSurface), MagicMock(spec=sdk.TriggerOutput)
    renderer.upload_texture.side_effect = lambda pixels: sdk.TextureHandle()
    try:
        paradigm.setup(renderer=renderer, trigger=trigger)
        assert renderer.upload_texture.call_count == 11
        paradigm.present_trial({"cue_target_id": 0})
        assert paradigm.render_frame(sdk.FrameTiming(refresh_index=0, target_time=0, refresh_period=1 / 60))
        trigger.send.assert_called_once_with(11)
    finally:
        paradigm.teardown()
    assert renderer.release_texture.call_count == 11
    command10_decoder = catalog.resolve_selection(
        "decoder", PluginSelection("OmniBCI.command10_fbcca", "default", "0.1.0"),
    )
    assert (command10.location / "targets.py").read_bytes() == (command10_decoder.location / "targets.py").read_bytes()
    signal = sdk.SignalInfo(sample_rate=250, channels=(
        sdk.ChannelDescriptor(0, "Oz", channel_id="oz", signal_type="eeg", unit="uV"),
    ))
    decoder = PluginLoader.instantiate(command10_decoder, sdk.Decoder, parameters={}, input_signal=signal)
    try:
        decoder.setup()
        time = np.arange(1000) / signal.sample_rate
        for target_id in range(1, 11):
            frequency = 8 + .8 * (target_id - 1)
            phase = ((target_id - 1) % 4) * np.pi / 2
            data = sum(np.sin(h * (2 * np.pi * frequency * time + phase)) / h for h in range(1, 6))[None, :]
            decoder.begin_epoch()
            assert decoder.decode(data, latest_raw_segment=data[:, -10:]).target_id == target_id
    finally:
        decoder.teardown()
    print(f"Installed wheel verified: {len(contributions)} API v1 contributions, 19 SDK exports, "
          "author CLI, 281 Classic40 frames, 11 Command10 textures and 10 FBCCA target predictions")


if __name__ == "__main__":
    main()
