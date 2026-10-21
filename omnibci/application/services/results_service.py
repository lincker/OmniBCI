"""A bounded view of decoded results for the current engine generation."""
from collections import deque
from threading import RLock

from omnibci.application.events import TrialOutcome


class ResultsService:
    def __init__(self, engine):
        self.engine = engine
        self._generation = None
        self._trials = deque(maxlen=200)
        self._lock = RLock()

    def on_trial_completed(self, event):
        result = event.data
        if not isinstance(result, TrialOutcome):
            return
        with self._lock:
            self._refresh()
            if result.generation != self._generation:
                return
            if any(item.trial_number == result.trial_number for item in self._trials):
                return
            self._trials.append(result)

    def _refresh(self):
        generation = self.engine.generation
        if generation != self._generation:
            self._generation = generation
            self._trials.clear()

    def snapshot(self):
        with self._lock:
            self._refresh()
            return {"generation": self._generation, "trials": [
                {"generation": item.generation, "trial_number": item.trial_number,
                 "config_revision": item.config_revision,
                 "target_id": item.classification.target_id,
                 "event_id": item.classification.event_id,
                 "confidence": item.classification.confidence,
                 "forced": item.classification.forced,
                 "produced_at": item.classification.produced_at}
                for item in self._trials
            ]}
