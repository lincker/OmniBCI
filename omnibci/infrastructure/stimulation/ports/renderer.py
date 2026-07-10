"""
Renderer port — abstract interface for 2D stimulus rendering.

Concrete adapters live in infrastructure/stimulation/adapters/:
  - PyOpenGLRenderer   (legacy, current implementation)
  - ModernGLRenderer   (primary target)

Design constraints:
  - Methods are GPU/windowing primitives only; no paradigm-specific knowledge.
  - Color defaults to black (0, 0, 0) where applicable; callers may override
    for non-black backgrounds (e.g. grey VR environments, white test rigs).
  - Coordinates use NDC (Normalized Device Coordinates): x/y in [-1, 1],
    origin at centre, +y up.
"""
from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable

import numpy as np


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

class TextureHandle(NamedTuple):
    """Opaque reference to a GPU-resident texture."""
    id: int


class Rect(NamedTuple):
    """Axis-aligned rectangle in NDC space.

    (x, y) is the bottom-left corner; w and h extend right and up.
    """
    x: float
    y: float
    w: float
    h: float


class Color(NamedTuple):
    """RGBA colour with components in [0.0, 1.0]."""
    r: float
    g: float
    b: float
    a: float = 1.0


# ---------------------------------------------------------------------------
# Renderer protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Renderer(Protocol):
    """2D rendering surface used by stimulus paradigms.

    Lifecycle
    ---------
    initialize → (upload_texture | draw_* | clear | end_frame | poll_events
                  | should_close)* → shutdown

    Thread safety
    -------------
    All methods must be called from the thread that owns the OpenGL/GPU
    context (i.e. the RenderLoop thread).
    """

    # -- Window / context ----------------------------------------------------

    def initialize(self, width: int, height: int, title: str) -> None:
        """Create the window and GPU context.

        Must be called once before any other method.
        """
        ...

    def shutdown(self) -> None:
        """Destroy the window and release all GPU resources."""
        ...

    def poll_events(self) -> None:
        """Process pending OS/window events (resize, close, input)."""
        ...

    def should_close(self) -> bool:
        """Return True when the window has been requested to close."""
        ...

    # -- Texture management --------------------------------------------------

    def upload_texture(self, rgba: np.ndarray) -> TextureHandle:
        """Upload an RGBA image to GPU memory and return a handle.

        Parameters
        ----------
        rgba:
            Numpy array of shape (H, W, 4) with dtype uint8, values 0-255.

        Returns
        -------
        TextureHandle
            Opaque handle valid until release_texture() is called.
        """
        ...

    def release_texture(self, handle: TextureHandle) -> None:
        """Free GPU memory associated with *handle*.

        Calling draw_texture() with a released handle is undefined behaviour.
        """
        ...

    # -- Drawing -------------------------------------------------------------

    def clear(self, color: Color = Color(0.0, 0.0, 0.0)) -> None:
        """Clear the framebuffer to *color*.

        Should be the first draw call of each frame.
        """
        ...

    def draw_texture(self, handle: TextureHandle, rect: Rect) -> None:
        """Blit *handle* into the screen region defined by *rect*."""
        ...

    def draw_rect(self, rect: Rect, color: Color) -> None:
        """Fill *rect* with a solid *color*.

        Primary use: SSVEP flickering targets, P300 highlight overlays.
        """
        ...

    # -- Frame submission ----------------------------------------------------

    def end_frame(self) -> None:
        """Swap front/back buffers and block until the next VSync.

        This is the synchronisation point that gates frame rate to the
        display refresh rate.  Must be called once at the end of each frame.
        """
        ...
