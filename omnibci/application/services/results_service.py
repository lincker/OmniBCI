"""Decoded results and all processed epoch summaries for one engine generation."""
from collections import deque
from copy import deepcopy
from threading import RLock

from omnibci.application.events import EpochProcessed, TrialOutcome


class ResultsService:
    def __init__(self, engine):
        self.engine = engine
        self._generation = None
        self._trials = deque(maxlen=200)
        self._epochs = {}
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

    def on_epoch_processed(self, event):
        record = event.data
        if not isinstance(record, EpochProcessed):
            return
        with self._lock:
            self._refresh()
            if record.generation != self._generation or record.epoch_number in self._epochs:
                return
            classification = record.classification
            self._epochs[record.epoch_number] = {
                "generation": record.generation,
                "epoch_number": record.epoch_number,
                "event_id": record.event_id,
                "config_revision": record.config_revision,
                "status": record.status,
                "reason": record.reason,
                "sample_rate": record.sample_rate,
                "channel_count": record.channel_count,
                "sample_count": record.sample_count,
                "pre_trigger_samples": record.pre_trigger_samples,
                "started_at": record.started_at,
                "completed_at": record.completed_at,
                "trial_number": record.trial_number,
                "result": None if classification is None else {
                    "target_id": classification.target_id,
                    "confidence": classification.confidence,
                    "forced": classification.forced,
                },
                "error": record.error,
            }

    def _refresh(self):
        generation = self.engine.generation
        if generation != self._generation:
            self._generation = generation
            self._trials.clear()
            self._epochs.clear()

    def epochs(self, *, offset=0, limit=25):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid epoch page")
        with self._lock:
            self._refresh()
            numbers = sorted(self._epochs, reverse=True)[offset:offset + limit]
            return {"generation": self._generation, "total": len(self._epochs),
                    "offset": offset, "limit": limit,
                    "epochs": [deepcopy(self._epochs[number]) for number in numbers]}

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
