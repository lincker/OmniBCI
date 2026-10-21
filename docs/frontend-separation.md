# Web and backend contracts

The React console in `frontend/` is shared by browsers and the Electron desktop
application. The Python API owns configuration and runtime control. Acquisition
and Signal Processing run in workers; Stimulation renders in a separate native
process. The browser displays status and decoded results, and does not render the
stimulus or process raw EEG.

## Minimal data flow

```text
Acquisition source
  -> signal chunk, including its event row
  -> channel mapping
  -> epoch detector
  -> filters and decoder
  -> TrialCompleted
  -> live result snapshot and WebSocket
  -> Run / Results pages
```

Reading and decoding have no experiment-session lifecycle. The console does not
create a session, select a subject, or wait for a session state before showing
results. It displays completed classifications, not a count of raw trigger pulses.
A trigger must open an epoch with enough usable samples for the selected decoder
before a result appears.

Acquisition, Signal Processing, Stimulation and Actuation have separate plugin
profiles. Saving a profile edits it; Apply changes the selected configuration.
Acquisition Connect operates on the acquisition source. Starting reading prepares
the selected processing configuration against that source, then starts its stream.
See [stage2-design.md](stage2-design.md) for the operating sequence and
[channel-mapping.md](channel-mapping.md) for input mapping.

## REST

The API prefix is `/api/v1`. Successful responses use
`{success: true, message: string, data: T | null}`. Errors use
`{success: false, code: string, message: string, detail?: unknown}`. Clients branch
on the error code and show the reported detail without inventing a runtime state.
`GET /healthz` is the health endpoint and returns its own small JSON response.

The complete contract is generated in [openapi.json](openapi.json). Principal Run
operations are:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/engine/start` | Start the engine and workers |
| POST | `/engine/stop` | Stop the engine |
| GET | `/engine/status` | Read engine and worker availability |
| POST | `/acquisition/connect` | Connect the selected source |
| POST | `/acquisition/disconnect` | Disconnect the source |
| POST | `/acquisition/stream/start` | Prepare processing, then start reading |
| POST | `/acquisition/stream/stop` | Stop reading and drain processing |
| GET | `/acquisition/status` | Read connection and stream state |
| GET | `/acquisition/metadata` | Read the current source descriptor |
| POST | `/acquisition/metadata/refresh` | Refresh descriptor while stopped |
| POST | `/stimulation/start` | Launch the selected native paradigm |
| POST | `/stimulation/stop` | Stop stimulation |
| POST | `/stimulation/trial/start` | Request trials using the selected paradigm controls |
| GET | `/results` | Read current live decoded results |

`GET /results` returns `data: {generation: string | null, trials: TrialResponse[]}`.
Both keys are always present. Each trial contains `generation`, `trial_number`,
`target_id`, `event_id`, `confidence`, `forced`, `produced_at`, and
`config_revision`. Results are held in memory for the current engine generation,
limited to the latest 200 trials. Restarting the engine clears them. This is not a
persistent history or export API.

The previous session and recording-control endpoints have been removed. External
clients must use Acquisition stream controls and `/results`.

## WebSocket results

`/api/v1/ws/results` sends JSON messages with an `event` discriminator:

| Event | Fields |
| --- | --- |
| `TrialCompleted` | `generation`, `trial_number`, classification fields, optional `config_revision` |
| `StimulationAck` | `command`, `target_index`, `status`, `message` |
| `Ping` | No additional fields |

The Python schema is `presentation/schemas/ws.py`; the manually maintained
TypeScript mirror is `frontend/src/api/ws-types.ts`. Heavy epoch arrays are not
included. The shared outbound mapper supplies the same classification fields to
WebSocket and socket consumers.

The console opens its socket before requesting the REST snapshot. It reconciles
snapshots and increments by `(generation, trial_number)`, preserving increments
that arrive while a snapshot is pending. Reconnection requests a fresh snapshot;
old responses from a previous connection are discarded. A message from a different
generation triggers an authoritative snapshot instead of replacing current results
with that message. A five-second snapshot refresh also observes engine restarts
before a new trial arrives. Failed snapshot requests keep the visible results and
show an error.

The server sends a heartbeat approximately every 15 seconds. After 45 seconds
without any message, the client reconnects with exponential backoff capped at
10 seconds. Only the latest 200 results and 50 stimulation acknowledgements are
retained by the console.

## Browser and desktop transport

Browsers use same-origin REST and WebSocket routes. Electron uses the same UI,
with a restricted preload API and a main-process backend proxy. The main process
owns credentials, connection identity, local Python startup and shutdown, and the
local recording-file picker. Switching backends cancels old requests and result
subscriptions. See [desktop.md](desktop.md) for packaging and connection details.

## Contract verification

From the repository root, using the project Python environment:

```sh
python scripts/export_openapi.py
npm run codegen --prefix frontend
npm run codegen:check --prefix frontend
npm run typecheck --prefix frontend
npm test --prefix frontend
python -m pytest tests/presentation/test_results_router.py tests/presentation/test_websocket_results.py tests/presentation/test_openapi_contract.py
```

Generated REST types and OpenAPI must change together. Frontend tests cover stream
button routing, snapshot/live overlap, reconnect backfill, stale responses,
generation changes, and bounded result retention. Hardware acquisition, trigger
transport and native stimulus timing still require device validation.
