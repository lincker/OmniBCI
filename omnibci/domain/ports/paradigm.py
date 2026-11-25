"""
Paradigm — domain port for stimulus-presentation plugins.

The stimulation subsystem loads the configured Paradigm plugin and drives it
to present stimuli. Each paradigm owns its own per-trial JSON object shape
(SSVEP: cue_target_id; P300: flash sequence; MI: cue class), opaque to the
dispatch/queue layer.
"""
from abc import ABC, abstractmethod
from collections.abc import Mapping

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
    def present_trial(self, parameters: Mapping[str, object]) -> None:
        """Present one trial using the declared, host-validated JSON object."""

    @abstractmethod
    def present_result(self, result: Mapping[str, object]) -> None:
        """Present feedback using the declared, host-validated JSON object."""

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
