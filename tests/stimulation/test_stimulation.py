"""
Unit tests for Stimulator.

All external dependencies are mocked:
  - _create_renderer   : returns a MagicMock renderer
  - _load_trigger      : returns a MagicMock trigger
  - _load_paradigm     : returns a MagicMock paradigm
  - RenderLoop         : patched so no graphics operations run
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch, call
from types import SimpleNamespace

import asyncio
import numpy as np
import pytest

from omnibci.infrastructure.stimulation.stimulator import Stimulator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stimulator():
    """Return a Stimulator with all IO mocked, ready to call start_async()."""
    s = Stimulator(settings_snapshot={"STIMULATION.renderer": "moderngl"})
    mock_renderer  = MagicMock()
    mock_paradigm  = MagicMock()
    mock_trigger   = MagicMock()
    mock_loop      = MagicMock()
    mock_loop.run_async = AsyncMock()
    for obj, methods in ((mock_renderer, ("initialize", "shutdown")),
                         (mock_trigger, ("setup", "clear", "teardown")),
                         (mock_paradigm, ("setup", "teardown"))):
        for method in methods:
            getattr(obj, method).return_value = None

    s._load_trigger  = MagicMock(return_value=mock_trigger)
    s._load_paradigm = MagicMock(return_value=mock_paradigm)

    return s, mock_renderer, mock_paradigm, mock_trigger, mock_loop


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestStimulatorStart:
    def test_creates_renderer_from_config(self):
        s, mock_renderer, _, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ) as mock_create, patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            asyncio.run(s.start_async())
        # config says "moderngl"
        mock_create.assert_called_once_with("moderngl")

    def test_renderer_initialize_called(self):
        s, mock_renderer, _, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            asyncio.run(s.start_async())
        mock_renderer.initialize.assert_called_once_with(
            width=1920, height=1080, title="OmniBCI Stimulation"
        )

    def test_paradigm_setup_receives_both_capabilities(self):
        s, mock_renderer, mock_paradigm, mock_trigger, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            asyncio.run(s.start_async())
        mock_paradigm.setup.assert_called_once_with(renderer=mock_renderer, trigger=mock_trigger)

    def test_render_loop_started(self):
        s, mock_renderer, mock_paradigm, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ) as MockLoop:
            asyncio.run(s.start_async())
        MockLoop.assert_called_once_with(mock_renderer, mock_paradigm)
        mock_loop.run_async.assert_awaited_once()

    def test_trigger_loaded_before_paradigm(self):
        s, mock_renderer, mock_paradigm, mock_trigger, mock_loop = _make_stimulator()
        call_order = []
        s._load_trigger  = MagicMock(side_effect=lambda: call_order.append("trigger") or mock_trigger)
        s._load_paradigm = MagicMock(side_effect=lambda: call_order.append("paradigm") or mock_paradigm)
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            asyncio.run(s.start_async())
        assert call_order == ["trigger", "paradigm"]


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStimulatorStop:
    def test_request_stop_signals_without_discarding_loop(self):
        s = Stimulator(settings_snapshot={})
        loop = MagicMock()
        s._loop = loop
        s.request_stop()
        loop.request_stop.assert_called_once_with()
        assert s._loop is loop

    def test_request_stop_without_start_is_noop(self):
        Stimulator(settings_snapshot={}).request_stop()


# ---------------------------------------------------------------------------
# present_trial / present_result
# ---------------------------------------------------------------------------

class TestStimulatorPresent:
    def _started(self):
        s = Stimulator(settings_snapshot={})
        s.paradigm = MagicMock()
        s.paradigm.present_trial.return_value = None
        s.paradigm.present_result.return_value = None
        s._payload_schemas = {name: {"type": "object", "properties": {key: {"type": "integer"}},
                                    "required": [key], "additionalProperties": False}
                              for name, key in (("trial", "event_id"), ("result", "target_id"))}
        return s, s.paradigm

    def test_present_trial_delegates(self):
        s, mock_paradigm = self._started()
        s.present_trial({"event_id": 5})
        mock_paradigm.present_trial.assert_called_once_with({"event_id": 5})

    def test_present_result_delegates(self):
        s, mock_paradigm = self._started()
        s.present_result({"target_id": 3})
        mock_paradigm.present_result.assert_called_once_with({"target_id": 3})

    def test_present_trial_requires_paradigm(self):
        s = Stimulator(settings_snapshot={})
        with pytest.raises(RuntimeError, match="not been initialized"):
            s.present_trial(0)

    def test_present_result_requires_paradigm(self):
        s = Stimulator(settings_snapshot={})
        with pytest.raises(RuntimeError, match="not been initialized"):
            s.present_result(0)


class TestStimulatorReadiness:
    @pytest.mark.parametrize("presenting", [False, True])
    def test_returns_required_boolean_without_coercion(self, presenting):
        s = Stimulator(settings_snapshot={})
        s.paradigm = MagicMock()
        s.paradigm.is_presenting.return_value = presenting

        assert s.is_presenting is presenting
        s.paradigm.is_presenting.assert_called_once_with()

    def test_uninitialized_paradigm_is_not_ready(self):
        s = Stimulator(settings_snapshot={})
        with pytest.raises(RuntimeError, match="not been initialized"):
            _ = s.is_presenting

    def test_missing_readiness_method_is_not_ready(self):
        s = Stimulator(settings_snapshot={})
        s.paradigm = SimpleNamespace()
        with pytest.raises(AttributeError, match="is_presenting"):
            _ = s.is_presenting

    @pytest.mark.parametrize("presenting", [
        None, 0, 1, "", "busy", [], object(), np.bool_(False), np.bool_(True),
    ])
    def test_rejects_non_boolean_readiness(self, presenting):
        s = Stimulator(settings_snapshot={})
        s.paradigm = MagicMock()
        s.paradigm.is_presenting.return_value = presenting

        with pytest.raises(TypeError, match="is_presenting.*bool"):
            _ = s.is_presenting

    def test_propagates_readiness_error(self):
        s = Stimulator(settings_snapshot={})
        error = RuntimeError("readiness failed")
        s.paradigm = MagicMock()
        s.paradigm.is_presenting.side_effect = error

        with pytest.raises(RuntimeError) as raised:
            _ = s.is_presenting
        assert raised.value is error


# ---------------------------------------------------------------------------
# _create_renderer
# ---------------------------------------------------------------------------

class TestCreateRenderer:
    def test_unknown_renderer_raises(self):
        from omnibci.infrastructure.stimulation.stimulator import _create_renderer
        with pytest.raises(ValueError, match="Unknown renderer"):
            _create_renderer("vulkan")

    def test_known_renderer_returns_instance(self):
        from omnibci.infrastructure.stimulation.stimulator import _create_renderer
        FakeCls = MagicMock(return_value=MagicMock())
        with patch.dict(
            "omnibci.infrastructure.stimulation.stimulator._RENDERER_REGISTRY",
            {"fake": "some.module.FakeCls"},
        ), patch("importlib.import_module") as mock_import:
            mock_import.return_value = MagicMock(FakeCls=FakeCls)
            _create_renderer("fake")
            mock_import.assert_called_once_with("some.module")
