#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Stimulator — wires together trigger, paradigm, renderer, and RenderLoop."""
from __future__ import annotations

import importlib
import os

import omnibci
from omnibci.conf import settings
from omnibci.domain.ports.acquisition import TriggerEmitter
from omnibci.domain.ports.paradigm import Paradigm
from omnibci.infrastructure.stimulation.render_loop import RenderLoop

# ---------------------------------------------------------------------------
# Renderer registry
# Renderer backends are imported lazily so that systems without a GPU (e.g.
# CI runners) can import this module without triggering OpenGL initialisation.
# ---------------------------------------------------------------------------

_RENDERER_REGISTRY: dict[str, str] = {
    "pyopengl": (
        "omnibci.infrastructure.stimulation.adapters"
        ".pyopengl_renderer.PyOpenGLRenderer"
    ),
    "moderngl": (
        "omnibci.infrastructure.stimulation.adapters"
        ".moderngl_renderer.ModernGLRenderer"
    ),
}


def _create_renderer(name: str):
    cls_path = _RENDERER_REGISTRY.get(name)
    if cls_path is None:
        raise ValueError(
            f"Unknown renderer {name!r}. Available: {list(_RENDERER_REGISTRY)}"
        )
    module_path, cls_name = cls_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, cls_name)()


# ---------------------------------------------------------------------------
# Stimulator
# ---------------------------------------------------------------------------

class Stimulator:
    def __init__(self) -> None:
        self.paradigm: Paradigm | None    = None
        self.trigger:  TriggerEmitter | None = None
        self._loop:    RenderLoop | None  = None

    # ── Internal loaders ───────────────────────────────────────────────────

    def _load_trigger(self) -> TriggerEmitter:
        plugin_path = os.path.join(
            next(iter(omnibci.__path__)), 'infrastructure', 'plugins', 'acquisition'
        )
        from omnibci.shared.loader import PluginLoader
        from omnibci.shared.factory import ObjectFactory
        PluginLoader.load_from_dir(
            plugin_path,
            base_package='omnibci.infrastructure.plugins.acquisition',
        )
        return ObjectFactory.create(TriggerEmitter, config_key='ACQUISITION.trigger')

    def _load_paradigm(self, trigger: TriggerEmitter) -> Paradigm:
        plugin_path = os.path.join(
            next(iter(omnibci.__path__)), 'infrastructure', 'plugins', 'paradigms'
        )
        from omnibci.shared.loader import PluginLoader
        from omnibci.shared.factory import ObjectFactory
        PluginLoader.load_from_dir(
            plugin_path,
            base_package='omnibci.infrastructure.plugins.paradigms',
        )
        return ObjectFactory.create(
            Paradigm, config_key='STIMULATION.paradigm', trigger=trigger
        )

    # ── Internal setup ─────────────────────────────────────────────────────

    def _setup(self) -> None:
        """Load plugins, initialise renderer and paradigm, build RenderLoop.

        Called by both ``start()`` and ``start_async()`` before the loop runs.
        """
        self.trigger  = self._load_trigger()
        self.paradigm = self._load_paradigm(self.trigger)

        renderer_name = settings.get("STIMULATION.renderer", "pyopengl")
        renderer = _create_renderer(renderer_name)
        renderer.initialize(width=1920, height=1080, title="OmniBCI Stimulation")

        self.paradigm.setup(renderer=renderer)
        self._loop = RenderLoop(renderer, self.paradigm)

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Setup and start render loop in a background thread (non-macOS)."""
        self._setup()
        self._loop.start()

    async def start_async(self) -> None:
        """Setup and run render loop as an asyncio coroutine.

        Preferred on macOS: the coroutine runs on the asyncio main thread,
        satisfying Cocoa's requirement that GLFW event polling stays on the
        main thread.  Blocks (awaits) until the window is closed or
        ``stop()`` is called from another coroutine.
        """
        self._setup()
        await self._loop.run_async()

    def stop(self) -> None:
        """Stop the RenderLoop (teardown + shutdown run on the GL thread)."""
        if self._loop is not None:
            self._loop.stop()
            self._loop = None

    def present_trial(self, params=None) -> None:
        # *params* is one trial's paradigm-specific parameter (opaque here);
        # the concrete Paradigm interprets it.
        if self.paradigm is not None:
            self.paradigm.trial_stimulus_present(params)

    def present_result(self, result) -> None:
        if self.paradigm is not None:
            self.paradigm.trial_result_present(result=result)
