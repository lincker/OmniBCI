"""
Paradigm — domain port for stimulus-presentation plugins.

The stimulation subsystem loads a Paradigm plugin (selected by
STIMULATION.paradigm) and drives it to present stimuli. Each paradigm owns its
own per-trial parameter shape (SSVEP: target/event id; P300: flash sequence;
MI: cue class), opaque to the dispatch/queue layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from omnibci.domain.ports.renderer import RenderSurface
from omnibci.domain.ports.acquisition import TriggerOutput


class Paradigm(ABC):

    @abstractmethod
    def setup(self, *, renderer: RenderSurface, trigger: TriggerOutput) -> None:
        """Borrow drawing/output capabilities and prepare stimulus resources."""

    @abstractmethod
    def teardown(self) -> None:
        """Release partial or complete stimulus resources on the owner thread."""

    @abstractmethod
    def trial_stimulus_present(self, params) -> None:
        """Present one trial.

        *params* is this paradigm's per-trial parameter, opaque to the
        dispatch/queue layer.  Each paradigm defines its own shape (e.g. SSVEP:
        an int target/event id; P300: a flash-sequence spec; MI: a cue class).
        """
        pass

    @abstractmethod
    def trial_result_present(self, result) -> None: pass

    @abstractmethod
    def render_frame(self, elapsed_seconds: float) -> None:
        """Draw on the renderer owner thread; the host submits the frame.

        Elapsed seconds use a monotonic clock since this rendering session began.
        """

    @abstractmethod
    def is_presenting(self) -> bool:
        """Report trial or feedback activity on the renderer owner thread.

        Return a bool: False admits another trial. Query failures propagate;
        neither a missing method nor an invalid result means the paradigm is idle.
        """
