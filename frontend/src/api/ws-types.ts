/**
 * WebSocket message types for /api/v1/ws/results.
 *
 * Hand-written mirror of omnibci/presentation/schemas/ws.py (WsMessage union).
 * Kept manually because WebSocket payloads are not part of the OpenAPI spec;
 * the shape is protected on the backend by the contract-freeze tests.
 * If schemas/ws.py changes, update this file in the same commit.
 *
 * Server → client only. The top-level `event` field is the discriminator.
 */

/** Engine decoded one trial. `session_id` is REST-queryable (unified id). */
export interface TrialCompletedMessage {
  event: "TrialCompleted";
  session_id: string | null;
  trial_number: number;
  target_id: number;
  event_id: number;
  confidence: number | null;
  forced: boolean;
  /** ISO 8601 UTC */
  produced_at: string;
}

export interface SessionStartedMessage {
  event: "SessionStarted";
  session_id: string;
}

export interface SessionEndedMessage {
  event: "SessionEnded";
  session_id: string | null;
  total_trials: number;
}

/** Stimulation process acknowledged a command (relayed engine → WS). */
export interface StimulationAckMessage {
  event: "StimulationAck";
  command: string;
  target_index: number | null;
  status: string;
  message: string | null;
}

/** Server heartbeat (~15 s). No payload — resets the client staleness timer. */
export interface PingMessage {
  event: "Ping";
}

export type WsMessage =
  | TrialCompletedMessage
  | SessionStartedMessage
  | SessionEndedMessage
  | StimulationAckMessage
  | PingMessage;

/** Type guard for incoming raw JSON. */
export function isWsMessage(value: unknown): value is WsMessage {
  return (
    typeof value === "object" &&
    value !== null &&
    "event" in value &&
    ["TrialCompleted", "SessionStarted", "SessionEnded", "StimulationAck", "Ping"].includes(
      (value as { event: string }).event,
    )
  );
}
