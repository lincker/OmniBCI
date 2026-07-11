"""
P0 regression tests — EnginePort decoupling.

These lock in the result of the P0 refactor:
  - Acquisition / Stimulation app services depend only on the EnginePort
    abstraction; they no longer reach into engine internals (event_bus) or a
    sibling service's private `_engine`.
  - NeuroEngineService satisfies EnginePort.

A pure FakeEnginePort (which has NO `event_bus` attribute) proves the穿透 is
gone: if any service still did `engine.event_bus.put_event(...)`, these tests
would raise AttributeError.
"""
from __future__ import annotations

import pytest

from omnibci.application.ports.engine_port import EnginePort
from omnibci.shared.events import Event, EventType
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand,
    StartStreamCommand,
)
from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StartStimTrialCommand,
)


class FakeEnginePort(EnginePort):
    """Minimal port implementation — records dispatched events, no event_bus."""

    def __init__(self):
        self.dispatched = []

    def dispatch(self, event: Event) -> None:
        self.dispatched.append(event)


class RaisingEnginePort(EnginePort):
    """Port that always rejects dispatch (engine not ready)."""

    def dispatch(self, event: Event) -> None:
        raise RuntimeError("engine not initialised")


# ---------------------------------------------------------------------------
# Port contract
# ---------------------------------------------------------------------------

def test_engine_port_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        EnginePort()  # abstract


def test_neuro_engine_service_is_engine_port():
    from omnibci.application.services.engine_service import NeuroEngineService
    assert issubclass(NeuroEngineService, EnginePort)
    from unittest.mock import MagicMock
    assert isinstance(NeuroEngineService(engine_factory=MagicMock()), EnginePort)


# ---------------------------------------------------------------------------
# Acquisition no longer touches engine internals
# ---------------------------------------------------------------------------

class TestAcquisitionUsesPort:
    def test_connect_dispatches_via_port_only(self):
        port = FakeEnginePort()
        svc = AcquisitionAppService(port)
        svc.handle_connect(ConnectDataProviderCommand())
        assert len(port.dispatched) == 1
        assert port.dispatched[0].type == EventType.ConnectDataProvider

    def test_start_stream_dispatches_via_port(self):
        port = FakeEnginePort()
        svc = AcquisitionAppService(port)
        svc.handle_start_stream(StartStreamCommand())
        assert port.dispatched[0].type == EventType.StartStream

    def test_no_private_engine_svc_attribute_remains(self):
        svc = AcquisitionAppService(FakeEnginePort())
        # the old穿透 seam is gone
        assert not hasattr(svc, "_engine_svc")

    def test_propagates_engine_not_ready(self):
        svc = AcquisitionAppService(RaisingEnginePort())
        with pytest.raises(RuntimeError):
            svc.handle_connect(ConnectDataProviderCommand())


# ---------------------------------------------------------------------------
# Stimulation no longer touches engine internals
# ---------------------------------------------------------------------------

class TestStimulationUsesPort:
    def test_start_stimulation_dispatches_via_port(self):
        port = FakeEnginePort()
        svc = StimulationAppService(port)
        svc.handle_start_stimulation(StartStimulationCommand(paradigm="classic40SSVEP"))
        assert port.dispatched[0].type == EventType.StartStimulation
        assert port.dispatched[0].data == "classic40SSVEP"

    def test_start_trial_dispatches_spec(self):
        port = FakeEnginePort()
        svc = StimulationAppService(port)
        svc.handle_start_trial(StartStimTrialCommand(n_trials=1, trial_params=[7]))
        assert port.dispatched[0].type == EventType.StartStimTrial
        assert port.dispatched[0].data == {"n_trials": 1, "trial_params": [7]}

    def test_no_private_engine_svc_attribute_remains(self):
        svc = StimulationAppService(FakeEnginePort())
        assert not hasattr(svc, "_engine_svc")
