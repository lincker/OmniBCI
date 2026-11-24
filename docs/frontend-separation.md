# Web and server contracts

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
profiles and independent current values. Saving preserves a reusable Profile;
loading copies its values into the editor. Connect applies current Acquisition
values. Start processing, Launch stimulation, and Start actuation apply their own
current values independently. Start engine provides shared infrastructure only.
Starting reading starts the Acquisition stream independently of Processing.
See [module operation](module-config.md#independent-modules-on-neuroengine)
for lifecycle and input preparation behavior.

Settings / General separates Engine current values from Server startup values.
Engine review posts explicit `settings` to `/config/apply-plan` with
`module: "engine"`; successful application saves `[engine]` in the user
`settings.toml`. Server Save uses `PATCH /config` with `expected_revision`.
`GET /config` returns saved `value`, startup `active_value`, and `restart_required`
for each Server field; PATCH reports `server_restart_required`. Restarting only
the Engine does not apply pending Server startup values. Sharing a file does not
combine their revision checks or the four module configurations.

Server diagnostics use `/server/info` and `/server/logs`. Desktop connections
require `desktop_api_version: 1`; Engine configuration and status use the
`engine` section and `engine_revision` fields throughout.

## Shared clients and authentication

Web and desktop clients can connect to the same independently running Server.
They share its Engine, module state and live results. Disconnecting a client does
not stop that Server. A desktop-owned local Server is a separate convenience mode:
its owning desktop still confirms and performs shutdown when it exits.

With `OMNIBCI_API_TOKEN` configured, desktop and external-node clients send
`Authorization: Bearer ...`. The Web console uses `GET /server/info` to check its
connection; a 401 opens the token form. `POST /server/session` verifies the Bearer
token and sets an HttpOnly, SameSite=Strict cookie for API and WebSocket requests.
`DELETE /server/session` clears that browser cookie only. Browser credentials are
invalid after a Server restart. Tokens are not passed in URLs or browser storage.

The Web console uses a same-origin `/api` proxy. Vite preserves the browser's Host
header for origin validation. Production proxies must likewise preserve the
public origin and forward WebSocket upgrades. Cookie-authenticated writes and
WebSocket handshakes require that origin; broad cross-origin access is not enabled.
Local loopback Servers may run without authentication, and desktop can attach to
them without a token. Non-loopback desktop connections require authentication.

## REST

The API prefix is `/api/v1`. Successful responses use
`{success: true, message: string, data: T | null}`. Errors use
`{success: false, code: string, message: string, detail?: unknown}`. Clients branch
on the error code and show the reported detail without inventing a runtime state.
`GET /healthz` is the health endpoint and returns its own small JSON response.

Plugin discovery uses `GET /config/plugin-catalog/{kind}` for Acquisition,
Decoder, Epoch Detector, Filter, Paradigm, Trigger and Effector. Setup and Run
consume the same `PluginConfigurationResponse` list, including exact selections,
parameter schemas, platform availability and trial controls. The removed
`/plugins/{category}` and `/stimulation/paradigms` routes return 404 without
redirects or compatibility aliases.

The complete contract is generated in [openapi.json](openapi.json). Principal Run
operations are:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/engine/start` | Start shared infrastructure without starting modules |
| POST | `/engine/stop` | Stop all modules and the engine |
| POST | `/engine/modules/{module}/start` | Submit current Processing or Actuation and start/prepare it |
| POST | `/engine/modules/{module}/stop` | Stop the specified module |
| GET | `/engine/status` | Read foundation readiness, A/P workers, and output availability |
| POST | `/acquisition/connect` | Submit current Acquisition and connect in one owned operation |
| POST | `/acquisition/disconnect` | Disconnect the source and stop Acquisition |
| POST | `/acquisition/stream/start` | Validate source revision, generation and connection ID before reading |
| POST | `/acquisition/stream/stop` | Stop reading and drain Processing if running |
| GET | `/acquisition/status` | Read connection and stream state |
| GET | `/acquisition/metadata` | Read the current source descriptor |
| POST | `/acquisition/metadata/refresh` | Refresh descriptor while stopped |
| POST | `/stimulation/start` | Submit current Stimulation and launch it or prepare its external listener |
| POST | `/stimulation/stop` | Stop stimulation |
| POST | `/stimulation/trial/start` | Request trials using the selected paradigm controls |
| GET | `/results` | Read current live decoded results |
| GET | `/results/epochs` | Page through every processed epoch in the current engine run |

`GET /results` returns `data: {generation: string | null, trials: TrialResponse[]}`.
Both keys are always present. Each trial contains `generation`, `trial_number`,
`target_id`, `event_id`, `confidence`, `forced`, `produced_at`,
`processing_revision`, and `acquisition_revision`. Results are held in memory for the current engine generation,
limited to the latest 200 trials. Restarting the engine clears them. This is not a
persistent history or export API.

The Run page's **Epoch processing** card separately shows every closed epoch,
including decoded, no-result, interrupted and failed processing. Epoch numbers
count all opened epochs; trial numbers still count decoded results only. The
card refreshes once per second and pages through the whole run, rather than
retaining only the latest 200 entries. A new engine generation clears this
history. An epoch appears when it closes; the currently collecting epoch has no
terminal record yet.

`GET /results/epochs?offset=0&limit=25` returns `generation`, `total`, `offset`,
`limit` and `epochs` in descending epoch-number order. The page limit is 100.
Each record includes its status, closing reason, signal counts, processing times,
Acquisition and Processing revisions and optional classification. Records contain no signal
matrices, and there is no epoch-data detail endpoint. The internal
`EpochProcessed` event is separate from `TrialCompleted` and is not forwarded to
stimulation or actuation.

The previous session and recording-control endpoints have been removed. External
clients must use Acquisition stream controls and `/results`.

## WebSocket results

`/api/v1/ws/results` sends JSON messages with an `event` discriminator:

| Event | Fields |
| --- | --- |
| `TrialCompleted` | `generation`, `trial_number`, classification fields, optional `processing_revision` and `acquisition_revision` |
| `StimulationAck` | `command`, `trial_params`, `status`, `message` |
| `Ping` | No additional fields |

The Python schema is `presentation/schemas/ws.py`; the manually maintained
TypeScript mirror is `frontend/src/api/ws-types.ts`. Heavy epoch arrays are not
included. `TrialOutcome.to_dict()` supplies the classification fields, and
`event_to_wire()` in `presentation/websockets/results.py` maps outbound events
against those WebSocket schemas.

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
with a restricted preload API and a main-process server proxy. The main process
owns credentials, connection identity, local Python startup and shutdown, and the
local recording-file picker. Switching servers cancels old requests and result
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
