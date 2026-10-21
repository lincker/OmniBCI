# Minimal console operation

This document describes the current Run workflow. Acquisition and processing use
a direct data stream; no experiment session, subject or recording transaction is
required to produce a decoded result.

## Configure modules

1. In Acquisition, select a plugin, then select or create its Profile. Save and
   Apply it. A file source needs a path on the backend computer. A device source
   needs its connection parameters and any metadata the source cannot discover.
2. In Signal Processing, select the Decoder and its Profile. Configure the input
   channel mapping, epoch detector and filters required by the decoder. Save and
   Apply. Signal channels can be renamed and selected; the native event row is
   preserved separately and does not need a signal mapping entry.
3. If stimulation is required, select its paradigm and optional trigger output
   independently in Stimulation. Apply the selected profiles. Acquisition's
   incoming events and stimulation's outgoing trigger plugin are separate roles.
4. Configure Actuation only when physical outputs are required.

Profiles belong to their modules. Connect does not require unrelated processing
settings. Starting reading validates the processing configuration needed by the
selected decoder against the connected source before starting the stream.

## Run

1. **Start engine** initializes the engine and workers.
2. **Connect** opens the selected acquisition source.
3. **Start reading** prepares signal processing, then reads source chunks.
4. Incoming event values reach the epoch detector. Once enough data is available,
   the selected filters and decoder produce a classification.
5. **Recent results** and **Results** display completed trials immediately through
   the live result feed. The Results page retains the latest 200 trials from the
   current engine. There is no session to create or end.
6. **Stop reading** stops the producer and drains processing. **Disconnect** closes
   the source. **Stop engine** disposes the running engine resources.

**Launch stimulation** starts the native stimulation process. **Trigger trial**
requires that process to report the selected paradigm and an engine connection,
with its controls loaded from the applied profile. This control does not create
an EEG event in an existing replay file; file annotations provide those events.

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
low byte of the final data word as the event value and suppresses repeated held
values. For a stream of nine signal rows plus one event row, configure ten total
rows, and map only the nine signal rows to the decoder.

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
