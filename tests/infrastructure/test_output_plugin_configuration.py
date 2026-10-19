"""Stimulation and Actuation use exact, copied plugin inputs."""
import sys
from unittest.mock import Mock

import pytest

from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.conf import settings
from omnibci.domain.ports.acquisition import TriggerEmitter
from omnibci.infrastructure.actuation.actuator import Actuator
from omnibci.infrastructure.plugins.loader import PluginLoadError, PluginLoader
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.stimulation.stimulator import Stimulator


def binding(slot, kind, alias):
    descriptor = PluginCatalog().resolve(kind, alias)
    return ProfileBinding(slot, kind, None, descriptor.selection, {}).to_dict()


def test_selected_paradigm_receives_trigger_without_reading_globals(monkeypatch):
    snapshot = {"PLUGINS.bindings": [binding("stimulation.paradigm", "paradigm",
                                            "STIMULATION_THUBCI_SSVEP_classic40")]}
    stimulator = Stimulator(settings_snapshot=snapshot)
    monkeypatch.setattr(settings, "get", Mock(side_effect=AssertionError("global settings read")))
    trigger = Mock()
    paradigm = stimulator._load_paradigm(trigger)
    assert paradigm.trigger is trigger


def test_stimulation_without_selected_trigger_starts_and_clears_without_hardware(monkeypatch):
    snapshot = {"PLUGINS.bindings": [binding("stimulation.paradigm", "paradigm",
                                            "STIMULATION_THUBCI_SSVEP_classic40")],
                "ACQUISITION.trigger": "neuroscan_scan45_trigger"}
    stimulator = Stimulator(settings_snapshot=snapshot)
    monkeypatch.setattr(settings, "get", Mock(side_effect=AssertionError("global settings read")))
    trigger = stimulator._load_trigger()
    assert isinstance(trigger, TriggerEmitter)
    assert trigger.send(3) is True
    assert trigger.clear() is None
    paradigm = stimulator._load_paradigm(trigger)
    assert paradigm.trigger is trigger
    paradigm.setup = Mock()
    monkeypatch.setattr(stimulator, "_load_paradigm", Mock(return_value=paradigm))
    renderer, loop = Mock(), Mock()
    monkeypatch.setattr("omnibci.infrastructure.stimulation.stimulator._create_renderer", Mock(return_value=renderer))
    monkeypatch.setattr("omnibci.infrastructure.stimulation.stimulator.RenderLoop", Mock(return_value=loop))

    stimulator.start()
    loop.start.assert_called_once()
    paradigm.setup.assert_called_once_with(renderer=renderer)
    stimulator.stop()
    stimulator.clear_trigger()
    loop.stop.assert_called_once()


def test_explicit_empty_trigger_selection_ignores_legacy_alias():
    stimulator = Stimulator(settings_snapshot={"PLUGINS.bindings": [],
                                              "ACQUISITION.trigger": "neuroscan_scan45_trigger"})
    assert stimulator._load_trigger().send(1) is True


@pytest.mark.parametrize("snapshot", [{}, {"PLUGINS.bindings": None}, {"PLUGINS.bindings": [{}]}])
def test_optional_trigger_still_requires_valid_pinned_bindings(snapshot):
    with pytest.raises((PluginLoadError, ValueError)):
        Stimulator(settings_snapshot=snapshot)._load_trigger()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_selected_trigger_platform_errors_are_not_replaced_by_empty_output(monkeypatch, platform):
    snapshot = {"PLUGINS.bindings": [binding("stimulation.trigger", "trigger", "neuroscan_scan45_trigger")]}
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(PluginLoader, "_prepare_module", Mock(side_effect=AssertionError("hardware import")))
    with pytest.raises(PluginLoadError, match=f"not supported on {platform}"):
        Stimulator(settings_snapshot=snapshot)._load_trigger()


def test_selected_trigger_loading_errors_are_not_replaced_by_empty_output(monkeypatch):
    snapshot = {"PLUGINS.bindings": [binding("stimulation.trigger", "trigger", "neuroscan_scan45_trigger")]}
    monkeypatch.setattr("omnibci.infrastructure.plugins.runtime_configuration.load_configured_plugin",
                        Mock(side_effect=OSError("trigger initialization failed")))
    with pytest.raises(OSError, match="trigger initialization failed"):
        Stimulator(settings_snapshot=snapshot)._load_trigger()


def test_selected_effector_keeps_revision_and_ignores_legacy_alias(monkeypatch):
    snapshot = {"PLUGINS.bindings": [binding("actuation.effector", "effector", "logger")],
                "CONFIG.revision": "selected-revision", "ACTUATION.effector": "wrong-alias"}
    actuator = Actuator(settings_snapshot=snapshot)
    monkeypatch.setattr(settings, "get", Mock(side_effect=AssertionError("global settings read")))
    actuator.setup()
    assert actuator.effector.config_revision == "selected-revision"
    actuator.execute({"target_id": 1})
    actuator.teardown()
    assert actuator.effector is None
