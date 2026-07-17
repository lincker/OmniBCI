"""
Effector — domain port (plugin contract for the actuation subsystem).

An Effector is the pluggable device driver the actuation subsystem loads to
carry out decoded intent — the output-side analogue of a stimulation Paradigm
(actuation : Effector :: stimulation : Paradigm). The engine emits
ActuationCommand events (neuroengine/actuation_translator); the actuation
subsystem — gated by ACTUATION.enabled and selected via ACTUATION.effector —
receives them over the actuation peer channel and drives the concrete device
through this interface.

Status (WIP): the engine-side wiring exists, but the actuation subsystem that
loads and drives Effector plugins is not built yet, so this port currently has
no concrete implementation. play()'s signature is intentionally left open until
there is a real caller to define it against.
"""
from abc import ABC, abstractmethod


class Effector(ABC):
    """Pluggable output-device driver (see module docstring)."""

    @abstractmethod
    def setup(self):
        """Acquire and initialise the device before any command is issued."""

    @abstractmethod
    def teardown(self):
        """Release the device and its resources."""

    @abstractmethod
    def play(self, *args, **kwargs) -> None:
        """Carry out one actuation command. Signature is provisional — it will
        be tightened (likely to ``play(command)``) when the actuation subsystem
        that calls it is built."""
