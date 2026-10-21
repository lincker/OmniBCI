"""
Tests for /api/v1/ws/results WebSocket endpoint and ResultsBroadcaster.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.presentation.websockets.results import ResultsBroadcaster
from omnibci.presentation.schemas.ws import (
    PingMessage,
    TrialCompletedMessage,
    WsMessage,
)
from omnibci.shared.events import EventType, Event
from omnibci.domain.classification import Classification


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clf(**kwargs) -> Classification:
    defaults = dict(
        target_id=3, event_id=101, confidence=0.85, forced=False,
        produced_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return Classification(**defaults)

def _event(event_type, data) -> Event:
    return Event(event_type=event_type, data=data)


# ---------------------------------------------------------------------------
# ResultsBroadcaster unit tests
# ---------------------------------------------------------------------------

class TestResultsBroadcasterInit:
    def test_no_clients_on_init(self):
        assert len(ResultsBroadcaster()._clients) == 0

    def test_disconnect_unknown_client_is_safe(self):
        ResultsBroadcaster().disconnect(MagicMock())


class TestOnTrialCompleted:
    def test_does_nothing_without_loop(self):
        b = ResultsBroadcaster()
        b._clients.add(MagicMock())
        b.on_trial_completed(_event(EventType.TrialCompleted, _clf()))

    def test_does_nothing_without_clients(self):
        b = ResultsBroadcaster()
        b.set_loop(asyncio.new_event_loop())
        b.on_trial_completed(_event(EventType.TrialCompleted, _clf()))

    def test_bad_payload_type_is_dropped_not_broadcast(self, caplog):
        # A non-TrialOutcome/Classification payload yields no wire message and
        # must not raise; the unified mapping (event_to_wire) logs + returns None.
        import logging
        sent = []
        b = ResultsBroadcaster()
        b.set_loop(asyncio.new_event_loop())
        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=sent.append)
        b._clients.add(ws)
        with caplog.at_level(logging.ERROR):
            b.on_trial_completed(_event(EventType.TrialCompleted, data=42))
        assert sent == []
        assert "TrialOutcome" in caplog.text or "Classification" in caplog.text

    def test_trial_outcome_broadcast_carries_engine_generation(self):
        from omnibci.infrastructure.outbound import TrialOutcome
        b = ResultsBroadcaster()
        b.set_loop(asyncio.new_event_loop())
        b._clients.add(MagicMock())
        captured = []
        b._schedule = captured.append  # capture the JSON instead of scheduling
        outcome = TrialOutcome(generation="engine-7", trial_number=4, classification=_clf(target_id=2))
        b.on_trial_completed(_event(EventType.TrialCompleted, outcome))
        assert len(captured) == 1
        payload = json.loads(captured[0])
        assert payload["generation"] == "engine-7"
        assert payload["trial_number"] == 4
        assert payload["target_id"] == 2

    @pytest.mark.asyncio
    async def test_serialises_all_classification_fields(self):
        b = ResultsBroadcaster()
        sent = []
        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=sent.append)
        b._clients.add(ws)

        clf = _clf(target_id=7, event_id=202, confidence=0.91, forced=True)
        await b._broadcast(TrialCompletedMessage(
            generation="s1", trial_number=3,
            target_id=clf.target_id, event_id=clf.event_id,
            confidence=clf.confidence, forced=clf.forced,
            produced_at=clf.produced_at,
        ).model_dump_json())

        payload = json.loads(sent[0])
        assert payload["event"] == "TrialCompleted"
        assert payload["target_id"] == 7
        assert payload["event_id"] == 202
        assert abs(payload["confidence"] - 0.91) < 1e-6
        assert payload["forced"] is True

    @pytest.mark.asyncio
    async def test_removes_dead_client(self):
        b = ResultsBroadcaster()
        dead = MagicMock()
        dead.send_text = AsyncMock(side_effect=Exception("closed"))
        b._clients.add(dead)
        await b._broadcast('{"event":"TrialCompleted"}')
        assert dead not in b._clients


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestTrialCompletedMessage:
    def test_defaults(self):
        msg = TrialCompletedMessage(
            generation="s", trial_number=1, target_id=0,
            event_id=100, produced_at=datetime.now(timezone.utc),
        )
        assert msg.event == "TrialCompleted"
        assert msg.confidence is None
        assert msg.forced is False

    def test_confidence_out_of_range_raises(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            TrialCompletedMessage(
                generation="s", trial_number=1, target_id=0,
                event_id=100, confidence=1.5,
                produced_at=datetime.now(timezone.utc),
            )


class TestPingMessage:
    def test_wire_shape(self):
        assert json.loads(PingMessage().model_dump_json()) == {"event": "Ping"}

    def test_ping_is_in_the_frozen_union(self):
        # Contract freeze: 3 ws message kinds. Changing this set must be a
        # deliberate, same-commit change with frontend/src/api/ws-types.ts.
        import typing
        names = {m.__name__ for m in typing.get_args(WsMessage)}
        assert names == {
            "TrialCompletedMessage", "StimulationAckMessage", "PingMessage",
        }


class TestHeartbeat:
    def test_start_before_set_loop_raises(self):
        with pytest.raises(RuntimeError):
            ResultsBroadcaster().start_heartbeat()

    @pytest.mark.asyncio
    async def test_heartbeat_sends_ping_periodically(self):
        b = ResultsBroadcaster()
        b.set_loop(asyncio.get_running_loop())
        sent = []
        ws = MagicMock()
        ws.send_text = AsyncMock(side_effect=sent.append)
        b._clients.add(ws)
        b.start_heartbeat(interval_s=0.01)
        await asyncio.sleep(0.05)
        b.stop_heartbeat()
        assert len(sent) >= 2
        assert all(json.loads(m) == {"event": "Ping"} for m in sent)

    @pytest.mark.asyncio
    async def test_heartbeat_reaps_dead_client_during_idle(self):
        b = ResultsBroadcaster()
        b.set_loop(asyncio.get_running_loop())
        dead = MagicMock()
        dead.send_text = AsyncMock(side_effect=Exception("half-open"))
        b._clients.add(dead)
        b.start_heartbeat(interval_s=0.01)
        await asyncio.sleep(0.05)
        b.stop_heartbeat()
        assert dead not in b._clients

    @pytest.mark.asyncio
    async def test_start_heartbeat_is_idempotent(self):
        b = ResultsBroadcaster()
        b.set_loop(asyncio.get_running_loop())
        b.start_heartbeat(interval_s=10)
        task = b._hb_task
        b.start_heartbeat(interval_s=10)
        assert b._hb_task is task  # no second task spawned
        b.stop_heartbeat()


# ---------------------------------------------------------------------------
# WebSocket integration
# ---------------------------------------------------------------------------

class TestWebSocketEndpoint:
    def test_receives_trial_completed(self, api_client):
        from omnibci.presentation.websockets.results import results_broadcaster

        clf = _clf(target_id=9, event_id=999)
        msg = TrialCompletedMessage(
            generation="test", trial_number=1,
            target_id=clf.target_id, event_id=clf.event_id,
            confidence=clf.confidence, forced=clf.forced,
            produced_at=clf.produced_at,
        ).model_dump_json()

        with api_client.websocket_connect("/api/v1/ws/results") as ws:
            asyncio.run(results_broadcaster._broadcast(msg))  # get_event_loop() is deprecated & flaky on Py3.10+
            payload = json.loads(ws.receive_text())

        assert payload["event"] == "TrialCompleted"
        assert payload["target_id"] == 9
        assert payload["event_id"] == 999
