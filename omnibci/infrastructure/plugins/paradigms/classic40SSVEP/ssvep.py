#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Classic 40-target SSVEP paradigm — Renderer-protocol edition.

Rendering is driven by an external RenderLoop that calls ``render_frame``
once per VSync.  Trial control methods (``trial_stimulus_present``,
``trial_result_present``) only mutate state and are safe to call from any
thread.
"""
from __future__ import annotations

import logging
import string
import threading
from enum import Enum, auto
from importlib import resources

from omnibci.domain.ports.paradigm import Paradigm
from omnibci.sdk import PluginContext, PluginParameters, Rect, Renderer, TextureHandle
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


def _frame_pack_path():
    pack = resources.files(__package__).joinpath("resource").joinpath(_PACK_NAME)
    if not pack.is_file():
        raise FileNotFoundError(f"Missing classic40SSVEP frame pack: {_PACK_NAME}")
    return resources.as_file(pack)


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
    plugin_type = 'STIMULATION_THUBCI_SSVEP_classic40'

    def __init__(self, trigger=None, *, parameters: PluginParameters | None = None,
                 context: PluginContext | None = None):
        self.trigger = trigger
        self._renderer: Renderer | None = None

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

    def setup(self, renderer: Renderer = None) -> None:  # type: ignore[override]
        """Upload all textures to GPU."""
        if renderer is None:
            return
        self._renderer = renderer

        with _frame_pack_path() as pack_path, FramePack(pack_path) as pack:
            self._init_tex = renderer.upload_texture(pack.idle_frame)
            self._stim_textures = [
                renderer.upload_texture(frame)
                for frame in pack.iter_stimulation_frames()
            ]
            self._cue_textures = [
                renderer.upload_texture(frame) for frame in pack.iter_cue_frames()
            ]
            self._result_textures = [
                renderer.upload_texture(frame) for frame in pack.iter_result_frames()
            ]

        # Text widget — uploads blank texture
        self._text_view.setup(renderer)

        with self._lock:
            self._state = _State.IDLE
            self._result_target_id = 0

    def teardown(self, renderer: Renderer = None) -> None:  # type: ignore[override]
        """Release all GPU textures."""
        r = renderer or self._renderer
        if r is None:
            return
        self._text_view.teardown(r)
        for tex in self._stim_textures:
            r.release_texture(tex)
        for tex in self._cue_textures:
            r.release_texture(tex)
        for tex in self._result_textures:
            r.release_texture(tex)
        if self._init_tex is not None:
            r.release_texture(self._init_tex)
        self._stim_textures.clear()
        self._cue_textures.clear()
        self._result_textures.clear()
        self._renderer = None

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

    def trial_stimulus_present(self, params=None) -> None:
        """Begin cue->stim sequence.  Thread-safe.

        This paradigm interprets *params* as the target/event id: an int in
        1..40 → cued trial for that target; anything else / None → no-cue
        free run (default target 0).
        """
        event_id = params if isinstance(params, int) else 0
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

    def trial_result_present(self, result=None) -> None:
        """Update the result target and feedback text for ~1.5 s.  Thread-safe."""
        text = self._parse_feedback(result)
        logging.info(f"feedback result={result!r}, char={text!r}")
        with self._lock:
            if text == 'Del':
                self._text_view.remove_last()
            else:
                self._text_view.add_text(text)
            self._result_target_id = result
            self._result_countdown = _RESULT_FRAMES
            self._state            = _State.RESULT

    # ── Private ────────────────────────────────────────────────────────────

    def _draw_idle(self, r: Renderer) -> None:
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
