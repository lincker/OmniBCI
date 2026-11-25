#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Classic 40-target SSVEP paradigm — RenderSurface-protocol edition.

Rendering is driven by an external RenderLoop that calls ``render_frame``
once per VSync.  Trial control methods (``present_trial``,
``present_result``) run on the renderer owner thread.
"""
from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
import logging
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

_CUE_FRAMES    = 60   # ~1 s at 60 Hz
_RESULT_FRAMES = 90   # ~1.5 s feedback display


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
        self._cue_countdown    = 0
        self._stim_frame_idx   = 0
        self._result_target_id = 0
        self._result_countdown = 0

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
            self._cue_countdown = 0
            self._stim_frame_idx = 0
            self._result_target_id = 0
            self._result_countdown = 0

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

    # ── RenderLoop interface ───────────────────────────────────────────────

    def render_frame(self, t: float = 0.0) -> None:
        """Draw current frame.  Called by RenderLoop on the GL thread."""
        r = self._renderer
        if r is None:
            return

        r.clear()

        with self._lock:
            if self._state == _State.IDLE:
                self._draw_idle(r)

            elif self._state == _State.CUE:
                r.draw_texture(self._cue_textures[self._current_event-1], _STIM_RECT)
                self._text_view.draw(r)
                self._cue_countdown -= 1
                if self._cue_countdown <= 0:
                    # CUE -> STIM: send EEG trigger
                    self.trigger.clear()
                    self.trigger.send(self._current_event)
                    self._stim_frame_idx = 0
                    self._state = _State.STIM

            elif self._state == _State.STIM:
                if self._stim_frame_idx < len(self._stim_textures):
                    r.draw_texture(
                        self._stim_textures[self._stim_frame_idx], _STIM_RECT
                    )
                    self._text_view.draw(r)
                    self._stim_frame_idx += 1
                else:
                    # All frames shown — clear trigger and return to IDLE
                    self.trigger.clear()
                    self._state = _State.IDLE
                    self._draw_idle(r)

            elif self._state == _State.RESULT:
                r.draw_texture(
                    self._result_textures[self._result_target_id - 1], _STIM_RECT
                )
                self._text_view.draw(r)
                self._result_countdown -= 1
                if self._result_countdown <= 0:
                    self._result_target_id = 0
                    self._state = _State.IDLE

    # ── Trial control (application thread) ────────────────────────────────

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
            if 0 < event_id <= _N_TARGETS:
                self._cue_countdown = _CUE_FRAMES
                self._state         = _State.CUE
            else:
                # No cue — go straight to STIM
                self.trigger.clear()
                self.trigger.send(event_id)
                self._stim_frame_idx = 0
                self._state          = _State.STIM

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
            self._result_countdown = _RESULT_FRAMES
            self._state            = _State.RESULT

    # ── Private ────────────────────────────────────────────────────────────

    def _draw_idle(self, r: RenderSurface) -> None:
        """Draw initial frame + text. Caller must hold _lock."""
        r.draw_texture(self._init_tex, _STIM_RECT)
        self._text_view.draw(r)

    def _parse_feedback(self, target_id: int) -> str:
        # target_id is 1-based (1..40), matching the decoders' result+1 output.
        # The char table stays 0-based, so index with target_id - 1.
        if not (0 < target_id <= _N_TARGETS):
            logging.error(f'Invalid feedback target_id: {target_id}')
            raise IndexError(target_id)
        return self._stim_target_text[target_id - 1]
