"""Ten-command SSVEP driven by the host's fixed 60 Hz display timeline.

The host serializes setup, trial/result callbacks, rendering and teardown on
the renderer owner thread. Only small label textures are uploaded; stimulus
luminance is selected from refresh indices, including gaps between callbacks.
"""
from collections.abc import Mapping
from enum import Enum, auto
import math
from pathlib import Path

import numpy as np
from PIL import Image

from omnibci.sdk import Color, FrameTiming, Paradigm, Rect, RenderSurface, TextureHandle, TriggerOutput
from .targets import CUE_FRAMES, FREE_CHOICE_EVENT_ID, REFRESH_RATE_HZ, RESULT_FRAMES, STIM_FRAMES, TARGETS


_BACKGROUND = Color(.06, .07, .09)
_IDLE_TARGET = Color(.18, .20, .23)
_CUE_COLOR = Color(1.0, 0.0, 0.0)
_RESULT_COLOR = Color(1.0, 0.0, 0.0)
_TEXT_COLOR = Color(.85, .85, .85)


def _rect(cx: float, cy: float, width: float, height: float) -> Rect:
    """Reference pixels in the host's 1920 x 1080 window to NDC."""
    return Rect(x=(cx - width / 2) / 960, y=(cy - height / 2) / 540,
                w=width / 960, h=height / 540)


_FEEDBACK_CAPTION = _rect(0, 70, 160, 40)
_FEEDBACK_TARGET = _rect(0, -20, 100, 100)
_FEEDBACK_EMPTY = _rect(0, -20, 28, 3)


class _State(Enum):
    IDLE = auto()
    CUE = auto()
    STIM = auto()
    RESULT = auto()


def _payload_id(payload: Mapping[str, object], key: str, minimum: int) -> int:
    if (not isinstance(payload, Mapping) or set(payload) != {key}
            or type(payload[key]) is not int or not minimum <= payload[key] <= 10):
        raise ValueError(f"Expected exactly {key} as an integer in {minimum}..10")
    return payload[key]


class Command10SSVEP(Paradigm):

    def __init__(self, *, parameters: Mapping[str, object], resource_root: Path) -> None:
        if not isinstance(parameters, Mapping) or parameters:
            raise ValueError("Command10 uses a fixed protocol and accepts empty parameters only")
        self._resource_root = resource_root
        self._renderer: RenderSurface | None = None
        self._trigger: TriggerOutput | None = None
        self._textures: dict[int, TextureHandle] = {}
        self._ready = False
        self._state = _State.IDLE
        self._phase_start_refresh: int | None = None
        self._last_refresh = -1
        self._cue_target_id = 0
        self._result_target_id: int | None = None
        self._marker_sent = False

    def setup(self, *, renderer: RenderSurface, trigger: TriggerOutput) -> None:
        if self._renderer is not None:
            raise RuntimeError("Command10 is already initialized; finish teardown before setup")
        self._renderer, self._trigger = renderer, trigger
        for target_id in range(11):
            name = 'feedback.png' if target_id == 0 else f'target-{target_id:02}.png'
            with Image.open(self._resource_root / 'resource' / name) as source:
                pixels = np.asarray(source.convert('RGBA'), dtype=np.uint8)
                # PNGs use a top-left origin; the drawing surface uses bottom-left.
                pixels = np.ascontiguousarray(pixels[::-1])
            self._textures[target_id] = renderer.upload_texture(pixels)
        self._state = _State.IDLE
        self._phase_start_refresh = None
        self._last_refresh = -1
        self._result_target_id = None
        self._marker_sent = False
        self._ready = True

    def teardown(self) -> None:
        """Release every owned texture; the host clears and releases its trigger."""
        self._ready = False
        self._state = _State.IDLE
        if self._renderer is None:
            return
        errors = []
        for target_id, handle in tuple(self._textures.items()):
            try:
                self._renderer.release_texture(handle)
            except Exception as exc:
                errors.append(exc)
            else:
                del self._textures[target_id]
        if errors:
            raise ExceptionGroup("Command10 texture cleanup failed", errors)
        self._renderer = self._trigger = None

    def _require_ready(self) -> None:
        if not self._ready:
            raise RuntimeError("Command10 requires successful setup")

    def present_trial(self, parameters: Mapping[str, object]) -> None:
        target_id = _payload_id(parameters, 'cue_target_id', 0)
        self._require_ready()
        if self.is_presenting():
            raise RuntimeError("Command10 already has an active trial")
        # Driver clearing may block: do it before the new display interval starts.
        self._trigger.clear()
        self._cue_target_id = target_id
        self._result_target_id = None
        self._marker_sent = False
        self._phase_start_refresh = None
        self._state = _State.CUE if target_id else _State.STIM

    def present_result(self, result: Mapping[str, object]) -> None:
        target_id = _payload_id(result, 'target_id', 1)
        self._require_ready()
        if self._state not in (_State.STIM, _State.RESULT) or not self._marker_sent:
            raise ValueError("Result requires an active trial whose stimulation has started")
        if self._result_target_id is not None:
            raise ValueError("This trial already has a result")
        # Keep the four-second stimulus interval even when decoding finishes early.
        self._result_target_id = target_id

    def is_presenting(self) -> bool:
        return self._state is not _State.IDLE

    def render_frame(self, frame: FrameTiming) -> bool:
        if not self._ready:
            return False
        if not math.isclose(1 / frame.refresh_period, REFRESH_RATE_HZ, rel_tol=0, abs_tol=.01):
            raise ValueError("Command10 requires a fixed 60 Hz display mode")
        if frame.refresh_index <= self._last_refresh:
            return False
        self._last_refresh = frame.refresh_index
        if self._phase_start_refresh is None:
            self._phase_start_refresh = frame.refresh_index
        elapsed = frame.refresh_index - self._phase_start_refresh

        if self._state is _State.CUE and elapsed >= CUE_FRAMES:
            self._phase_start_refresh += CUE_FRAMES
            elapsed -= CUE_FRAMES
            self._state = _State.STIM

        if self._state is _State.STIM:
            if elapsed >= STIM_FRAMES:
                if self._marker_sent:
                    self._trigger.clear()
                self._phase_start_refresh += STIM_FRAMES
                elapsed -= STIM_FRAMES
                self._state = _State.RESULT
            elif not self._marker_sent:
                # A request before drawing is not a physical presentation receipt.
                self._trigger.send(self._cue_target_id or FREE_CHOICE_EVENT_ID)
                self._marker_sent = True

        if self._state is _State.RESULT and elapsed >= RESULT_FRAMES:
            self._state = _State.IDLE

        renderer = self._renderer
        renderer.clear(_BACKGROUND)
        for target in TARGETS:
            cx, cy = (target.column - 3) * 360, (2 - target.row) * 300
            if self._state is _State.CUE and target.target_id == self._cue_target_id:
                renderer.draw_rect(_rect(cx, cy, 214, 214), _CUE_COLOR)
            color = _IDLE_TARGET
            if self._state is _State.RESULT and target.target_id == self._result_target_id:
                color = _RESULT_COLOR
            elif self._state is _State.STIM:
                brightness = .5 + .5 * math.sin(
                    2 * math.pi * target.frequency_hz * elapsed / REFRESH_RATE_HZ + target.phase_rad,
                )
                color = Color(brightness, brightness, brightness)
            renderer.draw_rect(_rect(cx, cy, 200, 200), color)
            renderer.draw_texture(self._textures[target.target_id], _rect(cx, cy, 128, 128))

        renderer.draw_texture(self._textures[0], _FEEDBACK_CAPTION)
        if self._state in (_State.IDLE, _State.RESULT) and self._result_target_id is not None:
            renderer.draw_texture(self._textures[self._result_target_id], _FEEDBACK_TARGET)
        else:
            renderer.draw_rect(_FEEDBACK_EMPTY, _TEXT_COLOR)
        return True
