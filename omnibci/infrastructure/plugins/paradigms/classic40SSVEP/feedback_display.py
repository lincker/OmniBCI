#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Feedback display widget — renders BCI speller output via the Renderer protocol."""
from __future__ import annotations

import threading

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from omnibci.infrastructure.stimulation.ports.renderer import Renderer, Rect, TextureHandle


def _center_rect(
    cx_px: int, cy_px: int, w_px: int, h_px: int,
    win_w: int = 1920, win_h: int = 1080,
) -> Rect:
    """Pixel center position (y-up from screen center) → NDC Rect."""
    cx = (cx_px / win_w) * 2
    cy = (cy_px / win_h) * 2
    hw = w_px / win_w
    hh = h_px / win_h
    return Rect(x=cx - hw, y=cy - hh, w=hw * 2, h=hh * 2)


class FeedbackDisplay:
    """Mutable text surface rendered via the Renderer protocol.

    Thread-safe: ``add_text`` / ``remove_last`` may be called from any thread;
    texture upload always happens on the GL thread inside ``draw``.
    """

    def __init__(
        self,
        pos_x: int = 0,
        pos_y: int = 495,
        width: int = 1920,
        height: int = 90,
        win_w: int = 1920,
        win_h: int = 1080,
        font_size: int = 40,
        text_x: int = 100,
    ) -> None:
        self._width     = width
        self._height    = height
        self._font_size = font_size
        self._text_x    = text_x
        self._rect      = _center_rect(pos_x, pos_y, width, height, win_w, win_h)

        self._lock    = threading.Lock()
        self._content = ""
        self._dirty   = False
        self._tex: TextureHandle | None = None

        try:
            self._font = ImageFont.truetype('arial.ttf', size=font_size)
        except IOError:
            self._font = ImageFont.load_default(size=font_size)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def setup(self, renderer: Renderer) -> None:
        """Upload initial (blank) texture. Call once from GL thread."""
        self._tex = renderer.upload_texture(self._render(""))

    def teardown(self, renderer: Renderer) -> None:
        """Release GPU texture. Call from GL thread."""
        if self._tex is not None:
            renderer.release_texture(self._tex)
            self._tex = None

    # ── Mutations (any thread) ─────────────────────────────────────────────

    def add_text(self, text: str) -> None:
        with self._lock:
            self._content += text
            self._dirty    = True

    def remove_last(self) -> None:
        with self._lock:
            self._content = self._content[:-1]
            self._dirty   = True

    # ── Rendering (GL thread only) ─────────────────────────────────────────

    def draw(self, renderer: Renderer) -> None:
        """Re-upload texture if content changed, then draw. GL thread only."""
        if self._dirty:
            with self._lock:
                snapshot    = self._content
                self._dirty = False
            if self._tex is not None:
                renderer.release_texture(self._tex)
            self._tex = renderer.upload_texture(self._render(snapshot))
        if self._tex is not None:
            renderer.draw_texture(self._tex, self._rect)

    # ── Private ────────────────────────────────────────────────────────────

    def _render(self, text: str) -> np.ndarray:
        """Render *text* into an RGBA numpy array (flipped for GL)."""
        img  = Image.new('RGBA', (self._width, self._height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)
        text_y = (self._height - self._font_size) / 2
        draw.text((self._text_x, text_y), text, fill='white', font=self._font)
        # Flip vertically: PIL origin = top-left, OpenGL origin = bottom-left
        return np.array(img.transpose(Image.FLIP_TOP_BOTTOM), dtype=np.uint8)
