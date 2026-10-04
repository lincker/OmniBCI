/**
 * Results feed: WebSocket increments + REST snapshot on (re)connect.
 *
 * Contract (docs/frontend-separation.md §五.3):
 *   - WS carries increments only; the server buffers nothing for us.
 *   - On every successful (re)connect, pull a snapshot:
 *       GET /sessions/active → GET /sessions/{id}/trials
 *     then keep consuming WS messages.
 *   - Dedupe by (session_id, trial_number): the aggregate's trial_number is
 *     monotonically increasing — a natural idempotency key, so overlap
 *     between snapshot and live increments is harmless.
 *   - A TrialCompleted for an unknown session id triggers a resync (the
 *     active session changed while we were not looking).
 *
 * Reconnect: exponential backoff 1s → ×2 → capped 10s (mirrors the backend's
 * NetworkClient supervisor loop).
 *
 * Liveness: the server sends a Ping every ~15 s. Browsers cannot observe
 * protocol-level pings, so we treat *any* message as proof of life and force
 * a reconnect (→ snapshot resync) when the feed is silent for 3 periods.
 */
import { useEffect, useRef, useState } from "react";
import { api, type SessionInfo, type Trial } from "../api/client";
import { isWsMessage, type StimulationAckMessage } from "../api/ws-types";
import { openResultsSocket, type ResultsSocket } from "../api/results-socket";

export type ConnectionState = "connecting" | "open" | "closed";

export interface ResultsFeed {
  connection: ConnectionState;
  session: SessionInfo | null;
  trials: Trial[];
  acks: StimulationAckMessage[];
}

const MAX_ACKS = 50;
/** Server heartbeat is ~15 s; tolerate 3 missed periods before reconnecting. */
const STALE_AFTER_MS = 45_000;
const STALE_CHECK_MS = 5_000;

export function useResultsFeed(): ResultsFeed {
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [trials, setTrials] = useState<Trial[]>([]);
  const [acks, setAcks] = useState<StimulationAckMessage[]>([]);

  // (session_id:trial_number) keys already applied — the dedupe set.
  const seen = useRef<Set<string>>(new Set());
  const sessionRef = useRef<SessionInfo | null>(null);
  sessionRef.current = session;

  useEffect(() => {
    let alive = true;
    let ws: ResultsSocket | null = null;
    let socketOpen = false;
    let socketGeneration = 0;
    let snapshotGeneration = 0;
    let backoff = 1000;
    let reconnectTimer: ReturnType<typeof setTimeout>;
    let lastMsgAt = Date.now();

    // Staleness watchdog: a half-open socket fires no close event — if the
    // feed (incl. server Pings) goes silent, close it to trigger reconnect.
    const staleTimer = setInterval(() => {
      if (socketOpen && Date.now() - lastMsgAt > STALE_AFTER_MS) {
        ws?.close();
      }
    }, STALE_CHECK_MS);

    const resync = async () => {
      const snapshot = ++snapshotGeneration;
      const active = await api.activeSession().catch(() => null);
      if (!alive || snapshot !== snapshotGeneration) return;
      if (active === null) {
        seen.current = new Set();
        setSession(null);
        setTrials([]);
        return;
      }
      const list = (await api.sessionTrials(active.session_id).catch(() => null)) ?? [];
      if (!alive || snapshot !== snapshotGeneration) return;
      seen.current = new Set(list.map((t) => `${active.session_id}:${t.trial_number}`));
      setSession(active);
      setTrials(list);
    };

    const connect = () => {
      const generation = ++socketGeneration;
      socketOpen = false;
      setConnection("connecting");
      ws = openResultsSocket((event) => {
        if (!alive || generation !== socketGeneration) return;
        if (event.type === "open") {
          socketOpen = true;
          backoff = 1000; // reset on success
          lastMsgAt = Date.now();
          setConnection("open");
          void resync(); // snapshot AFTER the socket is open → no gap window
          return;
        }
        if (event.type === "error") { ws?.close(); return; }
        if (event.type === "closed") {
          socketOpen = false;
          snapshotGeneration += 1;
          clearTimeout(reconnectTimer);
          setConnection("closed");
          reconnectTimer = setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, 10_000);
          return;
        }
        lastMsgAt = Date.now(); // any message (incl. Ping) is proof of life
        let msg: unknown;
        try {
          msg = JSON.parse(event.data ?? "");
        } catch {
          return;
        }
        if (!isWsMessage(msg)) return;

        switch (msg.event) {
          case "TrialCompleted": {
            const sid = msg.session_id;
            if (sid && sessionRef.current?.session_id !== sid) {
              // Active session changed under us → full resync.
              void resync();
              return;
            }
            const key = `${sid ?? "?"}:${msg.trial_number}`;
            if (seen.current.has(key)) return; // snapshot/live overlap
            seen.current.add(key);
            setTrials((prev) => [
              ...prev,
              {
                trial_number: msg.trial_number,
                config_revision: msg.config_revision ?? null,
                target_id: msg.target_id,
                event_id: msg.event_id,
                confidence: msg.confidence,
                forced: msg.forced,
                produced_at: msg.produced_at,
              },
            ]);
            break;
          }
          case "SessionStarted":
            void resync();
            break;
          case "SessionEnded":
            setSession((prev) =>
              prev && prev.session_id === msg.session_id
                ? { ...prev, state: "ended" }
                : prev,
            );
            break;
          case "StimulationAck":
            setAcks((prev) => [msg, ...prev].slice(0, MAX_ACKS));
            break;
          case "Ping":
            break; // liveness only — timestamp already updated above
        }
      });
    };

    connect();
    return () => {
      alive = false;
      clearTimeout(reconnectTimer);
      clearInterval(staleTimer);
      ws?.close();
    };
  }, []);

  return { connection, session, trials, acks };
}
