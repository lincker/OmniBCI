"""Stimulation and Actuation use exact, copied plugin inputs."""
from unittest.mock import Mock

from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.conf import settings
from omnibci.infrastructure.actuation.actuator import Actuator
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
