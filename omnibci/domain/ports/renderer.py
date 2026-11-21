"""
Renderer port — abstract interface for 2D stimulus rendering.

Concrete adapters live in infrastructure/stimulation/adapters/:
  - PyOpenGLRenderer
  - ModernGLRenderer

Design constraints:
  - Methods are GPU/windowing primitives only; no paradigm-specific knowledge.
  - Color defaults to black (0, 0, 0) where applicable; callers may override
    for non-black backgrounds (e.g. grey VR environments, white test rigs).
  - Coordinates use NDC (Normalized Device Coordinates): the visible viewport
    spans [-1, 1], with origin at centre and +y up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from numbers import Real
from typing import Protocol, runtime_checkable

import numpy as np


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True, eq=False, slots=True)
class TextureHandle:
    """Opaque identity issued by a surface, valid until release or shutdown."""
    _owner: object = field(default=None, repr=False)


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class FrameTiming:
    """Target display refresh, not a callback count or presentation receipt.

    Times are monotonic seconds relative to the rendering-session origin.
    Refresh indices preserve gaps when the render owner misses opportunities.
    """
    refresh_index: int
    target_time: float
    refresh_period: float

    def __post_init__(self) -> None:
        if type(self.refresh_index) is not int:
            raise TypeError("refresh_index must be an integer")
        if self.refresh_index < 0:
            raise ValueError("refresh_index must be nonnegative")
        for name in ("target_time", "refresh_period"):
            value = _finite(getattr(self, name), name)
            if value < 0 or (name == "refresh_period" and value == 0):
                raise ValueError(f"{name} is outside its valid range")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle in NDC space.

    (x, y) is the bottom-left corner; w and h extend right and up.
    """
    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "w", "h"):
            value = _finite(getattr(self, name), name)
            if name in ("w", "h") and value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class Color:
    """RGBA colour with components in [0.0, 1.0]."""
    r: float
    g: float
    b: float
    a: float = 1.0

    def __post_init__(self) -> None:
        for name in ("r", "g", "b", "a"):
            value = _finite(getattr(self, name), name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
            object.__setattr__(self, name, value)


# ---------------------------------------------------------------------------
# Renderer protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class RenderSurface(Protocol):
    """Drawing capability borrowed by a paradigm on the renderer owner thread."""

    # -- Texture management --------------------------------------------------

    def upload_texture(self, image: np.ndarray) -> TextureHandle:
        """Upload a full-frame image to GPU memory and return a handle.

        Parameters
        ----------
        image:
            Numpy array of positive-size (H, W, C) with dtype uint8, values 0-255.
            Supported channel counts are 1 (luminance), 3 (RGB), and 4 (RGBA).

        Returns
        -------
        TextureHandle
            Opaque handle owned by this surface, valid until release or shutdown.
        """
        ...

    def release_texture(self, handle: TextureHandle) -> None:
        """Free GPU memory associated with *handle*.

        Repeated release of an owned handle is harmless. Foreign handles,
        drawing released handles and using a closed surface raise errors.
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


@runtime_checkable
class Renderer(RenderSurface, Protocol):
    """Host-owned window, context and frame submission on one owner thread."""

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

    # -- Frame submission ----------------------------------------------------

    def begin_frame(self) -> FrameTiming | None:
        """Acquire the latest usable display opportunity without blocking.

        None means no new opportunity. Clock failure raises, never invents ticks.
        Poll events before acquiring; end each acquired opportunity exactly once.
        """
        ...

    def end_frame(self, frame: FrameTiming, *, submit: bool) -> bool:
        """Submit ready content only while its opportunity remains current.

        Discard unavailable/expired content. True means the submission call
        completed, not that the image appeared at its target display time.
        """
        ...
