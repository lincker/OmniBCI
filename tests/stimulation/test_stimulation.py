"""
Unit tests for Stimulator.

All external dependencies are mocked:
  - _create_renderer   : returns a MagicMock renderer
  - _load_trigger      : returns a MagicMock trigger
  - _load_paradigm     : returns a MagicMock paradigm
  - RenderLoop         : patched so no real thread spawns
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from omnibci.infrastructure.stimulation.stimulator import Stimulator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stimulator():
    """Return a Stimulator with all IO mocked, ready to call start()."""
    s = Stimulator()
    mock_renderer  = MagicMock()
    mock_paradigm  = MagicMock()
    mock_trigger   = MagicMock()
    mock_loop      = MagicMock()

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
            s.start()
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
            s.start()
        mock_renderer.initialize.assert_called_once_with(
            width=1920, height=1080, title="OmniBCI Stimulation"
        )

    def test_paradigm_setup_receives_renderer(self):
        s, mock_renderer, mock_paradigm, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            s.start()
        mock_paradigm.setup.assert_called_once_with(renderer=mock_renderer)

    def test_render_loop_started(self):
        s, mock_renderer, mock_paradigm, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ) as MockLoop:
            s.start()
        MockLoop.assert_called_once_with(mock_renderer, mock_paradigm)
        mock_loop.start.assert_called_once()

    def test_trigger_loaded_before_paradigm(self):
        s, mock_renderer, _, mock_trigger, mock_loop = _make_stimulator()
        call_order = []
        s._load_trigger  = MagicMock(side_effect=lambda: call_order.append("trigger") or mock_trigger)
        s._load_paradigm = MagicMock(side_effect=lambda t: call_order.append("paradigm") or MagicMock())
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            s.start()
        assert call_order == ["trigger", "paradigm"]


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStimulatorStop:
    def _started(self):
        s, mock_renderer, mock_paradigm, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            s.start()
        return s, mock_loop

    def test_stop_calls_loop_stop(self):
        s, mock_loop = self._started()
        s.stop()
        mock_loop.stop.assert_called_once()

    def test_stop_clears_loop_reference(self):
        s, _ = self._started()
        s.stop()
        assert s._loop is None

    def test_stop_without_start_is_noop(self):
        s = Stimulator()
        s.stop()   # should not raise


# ---------------------------------------------------------------------------
# present_trial / present_result
# ---------------------------------------------------------------------------

class TestStimulatorPresent:
    def _started(self):
        s, mock_renderer, mock_paradigm, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            s.start()
        return s, mock_paradigm

    def test_present_trial_delegates(self):
        s, mock_paradigm = self._started()
        s.present_trial(5)
        mock_paradigm.trial_stimulus_present.assert_called_once_with(5)

    def test_present_result_delegates(self):
        s, mock_paradigm = self._started()
        s.present_result(3)
        mock_paradigm.trial_result_present.assert_called_once_with(result=3)

    def test_present_trial_noop_without_paradigm(self):
        s = Stimulator()
        s.present_trial(0)   # should not raise

    def test_present_result_noop_without_paradigm(self):
        s = Stimulator()
        s.present_result(0)  # should not raise


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
