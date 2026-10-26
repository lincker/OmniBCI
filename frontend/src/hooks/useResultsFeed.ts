/** Live results: REST snapshots reconcile WebSocket increments by engine generation. */
import { useEffect, useState } from "react";
import { api, type Trial } from "../api/client";
import { isWsMessage, type StimulationAckMessage } from "../api/ws-types";
import { openResultsSocket, type ResultsSocket } from "../api/results-socket";

export type ConnectionState = "connecting" | "open" | "closed";

export interface ResultsFeed {
  connection: ConnectionState;
  generation: string | null;
  trials: Trial[];
  acks: StimulationAckMessage[];
  error: string | null;
}

export const MAX_ACKS = 50;
export const MAX_TRIALS = 200;
const STALE_AFTER_MS = 45_000;
const SNAPSHOT_INTERVAL_MS = 5_000;

export function useResultsFeed(): ResultsFeed {
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [generation, setGeneration] = useState<string | null>(null);
  const [trials, setTrials] = useState<Trial[]>([]);
  const [acks, setAcks] = useState<StimulationAckMessage[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let ws: ResultsSocket | null = null;
    let socketOpen = false;
    let socketGeneration = 0;
    let snapshotGeneration = 0;
    let currentGeneration: string | null = null;
    let currentTrials = new Map<number, Trial>();
    let backoff = 1000;
    let reconnectTimer: ReturnType<typeof setTimeout>;
    let lastMsgAt = Date.now();

    const publish = () => {
      setGeneration(currentGeneration);
      const retained = [...currentTrials.values()].sort((a, b) => a.trial_number - b.trial_number).slice(-MAX_TRIALS);
      currentTrials = new Map(retained.map((trial) => [trial.trial_number, trial]));
      setTrials(retained);
    };
    const resync = async () => {
      const request = ++snapshotGeneration;
      try {
        const snapshot = await api.results();
        if (!alive || request !== snapshotGeneration || !snapshot) return;
        if (snapshot.generation !== currentGeneration) {
          currentGeneration = snapshot.generation;
          currentTrials = new Map();
        }
        // Retain increments received while this snapshot was in flight.
        for (const trial of snapshot.trials) {
          if (trial.generation === currentGeneration) currentTrials.set(trial.trial_number, trial);
        }
        publish();
        setError(null);
      } catch (cause) {
        if (alive && request === snapshotGeneration) setError(cause instanceof Error ? cause.message : String(cause));
      }
    };

    // Also observe engine restarts when no decoded trial has arrived yet.
    const snapshotTimer = setInterval(() => {
      if (socketOpen && Date.now() - lastMsgAt > STALE_AFTER_MS) ws?.close();
      else void resync();
    }, SNAPSHOT_INTERVAL_MS);

    const connect = () => {
      const socket = ++socketGeneration;
      socketOpen = false;
      setConnection("connecting");
      ws = openResultsSocket((event) => {
        if (!alive || socket !== socketGeneration) return;
        if (event.type === "open") {
          socketOpen = true;
          backoff = 1000;
          lastMsgAt = Date.now();
          setConnection("open");
          void resync();
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
        lastMsgAt = Date.now();
        let msg: unknown;
        try { msg = JSON.parse(event.data ?? ""); } catch { return; }
        if (!isWsMessage(msg)) return;
        switch (msg.event) {
          case "TrialCompleted": {
            if (msg.generation !== currentGeneration) {
              void resync();
              break;
            }
            if (currentTrials.has(msg.trial_number)) break;
            currentTrials.set(msg.trial_number, {
              generation: msg.generation,
              trial_number: msg.trial_number,
              config_revision: msg.config_revision ?? null,
              target_id: msg.target_id,
              event_id: msg.event_id,
              confidence: msg.confidence,
              forced: msg.forced,
              produced_at: msg.produced_at,
            });
            publish();
            break;
          }
          case "StimulationAck":
            setAcks((previous) => [msg, ...previous].slice(0, MAX_ACKS));
            break;
          case "Ping":
            break;
        }
      });
    };

    connect();
    return () => {
      alive = false;
      clearTimeout(reconnectTimer);
      clearInterval(snapshotTimer);
      ws?.close();
    };
  }, []);

  return { connection, generation, trials, acks, error };
}
