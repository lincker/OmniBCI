"""Exercise the same downloadable starter used by the in-app authoring guide."""
from pathlib import Path
import zipfile

import numpy as np
import pytest

from omnibci.domain.acquisition_descriptor import AcquisitionDescriptor, ChannelDescriptor
from omnibci.infrastructure.plugins.descriptors import PluginSelection
from omnibci.infrastructure.plugins.loader import PluginLoadError, PluginLoader
from omnibci.infrastructure.plugins.package_installer import PluginPackageInstaller
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginRoot
from omnibci.sdk import Decoder, PluginContext


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
    source = AcquisitionDescriptor("tutorial", 4, tuple(
        ChannelDescriptor(index, name, signal_type="eeg", unit="uV")
        for index, name in enumerate(("Oz", "O2", "O1"))
    ), 3)
    mapped = AcquisitionDescriptor("tutorial", 4, tuple(
        ChannelDescriptor(index, name, signal_type="eeg", unit="uV") for index, name in enumerate(names)
    ), len(names))
    return PluginContext(sample_rate=4, channel_names=("Oz", "O2", "O1"),
                         acquired_signal=source, input_signal=mapped)


def test_starter_installs_and_decodes_mapped_named_input(descriptor):
    assert descriptor.signal_input_contract == 2
    assert descriptor.channel_requirements.required_names == ("Oz",)
    assert descriptor.channel_requirements.default_names == ("Oz",)
    decoder = PluginLoader.instantiate(descriptor, Decoder, parameters={}, context=context("O2", "Oz"))
    # Oz was source row 0 and is mapped row 1. The unrelated row has the opposite sign.
    window = np.array([[-100.0] * 4, [2.0] * 4])
    assert decoder.should_decode(window)
    assert decoder.decode(window, window[:, -1:]).target_id == 1
    assert decoder.decode(-window, -window[:, -1:]).target_id == 2
    assert decoder.for_new_epoch() is decoder  # The tutorial has no mutable epoch state.


def test_starter_declines_forced_short_and_nonfinite_windows(descriptor):
    decoder = PluginLoader.instantiate(descriptor, Decoder, parameters={}, context=context("Oz"))
    short = np.ones((1, 3))
    assert not decoder.should_decode(short)
    assert decoder.decode(short, short) is None
    invalid = np.full((1, 4), np.nan)
    assert decoder.decode(invalid, invalid) is None


def test_starter_rejects_unprepared_or_missing_named_input(descriptor):
    with pytest.raises(PluginLoadError, match="prepared input metadata"):
        PluginLoader.instantiate(descriptor, Decoder, parameters={}, context=PluginContext(sample_rate=4))
    with pytest.raises(PluginLoadError, match="Oz"):
        PluginLoader.instantiate(descriptor, Decoder, parameters={}, context=context("O2"))
