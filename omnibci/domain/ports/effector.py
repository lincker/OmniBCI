"""
Effector — domain port (plugin contract for the actuation subsystem).

An Effector is the pluggable device driver the actuation subsystem loads to
carry out decoded intent — the output-side analogue of a stimulation Paradigm
(actuation : Effector :: stimulation : Paradigm). The engine emits
ActuationCommand events (neuroengine/actuation_translator); the actuation
subsystem (infrastructure/actuation) — gated by ACTUATION.enabled and
selected via ACTUATION.effector — receives them over the actuation peer
channel and drives the concrete device through this interface.
"""
from abc import ABC, abstractmethod
from typing import Any, Mapping


class Effector(ABC):
    """Pluggable output-device driver (see module docstring)."""

    @abstractmethod
    def setup(self):
        """Acquire and initialise the device before any command is issued."""

    @abstractmethod
    def teardown(self):
        """Release the device and its resources."""

    @abstractmethod
    def play(self, command: Mapping[str, Any]) -> None:
        """Carry out one actuation command.

        *command* is the ActuationCommand payload emitted by the engine\'s
        translator: ``{"target_id": int, "event_id": int|None,
        "confidence": float|None}``. The target → concrete device action
        mapping (which joint, which direction, how far) is device-specific
        and lives in the implementation."""
