#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Classic 40-target SSVEP paradigm — RenderSurface-protocol edition.

Rendering is driven by an external RenderLoop that calls ``render_frame``
with monotonic elapsed seconds.  Trial control methods (``present_trial``,
``present_result``) run on the renderer owner thread.
"""
from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
import logging
import math
import string
import threading
from enum import Enum, auto

from omnibci.sdk import Paradigm, Rect, RenderSurface, TextureHandle, TriggerOutput
from .feedback_display import FeedbackDisplay
from .frame_pack import FramePack


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WIN_W, _WIN_H = 1920, 1080
_PACK_NAME = "classic40_ssvep.ofp"

# Targets are numbered 1-based (1..40); 0 is reserved as the trigger baseline
# (Trigger.clear() sends 0).  Internal arrays (textures, char table) stay 0-based
# and are indexed with target_id - 1.  This matches the decoders, which already
# emit 1-based ids (result + 1).
_N_TARGETS     = 40

_REFRESH_RATE_HZ = 60
_CUE_FRAMES      = 60   # 1 s at 60 Hz
_RESULT_FRAMES   = 90   # 1.5 s feedback display


def _frame_pack_path(resource_root: Path) -> Path:
    pack = resource_root / "resource" / _PACK_NAME
    if not pack.is_file():
        raise FileNotFoundError(f"Missing classic40SSVEP frame pack: {_PACK_NAME}")
    return pack


def _center_rect(cx_px: int, cy_px: int, w_px: int, h_px: int) -> Rect:
    """Pixel center (y-up from screen center) → NDC Rect (bottom-left origin)."""
    cx = (cx_px / _WIN_W) * 2
    cy = (cy_px / _WIN_H) * 2
    hw = w_px / _WIN_W
    hh = h_px / _WIN_H
    return Rect(x=cx - hw, y=cy - hh, w=hw * 2, h=hh * 2)


# Preserve the 1920×990 frame pack's aspect ratio below the 90-pixel feedback area.
_STIM_RECT: Rect = _center_rect(0, -45, _WIN_W, 990)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class _State(Enum):
    IDLE   = auto()   # showing initial frame
    CUE    = auto()   # showing cue overlay (~1 s)
    STIM   = auto()   # playing pre-generated frames
    RESULT = auto()   # showing feedback result (~1.5 s)


# ---------------------------------------------------------------------------
# Paradigm implementation
# ---------------------------------------------------------------------------

class Classic40SSVEP_2(Paradigm):
    required_refresh_rate_hz = float(_REFRESH_RATE_HZ)

    def __init__(
        self, *, parameters: Mapping[str, object], resource_root: Path,
    ) -> None:
        self._resource_root = resource_root
        self.trigger: TriggerOutput | None = None
        self._renderer: RenderSurface | None = None

        # GPU texture handles (populated in setup)
        self._init_tex: TextureHandle | None     = None
        self._stim_textures: list[TextureHandle] = []
        self._cue_textures:  list[TextureHandle] = []
        self._result_textures: list[TextureHandle] = []

        # Text feedback widget
        self._text_view = FeedbackDisplay(pos_x=0, pos_y=495)

        # Feedback character set: A-Z, 0-9, '.', ' ', ',', 'Del'
        self._stim_target_text: list[str] = (
            list(string.ascii_uppercase + string.digits) + ['.', ' ', ',', 'Del']
        )

        # State machine — all fields mutated only while holding _lock
        self._lock             = threading.Lock()
        self._state            = _State.IDLE
        self._current_event    = 0
        self._phase_start_time: float | None = None
        self._last_slot        = -1
        self._cue_frames       = 0
        self._marker_sent      = False
        self._missed_frames    = 0
        self._result_target_id = 0
        self._last_drawn_tex: TextureHandle | None = None

    # ── Paradigm port ──────────────────────────────────────────────────────

    def setup(self, *, renderer: RenderSurface, trigger: TriggerOutput) -> None:
        """Borrow host capabilities and retain every uploaded texture."""
        self._renderer = renderer
        self.trigger = trigger

        with FramePack(_frame_pack_path(self._resource_root)) as pack:
            self._init_tex = renderer.upload_texture(pack.idle_frame)
            for frame in pack.iter_stimulation_frames():
                self._stim_textures.append(renderer.upload_texture(frame))
            for frame in pack.iter_cue_frames():
                self._cue_textures.append(renderer.upload_texture(frame))
            for frame in pack.iter_result_frames():
                self._result_textures.append(renderer.upload_texture(frame))

        # Text widget — uploads blank texture
        self._text_view.setup(renderer)

        with self._lock:
            self._state = _State.IDLE
            self._phase_start_time = None
            self._last_slot = -1
            self._cue_frames = 0
            self._marker_sent = False
            self._missed_frames = 0
            self._result_target_id = 0
            self._last_drawn_tex = self._init_tex

    def teardown(self) -> None:
        """Attempt every safe release, including after partial texture upload."""
        r = self._renderer
        if r is None:
            return
        errors = []
        try:
            self._text_view.teardown(r)
        except Exception as exc:
            errors.append(exc)
        for textures in (self._stim_textures, self._cue_textures, self._result_textures):
            for tex in tuple(textures):
                try:
                    r.release_texture(tex)
                except Exception as exc:
                    errors.append(exc)
                else:
                    textures.remove(tex)
        if self._init_tex is not None:
            try:
                r.release_texture(self._init_tex)
            except Exception as exc:
                errors.append(exc)
            else:
                self._init_tex = None
        if errors:
            raise ExceptionGroup("Paradigm texture cleanup failed", errors)
        self._renderer = None
        self.trigger = None
        self._last_drawn_tex = None

    # ── RenderLoop interface ───────────────────────────────────────────────

    def render_frame(self, t: float = 0.0) -> None:
        """Draw current frame using monotonic *t* without extending missed trials."""
        r = self._renderer
        if r is None:
            return
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise TypeError("elapsed_seconds must be a real number")
        t = float(t)
        if not math.isfinite(t) or t < 0:
            raise ValueError("elapsed_seconds must be finite and nonnegative")

        with self._lock:
            if self._state == _State.IDLE:
                self._last_drawn_tex = self._init_tex
                self._draw_frame(r, self._init_tex)
                return

            if self._phase_start_time is None:
                self._phase_start_time = t
            slot = math.floor((t - self._phase_start_time) * _REFRESH_RATE_HZ + 0.5)
            if slot <= self._last_slot:
                # Redraw the current slot's image so the unconditional swap never
                # presents a stale or undefined back buffer, without advancing state.
                self._draw_frame(r, self._last_drawn_tex or self._init_tex)
                return
            if self._state in (_State.CUE, _State.STIM) and self._last_slot >= 0 and slot > self._last_slot + 1:
                missed_start = max(self._last_slot + 1, self._cue_frames)
                missed_end = min(slot, self._cue_frames + len(self._stim_textures))
                if missed_end > missed_start:
                    self._missed_frames += missed_end - missed_start
            self._last_slot = slot

            if self._state == _State.CUE and slot >= self._cue_frames:
                self._state = _State.STIM
                stim_idx = slot - self._cue_frames
                if stim_idx < len(self._stim_textures):
                    logging.warning("Classic40 start marker delayed by %d frames", stim_idx + 1)
                    self.trigger.clear()
                    self.trigger.send(self._current_event)
                    self._marker_sent = True
                else:
                    logging.warning("Classic40 stimulus interval elapsed before its start marker")

            tex = self._init_tex
            if self._state == _State.CUE:
                tex = self._cue_textures[self._current_event - 1]
                if slot == self._cue_frames - 1:
                    # Preserve last-cue marker placement on nominal 60 Hz pacing.
                    self.trigger.clear()
                    self.trigger.send(self._current_event)
                    self._marker_sent = True
                    self._state = _State.STIM

            elif self._state == _State.STIM:
                stim_idx = slot - self._cue_frames
                if stim_idx < len(self._stim_textures):
                    tex = self._stim_textures[stim_idx]
                else:
                    if self._missed_frames:
                        logging.warning(
                            "Classic40 stimulus missed %d of %d frames",
                            self._missed_frames,
                            len(self._stim_textures),
                        )
                    if self._marker_sent:
                        self.trigger.clear()
                    self._state = _State.IDLE

            elif self._state == _State.RESULT:
                if slot < _RESULT_FRAMES:
                    tex = self._result_textures[self._result_target_id - 1]
                if slot >= _RESULT_FRAMES - 1:
                    self._result_target_id = 0
                    self._state = _State.IDLE

            self._last_drawn_tex = tex
            self._draw_frame(r, tex)

    # ── Trial control (application thread) ────────────────────────────────

    @property
    def missed_frames(self) -> int:
        """Number of omitted stimulus slots observed during the latest trial."""
        with self._lock:
            return self._missed_frames

    def is_presenting(self) -> bool:
        """True while a trial is mid-sequence (cue/stim/result) — not IDLE.

        Lets the command loop hold the next queued trial until this one finishes,
        so ``n_trials`` produces that many sequential trials instead of one.
        """
        with self._lock:
            return self._state is not _State.IDLE

    def present_trial(self, parameters: Mapping[str, object]) -> None:
        """Begin a cued trial (1..40) or an explicit free run (0)."""
        if (not isinstance(parameters, Mapping) or set(parameters) != {"event_id"}
                or type(parameters["event_id"]) is not int or not 0 <= parameters["event_id"] <= _N_TARGETS):
            raise ValueError("Trial requires event_id as an integer in 0..40")
        event_id = parameters["event_id"]
        with self._lock:
            self._current_event = event_id
            self._result_target_id = 0
            self._phase_start_time = None
            self._last_slot = -1
            self._missed_frames = 0
            if 0 < event_id <= _N_TARGETS:
                self._cue_frames = _CUE_FRAMES
                self._marker_sent = False
                self._state = _State.CUE
            else:
                # No cue — go straight to STIM
                self.trigger.clear()
                self.trigger.send(event_id)
                self._cue_frames = 0
                self._marker_sent = True
                self._state = _State.STIM

    def present_result(self, result: Mapping[str, object]) -> None:
        """Update feedback for the explicitly selected target."""
        if (not isinstance(result, Mapping) or set(result) != {"target_id"}
                or type(result["target_id"]) is not int or not 1 <= result["target_id"] <= _N_TARGETS):
            raise ValueError("Result requires target_id as an integer in 1..40")
        target_id = result["target_id"]
        text = self._parse_feedback(target_id)
        logging.info(f"feedback target_id={target_id!r}, char={text!r}")
        with self._lock:
            if text == 'Del':
                self._text_view.remove_last()
            else:
                self._text_view.add_text(text)
            self._result_target_id = target_id
            self._phase_start_time = None
            self._last_slot = -1
            self._state = _State.RESULT

    # ── Private ────────────────────────────────────────────────────────────

    def _draw_frame(self, r: RenderSurface, tex: TextureHandle) -> None:
        """Clear and draw the selected texture + text view. Caller must hold _lock."""
        r.clear()
        r.draw_texture(tex, _STIM_RECT)
        self._text_view.draw(r)

    def _parse_feedback(self, target_id: int) -> str:
        # target_id is 1-based (1..40), matching the decoders' result+1 output.
        # The char table stays 0-based, so index with target_id - 1.
        if not (0 < target_id <= _N_TARGETS):
            logging.error(f'Invalid feedback target_id: {target_id}')
            raise IndexError(target_id)
        return self._stim_target_text[target_id - 1]
