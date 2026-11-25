"""Ten-command SSVEP driven by a fixed 60 Hz display and monotonic elapsed time.

The host validates the 60 Hz display mode and serializes setup, trial/result
callbacks, rendering and teardown on the renderer owner thread. Only small
label textures are uploaded; stimulus luminance and phase deadlines follow
monotonic ``elapsed_seconds`` without extending missed intervals.
"""
from collections.abc import Mapping
from enum import Enum, auto
import logging
import math
from pathlib import Path

import numpy as np
from PIL import Image

from omnibci.sdk import Color, Paradigm, Rect, RenderSurface, TextureHandle, TriggerOutput
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
    required_refresh_rate_hz = float(REFRESH_RATE_HZ)

    def __init__(self, *, parameters: Mapping[str, object], resource_root: Path) -> None:
        if not isinstance(parameters, Mapping) or parameters:
            raise ValueError("Command10 uses a fixed protocol and accepts empty parameters only")
        self._resource_root = resource_root
        self._renderer: RenderSurface | None = None
        self._trigger: TriggerOutput | None = None
        self._textures: dict[int, TextureHandle] = {}
        self._ready = False
        self._state = _State.IDLE
        self._trial_start_time: float | None = None
        self._last_trial_slot = -1
        self._cue_frames = 0
        self._missed_frames = 0
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
        self._trial_start_time = None
        self._last_trial_slot = -1
        self._cue_frames = 0
        self._missed_frames = 0
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
        self._trial_start_time = None
        self._last_trial_slot = -1
        self._cue_frames = CUE_FRAMES if target_id else 0
        self._missed_frames = 0
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

    @property
    def missed_frames(self) -> int:
        """Number of omitted stimulus slots observed during the latest trial."""
        return self._missed_frames

    def is_presenting(self) -> bool:
        return self._state is not _State.IDLE

    def render_frame(self, t: float = 0.0) -> None:
        if not self._ready:
            return
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise TypeError("elapsed_seconds must be a real number")
        t = float(t)
        if not math.isfinite(t) or t < 0:
            raise ValueError("elapsed_seconds must be finite and nonnegative")

        stim_idx = 0
        if self._state is not _State.IDLE:
            if self._trial_start_time is None:
                self._trial_start_time = t
            trial_slot = math.floor((t - self._trial_start_time) * REFRESH_RATE_HZ + 0.5)
            if trial_slot <= self._last_trial_slot:
                return
            if self._state in (_State.CUE, _State.STIM) and self._last_trial_slot >= 0 and trial_slot > self._last_trial_slot + 1:
                missed_start = max(self._last_trial_slot + 1, self._cue_frames)
                missed_end = min(trial_slot, self._cue_frames + STIM_FRAMES)
                if missed_end > missed_start:
                    self._missed_frames += missed_end - missed_start
            self._last_trial_slot = trial_slot

            if self._state is _State.CUE and trial_slot >= self._cue_frames:
                self._state = _State.STIM

            if self._state is _State.STIM:
                stim_idx = trial_slot - self._cue_frames
                if stim_idx >= STIM_FRAMES:
                    if self._missed_frames:
                        logging.warning(
                            "Command10 stimulus missed %d of %d frames",
                            self._missed_frames,
                            STIM_FRAMES,
                        )
                    if self._marker_sent:
                        self._trigger.clear()
                    else:
                        logging.warning("Command10 stimulus interval elapsed before its start marker")
                    self._state = _State.RESULT
                elif not self._marker_sent:
                    if stim_idx > 0:
                        logging.warning("Command10 start marker delayed by %d frames", stim_idx)
                    # A request before drawing is not a physical presentation receipt.
                    self._trigger.send(self._cue_target_id or FREE_CHOICE_EVENT_ID)
                    self._marker_sent = True

            if self._state is _State.RESULT and trial_slot >= self._cue_frames + STIM_FRAMES + RESULT_FRAMES:
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
                    2 * math.pi * target.frequency_hz * stim_idx / REFRESH_RATE_HZ + target.phase_rad,
                )
                color = Color(brightness, brightness, brightness)
            renderer.draw_rect(_rect(cx, cy, 200, 200), color)
            renderer.draw_texture(self._textures[target.target_id], _rect(cx, cy, 128, 128))

        renderer.draw_texture(self._textures[0], _FEEDBACK_CAPTION)
        if self._state in (_State.IDLE, _State.RESULT) and self._result_target_id is not None:
            renderer.draw_texture(self._textures[self._result_target_id], _FEEDBACK_TARGET)
        else:
            renderer.draw_rect(_FEEDBACK_EMPTY, _TEXT_COLOR)
