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

import os
import string
import logging
import threading
from enum import Enum, auto

import numpy as np
from PIL import Image

from omnibci.domain.ports.paradigm import Paradigm
from omnibci.infrastructure.stimulation.ports.renderer import (
    Renderer, Rect, TextureHandle,
)
from .feedback_display import FeedbackDisplay


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WIN_W, _WIN_H = 1920, 1080

_CUE_FRAMES    = 60   # ~1 s at 60 Hz
_RESULT_FRAMES = 90   # ~1.5 s feedback display


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_rgba(path: str) -> np.ndarray:
    """PIL image → H×W×4 uint8, flipped for OpenGL bottom-up convention."""
    img = Image.open(path).convert('RGBA').transpose(Image.FLIP_TOP_BOTTOM)
    return np.array(img, dtype=np.uint8)


def _center_rect(cx_px: int, cy_px: int, w_px: int, h_px: int) -> Rect:
    """Pixel center (y-up from screen center) → NDC Rect (bottom-left origin)."""
    cx = (cx_px / _WIN_W) * 2
    cy = (cy_px / _WIN_H) * 2
    hw = w_px / _WIN_W
    hh = h_px / _WIN_H
    return Rect(x=cx - hw, y=cy - hh, w=hw * 2, h=hh * 2)


# Matches original StimImage(position=(0, -45)) for a 1920×1080 image
_STIM_RECT: Rect = _center_rect(0, -45, _WIN_W, _WIN_H)


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

    def __init__(self, trigger):
        self.trigger = trigger
        self._renderer: Renderer | None = None

        # GPU texture handles (populated in setup)
        self._init_tex: TextureHandle | None     = None
        self._stim_textures: list[TextureHandle] = []
        self._cue_textures:  list[TextureHandle] = []

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
        self._result_countdown = 0

        _here = os.path.dirname(os.path.abspath(__file__))
        self._ssvep_dir = os.path.join(_here, 'resource', 'ssvep')
        self._cue_dir   = os.path.join(_here, 'resource', 'ssvep_cue')

    # ── Paradigm port ──────────────────────────────────────────────────────

    def setup(self, renderer: Renderer = None) -> None:  # type: ignore[override]
        """Upload all textures to GPU.

        *renderer* is injected at the infrastructure layer.  The domain port
        declares ``setup(self)`` with no parameters; this override adds an
        optional *renderer* keyword so the existing ``Stimulator.start()``
        call (no arguments) still works as a no-op until Step 7 updates it.
        """
        if renderer is None:
            return
        self._renderer = renderer

        # Initial / rest frame
        self._init_tex = renderer.upload_texture(
            _load_rgba(os.path.join(self._ssvep_dir, 'initial_frame.png'))
        )

        # Stimulation frames: 0.png, 1.png, ... (sorted numerically)
        stim_files = sorted(
            (f for f in os.listdir(self._ssvep_dir)
             if f.endswith('.png') and f != 'initial_frame.png'),
            key=lambda f: int(os.path.splitext(f)[0]),
        )
        for fname in stim_files:
            self._stim_textures.append(
                renderer.upload_texture(
                    _load_rgba(os.path.join(self._ssvep_dir, fname))
                )
            )

        # Cue frames: 0.png, 1.png, ...
        cue_files = sorted(
            (f for f in os.listdir(self._cue_dir) if f.endswith('.png')),
            key=lambda f: int(os.path.splitext(f)[0]),
        )
        for fname in cue_files:
            self._cue_textures.append(
                renderer.upload_texture(
                    _load_rgba(os.path.join(self._cue_dir, fname))
                )
            )

        # Text widget — uploads blank texture
        self._text_view.setup(renderer)

        with self._lock:
            self._state = _State.IDLE

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
        if self._init_tex is not None:
            r.release_texture(self._init_tex)
        self._stim_textures.clear()
        self._cue_textures.clear()
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
                r.draw_texture(self._cue_textures[self._current_event], _STIM_RECT)
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
                self._draw_idle(r)
                self._result_countdown -= 1
                if self._result_countdown <= 0:
                    self._state = _State.IDLE

    # ── Trial control (application thread) ────────────────────────────────

    def trial_stimulus_present(self, event_id: int) -> None:
        """Begin cue->stim sequence for *event_id*.  Thread-safe."""
        with self._lock:
            self._current_event = event_id
            if 0 <= event_id < 40:
                self._cue_countdown = _CUE_FRAMES
                self._state         = _State.CUE
            else:
                # No cue — go straight to STIM
                self.trigger.clear()
                self.trigger.send(event_id)
                self._stim_frame_idx = 0
                self._state          = _State.STIM

    def trial_result_present(self, result=None) -> None:
        """Update feedback text and display for ~1.5 s.  Thread-safe."""
        text = self._parse_feedback(result)
        logging.info(f"feedback result={result!r}, char={text!r}")
        if text == 'Del':
            self._text_view.remove_last()
        else:
            self._text_view.add_text(text)
        with self._lock:
            self._result_countdown = _RESULT_FRAMES
            self._state            = _State.RESULT

    # ── Private ────────────────────────────────────────────────────────────

    def _draw_idle(self, r: Renderer) -> None:
        """Draw initial frame + text. Caller must hold _lock."""
        r.draw_texture(self._init_tex, _STIM_RECT)
        self._text_view.draw(r)

    def _parse_feedback(self, index: int) -> str:
        if not (0 <= index < 40):
            logging.error(f'Invalid feedback index: {index}')
            raise IndexError(index)
        return self._stim_target_text[index]
