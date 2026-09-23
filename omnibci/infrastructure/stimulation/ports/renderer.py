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
from omnibci.domain.ports.renderer import Color, Rect, Renderer, TextureHandle

# Keep the historical import path bound to the exact same domain classes.
__all__ = ["Color", "Rect", "Renderer", "TextureHandle"]
