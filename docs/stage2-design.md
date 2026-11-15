# Minimal console operation

This document describes the current Run workflow. Acquisition and processing use
a direct data stream; no experiment session, subject or recording transaction is
required to produce a decoded result.

## Configure modules

1. In Acquisition, select a plugin and edit its current parameters. A file source
   needs a path on the backend computer. A device source needs its connection
   parameters and any metadata the source cannot discover.
2. In Signal Processing, select the Decoder and configure its channel mapping,
   epoch detector and ordered filters. Signal channels can be renamed and
   selected; the native event row remains separate.
3. In Stimulation, select the paradigm and optional trigger output. Acquisition's
   incoming events and Stimulation's outgoing trigger are separate roles.
4. In Actuation, select an Effector when physical outputs are required.

Each plugin can save and load its own named Profiles. Saving is optional; each
module's start action applies its current values. Unrelated modules do not need
to be configured before starting one module.

## Run

**Start engine** initializes shared infrastructure without starting any module.
**Stop engine** stops all modules. With the foundation running, each module has
its own controls:

- **Connect** applies current Acquisition values, starts its worker, and connects
  the source. It starts the engine if needed. **Start reading** begins source data
  flow; **Stop reading** retains the connection. **Disconnect** stops Acquisition.
- **Start processing** applies current Processing values and starts its worker,
  starting the engine if needed. It can wait for source data. When reading starts,
  active Processing validates its mapping against observed source metadata before
  receiving chunks. **Stop processing** keeps Acquisition connected and resumes
  its prior reading state. Source-only reading and preview remain available.
- **Launch stimulation** starts its native process. External mode first prepares
  the current configuration, then enables Stimulation after its node connects.
  **Trigger trial** requires active, connected Stimulation and valid trial controls.
  It does not create an EEG event in an existing replay file; file annotations
  provide those events.
- **Start actuation** applies its own current values and starts its Effector.
  External mode first prepares the configuration, then starts Actuation after its
  node connects. Acquisition and Processing are not startup prerequisites.

When Acquisition and Processing are both running, incoming event values reach the
Epoch Detector. Once enough data is available, Filters and Decoder produce a
classification. Run and Results show completed trials immediately and retain the
latest 200 trials for the current engine generation. No session is required.

## Interpret an empty results view

A result is a completed classification, not an incoming trigger notification.
If EEG is visible but no trial result appears, check:

- The acquisition plugin actually decoded a nonzero event from its input stream.
- The configured channel count includes the event row where the source requires it.
- The decoder's selected channel names and signal types match the mapped input.
- Enough samples arrive after the trigger for the epoch and decoder settings.
- The processing worker remains available and no processing error is reported.
- The result connection is healthy; snapshot errors are displayed in Run and Results.

Scan45 currently uses the Profile's sample rate and total channel count as
user-declared metadata. It does not query device metadata. Its parser reads the
low byte of the final data word as the event value and preserves those samples.
The shared acquisition preprocessing step suppresses consecutive equal nonzero
values for Scan45 and Curry8 before queue and preview publication, preserving a
change to another nonzero code. Discrete file events remain unchanged. See
[shared trigger preprocessing](channel-mapping.md#shared-trigger-preprocessing).
For a stream of nine signal rows plus one event row, configure ten total rows,
and map only the nine signal rows to the decoder.

## Minimal result lifetime

Decoded results are a bounded in-memory view for the current engine generation.
They are deduplicated by generation and trial number and can be recovered from
`GET /api/v1/results` after a brief WebSocket disconnect. Restarting the engine or
backend clears this live view. Historical Session queries, continuation, subject
tracking, raw recording management and result export are outside this MVP.

Closing a local desktop backend stops its owned processes after confirmation.
Disconnecting a remote console leaves that backend running. Neither action needs
a session-export decision.

See [frontend-separation.md](frontend-separation.md) for REST/WebSocket contracts
and [validation.md](validation.md) for validation procedures.
