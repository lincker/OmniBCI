#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Stimulator — wires together trigger, paradigm, renderer, and RenderLoop."""
from __future__ import annotations

import importlib
import threading
from copy import deepcopy

from omnibci.application.configuration.definitions import MODULE_SETTING_DEFAULTS
from omnibci.application.plugin_management.schema import validate_parameters
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

class _NoTrigger(TriggerEmitter):
    """Honor an explicit configuration without trigger output."""

    def setup(self) -> None:
        pass

    def send(self, event_id: int) -> None:
        pass

    def clear(self) -> None:
        pass

    def teardown(self) -> None:
        pass


class Stimulator:
    def __init__(self, *, settings_snapshot: dict) -> None:
        self._snapshot = deepcopy(settings_snapshot)
        self._payload_schemas = {}
        self.paradigm: Paradigm | None    = None
        self.trigger:  TriggerEmitter | None = None
        self._loop:    RenderLoop | None  = None
        self._renderer = None
        self._running = False
        self._stop_requested = False
        self._owner_thread = None
        self._cleanup_error = None
        self._renderer_started = False
        self._trigger_started = False
        self._trigger_ready = False
        self._paradigm_started = False

    # ── Internal loaders ───────────────────────────────────────────────────

    def _load_trigger(self) -> TriggerEmitter:
        from omnibci.infrastructure.plugin_management.preparation import configured_bindings
        from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin
        if not any(binding.slot == "stimulation.trigger" for binding in configured_bindings(self._snapshot)):
            return _NoTrigger()
        return load_configured_plugin(
            self._snapshot, "stimulation.trigger", "trigger", TriggerEmitter,
        )

    def _load_paradigm(self) -> Paradigm:
        from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin_bundle
        loaded = load_configured_plugin_bundle(
            self._snapshot, "stimulation.paradigm", "paradigm", Paradigm,
        )
        self._payload_schemas = dict(loaded.payload_schemas)
        return loaded.instance

    # ── Internal setup ─────────────────────────────────────────────────────

    def _setup(self) -> None:
        self.trigger = self._load_trigger()
        self.paradigm = self._load_paradigm()
        renderer_name = self._snapshot.get(
            "STIMULATION.renderer", MODULE_SETTING_DEFAULTS["stimulation"]["STIMULATION.renderer"],
        )
        self._renderer = _create_renderer(renderer_name)
        self._renderer_started = True
        self._require_none(self._renderer.initialize(width=1920, height=1080, title="OmniBCI Stimulation"),
                           "Renderer.initialize")
        self._trigger_started = True
        self._require_none(self.trigger.setup(), "TriggerEmitter.setup")
        self._trigger_ready = True
        self._require_none(self.trigger.clear(), "TriggerEmitter.clear")
        self._paradigm_started = True
        self._require_none(self.paradigm.setup(renderer=self._renderer, trigger=self.trigger), "Paradigm.setup")
        self._loop = RenderLoop(self._renderer, self.paradigm)
        if self._stop_requested:
            self._loop.request_stop()

    @staticmethod
    def _require_none(result, operation) -> None:
        if result is not None:
            raise TypeError(f"{operation} must return None")

    def _cleanup(self) -> None:
        errors = []
        actions = []
        if self._paradigm_started:
            actions.append((self.paradigm, "teardown", "Paradigm.teardown"))
        if self._trigger_ready:
            actions.append((self.trigger, "clear", "TriggerEmitter.clear"))
        if self._trigger_started:
            actions.append((self.trigger, "teardown", "TriggerEmitter.teardown"))
        if self._renderer_started:
            actions.append((self._renderer, "shutdown", "Renderer.shutdown"))
        for component, method, name in actions:
            try:
                self._require_none(getattr(component, method)(), name)
            except BaseException as exc:
                exc.add_note(name)
                errors.append(exc)
        if errors:
            self._cleanup_error = errors[0] if len(errors) == 1 else BaseExceptionGroup(
                "Stimulation resource cleanup failed", errors)
            raise self._cleanup_error
        self.paradigm = self.trigger = self._renderer = self._loop = None
        self._renderer_started = self._trigger_started = self._trigger_ready = self._paradigm_started = False

    async def start_async(self) -> None:
        """Prepare, render and release on this thread; the application owns this task."""
        if self._running:
            raise RuntimeError("Stimulation is already running")
        if self._cleanup_error is not None:
            raise RuntimeError("Stimulation cleanup is unsettled; replacement is blocked") from self._cleanup_error
        self._running = True
        self._stop_requested = False
        self._owner_thread = threading.get_ident()
        failure = None
        try:
            self._setup()
            await self._loop.run_async()
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                self._cleanup()
            except BaseException as cleanup_error:
                if failure is not None:
                    raise BaseExceptionGroup("Stimulation failed and cleanup also failed", [failure, cleanup_error])
                raise
            finally:
                self._running = False

    def request_stop(self) -> None:
        """Signal the loop without releasing resources or claiming settlement."""
        self._stop_requested = True
        if self._loop is not None:
            self._loop.request_stop()

    def _check_owner(self) -> None:
        if self._owner_thread is not None and self._owner_thread != threading.get_ident():
            raise RuntimeError("Stimulation callbacks require the renderer owner thread")

    def present_trial(self, parameters: dict[str, object]) -> None:
        self._check_owner()
        if self.paradigm is None:
            raise RuntimeError("Stimulation paradigm has not been initialized")
        payload = validate_parameters(self._payload_schemas["trial"], parameters, apply_defaults=False)
        self._require_none(self.paradigm.present_trial(payload), "Paradigm.present_trial")

    @property
    def is_presenting(self) -> bool:
        """True while a trial or feedback is active; query failures propagate."""
        self._check_owner()
        if self.paradigm is None:
            raise RuntimeError("Stimulation paradigm has not been initialized")
        presenting = self.paradigm.is_presenting()
        if not isinstance(presenting, bool):
            raise TypeError("Paradigm.is_presenting() must return a bool")
        return presenting

    def present_result(self, result) -> None:
        self._check_owner()
        if self.paradigm is None:
            raise RuntimeError("Stimulation paradigm has not been initialized")
        payload = validate_parameters(self._payload_schemas["result"], result, apply_defaults=False)
        self._require_none(self.paradigm.present_result(payload), "Paradigm.present_result")
