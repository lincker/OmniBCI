"""Exercise the same downloadable starter used by the in-app authoring guide."""
from dataclasses import replace
from pathlib import Path
import zipfile

import numpy as np
import pytest

from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.infrastructure.plugin_management.loader import PluginLoadError, PluginLoader
from omnibci.infrastructure.plugin_management.installer import PluginPackageInstaller
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog, PluginRoot
from omnibci.sdk import Decoder


STARTER = Path(__file__).resolve().parents[2] / "examples" / "decoder_plugin"


@pytest.fixture
def descriptor(tmp_path):
    archive = tmp_path / "starter.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for name in ("manifest.toml", "decoder.py", "default.schema.json", "README.md"):
            package.write(STARTER / name, name)
    installed = PluginPackageInstaller(tmp_path / "packages").install(archive)
    assert installed.package_id == "example.oz-polarity-demo"
    assert not list(installed.location.rglob("__pycache__"))
    catalog = PluginCatalog({"decoder": lambda: [PluginRoot(installed.location, "user", 0)]})
    return catalog.resolve_selection("decoder", PluginSelection(
        "example.oz-polarity-demo", "oz-polarity", "0.1.0",
    ))


def context(*names):
    source = SignalInfo(sample_rate=4, channels=tuple(
        ChannelDescriptor(index, name, signal_type="eeg", unit="uV", channel_id=f"channel-{index}")
        for index, name in enumerate(("Oz", "O2", "O1"))
    ))
    mapped = replace(source, channels=tuple(
        replace(next(channel for channel in source.channels if channel.name == name), index=index)
        for index, name in enumerate(names)
    ))
    return mapped


def test_starter_installs_and_decodes_mapped_named_input(descriptor):
    assert descriptor.channel_requirements.required_names == ("Oz",)
    assert descriptor.channel_requirements.default_names == ("Oz",)
    prepared = context("O2", "Oz")
    assert tuple(channel.channel_id for channel in prepared.channels) == ("channel-1", "channel-0")
    decoder = PluginLoader.instantiate(descriptor, Decoder, parameters={}, input_signal=prepared)
    decoder.setup()
    decoder.begin_epoch()
    # Oz was source row 0 and is mapped row 1. The unrelated row has the opposite sign.
    window = np.array([[-100.0] * 4, [2.0] * 4])
    assert decoder.should_decode(window)
    assert decoder.decode(window, latest_raw_segment=window[:, -1:]).target_id == 1
    assert decoder.decode(-window, latest_raw_segment=-window[:, -1:]).target_id == 2
    decoder.teardown()


def test_starter_declines_forced_short_and_nonfinite_windows(descriptor):
    decoder = PluginLoader.instantiate(descriptor, Decoder, parameters={}, input_signal=context("Oz"))
    decoder.setup()
    decoder.begin_epoch()
    short = np.ones((1, 3))
    assert not decoder.should_decode(short)
    assert decoder.decode(short, latest_raw_segment=short) is None
    invalid = np.full((1, 4), np.nan)
    assert decoder.decode(invalid, latest_raw_segment=invalid) is None
    decoder.teardown()


def test_starter_rejects_unprepared_or_missing_named_input(descriptor):
    with pytest.raises(PluginLoadError, match="prepared input_signal"):
        PluginLoader.instantiate(descriptor, Decoder, parameters={}, input_signal=None)
    with pytest.raises(PluginLoadError, match="Oz"):
        PluginLoader.instantiate(descriptor, Decoder, parameters={}, input_signal=context("O2"))
