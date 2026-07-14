"""
Paradigm — domain port for stimulus-presentation plugins.

The stimulation subsystem loads a Paradigm plugin (selected by
STIMULATION.paradigm) and drives it to present stimuli. Each paradigm owns its
own per-trial parameter shape (SSVEP: target/event id; P300: flash sequence;
MI: cue class), opaque to the dispatch/queue layer.
"""
from abc import ABC, abstractmethod


class Paradigm(ABC):

    @abstractmethod
    def setup(self):
        pass
    @abstractmethod
    def teardown(self):
        pass

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
