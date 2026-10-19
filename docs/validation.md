# Configuration and plugin validation

## Automated checks

The `tests` workflow configures Python 3.14 jobs on GitHub's Linux, macOS, and
Windows runners. Each OS runs the headless unit suite and a separate integration
suite using real subprocesses, local IPC, and checked-in signal fixtures. The
headless suite includes runtime failure injection for drain, projection,
checkpoint, startup, persistence, output settlement, and unknown resume outcomes.

Managed stimulation and actuation children explicitly use the `spawn` context so
each launch inherits the caller's current deployment environment. A regression
starts a forkserver before changing the plugin directory, then checks that both
managed children receive the updated directory without changing pytest's global
process start method.

An independent Linux job generates the frontend REST types, checks that they
match the committed API contract, runs the frontend tests, and builds the console.
It runs even when backend checks fail. On each OS, a fresh wheel replaces the
editable installation before isolated Python verifies bundled plugin manifests,
schemas/defaults, host settings, and all 281 stimulus frames. This catches assets
that work in a checkout but are missing from the distributed package. The wheel
checks still run after a headless test failure if dependency installation
succeeded; cancellation stops them, and test failures still fail the job.

Build release wheels from a clean checkout. Setuptools can retain deleted files
in a previous `build/lib` directory; a stale parent plugin manifest can hide newer
contributions. The isolated wheel check rejects an import from the source tree
and detects missing contributions caused by such artifacts.

The separate `contract` workflow compares a newly exported OpenAPI document with
the committed document on pull requests and pushes to `dev` or `main`.
Import-linter checks the architecture dependency rules, including the prohibition
on domain imports of infrastructure. A push to either branch or a pull request
runs nine checks: three headless, three integration, frontend, architecture, and
OpenAPI. Configure these check names as required in branch protection.

## What these checks establish

The dated results below describe the corresponding implementation checkpoints.
They do not certify every subsequent change or hardware/storage deployment.

### Curry 8 signal path and runtime checks

The Curry 8 path is TCP NetStreaming → source adapter → `SignalChunk` → raw
recording and IPC queue → processing channel mapping → epoch collection →
ordered filters → decoder → session Trial results. Raw recording happens before
mapping and filtering, so it retains auxiliary channels and the native event row.

| Boundary | Processing and checks | Runtime implication |
| --- | --- | --- |
| Connection | Negotiate protocol 802 or 803; read a 20-byte header and bounded body. | Unsupported versions, unknown packet identifiers and oversized packets fail with a protocol error. |
| Device metadata | Read sample rate, row count, sample size and 144-byte channel records; exclude the final event row from signal-channel descriptors. | Actual device dimensions feed processing. Labels are retained; missing labels receive generated names. Curry channel types remain `unknown`. |
| Experiment expectations | Compare only explicitly configured sample rate and output-row count with observed metadata. | Missing expectations use the device values. A provided mismatch still blocks preparation, even when the other field is absent. Profiles are not rewritten. |
| Processing preparation | Resolve channel IDs, ordering, aliases and declared types; check the decoder's channel requirements and acknowledge the plan for the current connection/configuration. | CCA/FBCCA require EEG inputs. Select actual EEG channels and declare `eeg` in processing input when Curry reports no types. Coordinates and a montage are not required. Stale plans and unavailable channels remain errors. |
| EEG packets | Decode supported int16/float32 payloads into channels × samples; preserve the native final trigger row. Ignore separate event-list and impedance packets. | Truncated/misaligned EEG bodies fail. Compressed streaming is currently unsupported and must be disabled on the server. No amplitude threshold, resampling or unit conversion is applied here. |
| Raw storage | Add host acquisition time and increasing chunk sequence; append the full original matrix before sending it downstream. | Numeric dtype, row count, finite timestamp, sequence order and active writer/session ownership are checked. These protect recording integrity; absent experiment expectations do not prevent recording. |
| Mapping and epochs | Select/reorder signal rows while preserving trigger values; detect boundaries and collect post-trigger samples plus separate pre-trigger history. | The default detector treats each nonzero trigger sample as a new start. No trigger means no decoded Trial, while raw acquisition can continue. |
| Filters and decoding | Apply configured filters in order to the accumulated window, then call the decoder. Baseline data is supplied separately to filters. | Baseline collection alone does not apply baseline correction. Closing boundaries can force a decode before the normal duration gate. A decoder may decline an unusable window with `None`. |
| Results and stopping | Validate decoder output, record a Trial in the running session, publish result events; stop joins acquisition, flushes storage and drains processing before closing the session. | No classification is invented for a declined epoch. A failed write or incomplete drain retains failure/recovery evidence. |

For a minimal Curry run, select **Neuroscan CURRY 8**, set its host and TCP port
(default `4455`), start the engine and connect, then read metadata. Keep the
connection-only binding to use observed dimensions, or save and apply the
observed values to establish an experiment baseline. In the decoder
Profile, select the EEG channels used by the experiment and declare their type
as `eeg`; preview, save and apply that mapping before starting a recording.
The prepared decoder uses the observed sample rate. Neither electrode positions
nor a manually duplicated sample rate is required.

The default source selection is File Reader, so it must be changed for this run.
Acquisition/decoding does not require launching visual stimulation. If stimulation
is also needed, the bundled Scan45 parallel trigger is Windows-only; select a
suitable installed trigger or run stimulation on a compatible Windows host.
Synthetic signal tests do not validate physical trigger timing or device settings.

The simulated TCP test is executable with:

```sh
python -m pytest tests/infrastructure/test_curry8_runtime_integration.py
python -m pytest tests/infrastructure/test_classic40_short_epochs.py
```

The TCP test uses 500 Hz and 700 float32 samples across five signal channels and
one event row. It loads the configured Curry plugin, acknowledges preparation
without expected source dimensions, records all rows exactly, selects O2/O1/Oz
as EEG, and produces target 1 for a simulated 8 Hz input through the real CCA
and default epoch detector. It exercises the runtimes in one process; the CNT
mapping integration test separately covers spawned workers and application-level
session/recording creation without source expectations.
The simulator follows the current driver's protocol contract; it does not prove
vendor conformance. Acquisition start/stop control packets invalidate cached
Curry metadata. Restarting the amplifier during a run still requires recovery
and fresh preparation; automatic recovery and real-device control-packet order
have not been established by these tests.

Short-epoch regressions cover adjacent equal or different events within and
across packets. CCA declines fewer than two post-offset samples; FBCCA declines
windows no longer than its filter padding. Both continue processing the next
valid epoch. These checks retain forced decoding of usable partial windows.

Local verification on 2026-10-18 used macOS and Python 3.14. The full headless
run passed 3,157 tests; its other 32 tests failed solely because the sandbox
denied loopback socket binding, and all 32 passed when rerun with socket access.
The 17 new short-epoch cases were verified separately as part of 112 passing
decoder/processing regressions. All 27 integration tests passed with socket
access, with one skip. The frontend's 152 tests, TypeScript check and production
build passed, as did all eight architecture contracts. No hardware or other-OS
run was performed.

The local raw-session recording and format-boundary update on 2026-10-17 passed
all 2,976 headless backend tests. The complete integration suite passed 26 tests
with one skip. Coverage includes real-worker recording, session switching,
failure recovery, alternative writer injection, writer construction under spawn,
legacy file compatibility, and rejection of unsupported formats and versions.
All seven architecture contracts passed. The preceding API update also passed
all 152 frontend tests, TypeScript checking, and generated REST contract checks.
These checks use synthetic signals and file replay on macOS, not live hardware
or other operating systems.

The Signal Processing refactor on 2026-10-17 passed the complete headless backend
suite on macOS with Python 3.14: 2,768 passed, one skipped, and two dependency
deprecation warnings. All six architecture contracts passed. Coverage includes
window/latest-sample contracts, configuration transaction retries, trial
completion failures, strict decoder outputs, runtime worker supervision,
cross-process queue timing, and buffer equivalence with the original NumPy
append path. Actual CCA and FBCCA predictions agree with the reference buffer;
their implementations and plugin timing parameters were not changed. These
local results do not establish Windows/Linux or physical-device behavior.

The two-panel Signal Processing channel binding update on 2026-10-12 passed 372 focused
backend tests and all 96 frontend tests. One recording integration test required
a rerun with loopback sockets enabled after the sandbox blocked engine startup.
Production build, generated REST type consistency, and all six architecture
contracts passed. Browser checks against an isolated backend verified subset
selection, explicit source-to-input aliases, ordering, mapping preview, unchanged
Acquisition metadata, profile save/reload, dynamic all-channel selection, and
responsive layout at 1440 by 1000 and 390 by 844. Runtime coverage confirms that
bound signals enter Epoching and pre-trigger buffering before ordered window filters
and decoding. The complete backend suite was not repeated for this update.

Decoder channel requirements and the offline authoring guide on 2026-10-12 were
verified locally with Python 3.14 on macOS. The full backend run produced 2,603
passes, one skip, and 11 failures in historical test fixtures that omitted typed
source metadata or prepared decoder context. Those fixtures were corrected
without weakening the live update, rollback, or plugin-construction assertions;
all failed cases passed in focused follow-ups. The final combined configuration,
source, runtime, and worker recheck passed 284 tests; a separate batch passed all
39 live rebuild, rollback, and narrow-apply tests. Legacy profile compatibility
also covers transient all-source input preparation without rewriting saved
profiles or revisions.
The complete backend suite was not repeated after the fixture corrections.

All 90 frontend tests, TypeScript checking, production build, generated REST type
consistency, and six architecture contracts passed. The executable Decoder starter
passed three ZIP installation, discovery, construction, and named-channel tests.
Browser checks at 1365 by 900 and 390 by 844 verified the guide layout, offline
access, and downloaded manifest bytes against the source file. No desktop release
package was rebuilt for this follow-up; physical-device and other-OS checks remain
release validation work.

The Signal Processing source-binding refactor on 2026-10-11 was verified locally with
Python 3.14 on macOS. The full backend run produced 2,553 passes, one skip, and
two failures from integration assertions that still expected the retired
modern-plan `setup` field. After updating those assertions to `processing_input`
and wiring acquisition ownership in the rebuild test fixture, all 39 live rebuild,
rollback, and narrow-apply tests passed. The session REST/WebSocket end-to-end
test and real CNT mapping integration also passed separately. The complete suite
was not repeated after these test-only corrections; production code was unchanged.
Coverage includes same-plugin source profile switches, channel reordering,
reconnection, stale preview rejection, pinned inputs versus saved drafts,
inspectable source conflicts, and legacy plan serialization. All 82 frontend
tests, TypeScript checking, production build, OpenAPI/REST consistency, and all
six architecture contracts passed. Tests that bind loopback or Unix sockets ran
outside the restricted execution sandbox.

The acquisition-profile montage follow-up on 2026-10-10 passed focused backend
batches of 195 configuration/API/domain checks, 125 profile/storage checks, 26
signal/API-contract checks, 61 acquisition/bootstrap checks, and 34 processing/API
checks including real CNT acquisition across processes. These batches overlap.
Coverage includes persistent source observations, missing-field manual fallback,
read-only device facts, stale-profile and concurrent-write rejection, pinned
manual inputs, connection invalidation, and device/configuration conflicts.
All 70 frontend tests, type checking, production build, OpenAPI/REST consistency,
and six architecture contracts passed. Frontend regressions also cover changed
channel counts, generated names, and avoiding reuse of a file's observed
coordinates as manual declarations for another file. The full backend suite and
physical-device/other-OS release checks were not rerun for this follow-up.

Montage validation on 2026-10-10 used the existing Python 3.14 environment on
macOS. The final focused suites passed 153 runtime/API tests and 224
domain/configuration/plugin tests (these suites overlap). The complete integration
suite passed 24 tests with one skip; the real CNT montage and command-confirmation
process tests also passed after the final acquisition fix. The full headless run
had 2,361 passes and five failures: a lightweight source test double lacked a
connection property, and the event-routing expectation omitted the two new
commands. Those failures were corrected and covered by the final focused suites;
the complete headless suite was not rerun after those fixes.

All 57 frontend tests, type checking, production build, OpenAPI/REST type
consistency, and six architecture contracts passed. A clean temporary wheel was
installed outside the checkout and checked without the editable-package import
hook: all ten contributions, 281 stimulus frames, new montage modules, and the
installed MNE electrode template resolved successfully. Template coordinates
were explicitly verified in the head frame and meters. Browser screenshot QA
was unavailable because the computer-use runtime exposed no browser surfaces.
These checks do not establish physical Scan45/CURRY behavior or Windows/Linux
results; those remain release validation work.

Local verification on 2026-09-24: 1,927 headless/backend tests passed, including
real-process integration. The final source-capability change was then rechecked
with 49 relevant coordinator, failure, and process tests. All 18 frontend tests,
the production build, generated REST type check, and five import contracts passed.
A clean temporary wheel verified 10 configured contributions and 281 stimulus
frames without importing the source checkout.

Local results only establish behavior on the machine that ran them. Adding a CI
matrix does not establish that Windows or Linux jobs have passed: inspect the
actual workflow results for each commit. The current development environment is
macOS; other OS results require their corresponding runners.

The local console check used isolated configuration and plugin directories. It
verified profile editing, saving without activation, Apply review and completion,
the Run module-progress view, ZIP upload, explicit enablement, and the disabled
repository state. Frontend tests additionally cover uncertain Apply responses,
recovery after a confirmed stop, exact package identities, and version-profile
creation without automatic activation. Signed repository installation is tested
through a controlled HTTP repository and the application's real lifespan.

Final review regressions cover cancellation after a live network packet header,
safe resumption after source-version replacement, and clearing runtime quarantine
without erasing historical failed operations. Timing and sample continuity still
require the hardware checks below.

Headless tests do not validate physical EEG connectivity, parallel-port trigger
timing, display refresh/VSync behavior, or safe stopping of a physical actuator.
Those require the relevant hardware and display setup. Tests tagged `gui` or
`hardware` are excluded from both headless jobs. Fake effectors establish host
protocol ordering only; an installed driver must implement and validate its own
physical quiescence contract before automatic reconfiguration can use it.

## Signal Processing buffer and latency diagnostics

Epoch buffering grows a C-order backing array geometrically and exposes only its
valid prefix to the decoder. Appending writes after that prefix; older borrowed
windows keep their original samples and shape. Dtype promotion follows NumPy's
`result_type`, including empty appended segments. `latest_samples` remains the
original most recently appended segment, and completed `Epoch` objects still
copy and freeze their data. Capacity slack trades additional memory for fewer
copies; it does not bound the maximum epoch length.

Run `python scripts/benchmark_epoch_buffer.py` to compare the actual processing
path with the former `np.append` implementation. The benchmark uses a decoder
whose readiness gate stays closed, so it isolates buffering and readiness checks
from algorithm, filter, IPC, and completed-Epoch construction costs. Inputs are
allocated before measurement; wall-clock medians and peak `tracemalloc` memory
are measured separately. The benchmark checks exact output and dtype equality.
It is a local microbenchmark, not an end-to-end throughput guarantee.

Measured on 2026-10-17 after the full test suite finished, using macOS 14.8.3
x86_64, Python 3.14.2, NumPy 2.4.4, float64 data, seed 12, and three timed
repetitions per implementation:

| Channels | Total samples | Chunk samples | Append median (ms) | Capacity median (ms) | Append peak (MiB) | Capacity peak (MiB) |
| --- | --- | --- | --- | --- | --- | --- |
| 8 | 3,000 | 100 | 0.291 | 0.166 | 0.363 | 0.295 |
| 64 | 30,000 | 100 | 609.975 | 12.590 | 29.252 | 37.504 |
| 64 | 30,000 | 10 | 5,383.813 | 19.883 | 29.317 | 30.025 |

The long-window cases reduce repeated copying substantially, but the reserved
capacity increases peak memory in both of these cases. The measurements do not
include the new queue envelope, clock, or state timing diagnostics overhead.
The valid window keeps contiguous samples within each channel; its full array
need not be C-contiguous when unused capacity remains between rows.

With DEBUG logging enabled for
`omnibci.infrastructure.ipc.data_manager`, `queue_dwell_ms` measures monotonic
elapsed time from enqueue submission to completed dequeue on the same host.
This includes multiprocessing feeder, serialization, transport, and
deserialization time. Raw messages inserted directly into the underlying queue
are delivered without a timing measurement. This measures the age of a consumed
message, not queue depth, total pending signal duration, or acquisition-to-output
latency. It does not depend on acquisition timestamps or `Queue.qsize()`.

DEBUG logging for
`omnibci.infrastructure.signal_processing.processing_state` reports
chunk processing duration with sequence ID, sample count, and configuration
revision at entry. This includes boundary detection, epoch buffering and
decoding, trial completion, and pre-trigger history updates, including failed
attempts. It excludes queue waiting, signal preparation before state dispatch,
and drain-barrier processing. A chunk can cross a configuration boundary, so the
entry revision is not necessarily the revision of every epoch in that chunk.

The overload policy preserves samples and FIFO barrier ordering. Live acquisition
must report a failure and stop when its deployment's latency or capacity budget
cannot be maintained; it must not silently discard or reorder signal samples.
Offline playback should reduce producer throughput to preserve every sample.
These are requirements for a future overload controller: this change adds
measurements, not an automatic stop threshold, adaptive pacing, or a bounded
queue. The current file reader's fixed delay is not consumer-feedback pacing.
Until such a controller is configured and implemented, the queue remains
unbounded and operators must use the existing stop/drain controls when measured
latency exceeds their deployment budget.

## Session persistence and ownership

The default `PERSISTENCE.session_repository` is `sqlite`. Session metadata,
subject references, append-only Trial results, prepared signal plans, and raw-file
receipts are stored in `omnibci.sqlite3` in the platform application-data directory
for `OmniBCI`. `OMNIBCI_DATABASE_PATH` selects an alternative absolute database
path. `in_memory` remains available for tests and explicitly transient runs.
The database does not contain raw signal arrays or filtered epoch matrices.

The SQLite adapter uses transactions, WAL, foreign keys, and full synchronous
writes. Duplicate Trial events with identical content are idempotent; conflicting
content for the same session/trial number is rejected. A backend holds an OS
lease beside the database for its lifetime. Starting a second backend against
the same database fails instead of reconciling another backend's active sessions.

Before accepting commands after restart, the backend checks ownership of any
referenced recording writers. Old `running` sessions become `ended` with an
interruption reason; acquisition and stimulation are not resumed. A continuation
uses a new session ID and retains `continuation_of` and `subject_id`. Recovery
can locate a unique recording for an existing session whose start receipt was
lost. It does not import arbitrary orphan directories as invented sessions.

`omnibci.application.session_runtime.SessionRuntime` owns `BCISession` inside the
existing Signal Processing child process. `SignalProcessor` delivers decoded
Trial metadata to that runtime synchronously; the aggregate records and publishes
the result before the stream-drain acknowledgment. `SessionAppService` in the
main process coordinates commands, recording lifecycle, and persistent projections.
Acquisition writes the original signal through the `SignalRecorder` port.

For a failed or uncertain session, use `POST /api/v1/sessions/{id}/abort` with
`{"reason": "description of the failure"}`. Abort blocks new ordinary operations,
attempts drain and file closure, stops the old engine generation, and preserves
failure diagnostics. It closes the session only after worker shutdown and recording
ownership are confirmed. A timeout retains the recovery requirement; it does not
authorize another writer or turn a failed recording into `completed`.

## Local raw session recordings

Connect the acquisition source before creating a session. The normal session
creation path prepares the source and Signal Processing binding, then opens a
local raw recording for that session before samples flow. `POST /api/v1/recording/start`
combines this with starting the stream. Both this endpoint and `POST /api/v1/sessions`
accept an optional `subject_id`; omit it or pass `null` when no subject is known.
It is an opaque external reference, not an identity-management feature. Empty or
whitespace-only values and non-string values are rejected. A continuation creates
a new session and recording while retaining the subject reference.

Recordings use the platform application-data directory for `OmniBCI`, under
`recordings/`. Set `OMNIBCI_RECORDING_DIR` to an absolute path to select another
root. Each recording has a generated UUID directory; subject and session labels
are stored in metadata and are never used as directory names. Session queries
and JSON/CSV exports expose the local recording receipt in the optional
`recording` field. Existing records without recording metadata return `null`.

Each directory contains:

- `metadata.json`: format identifier/version, session/subject identifiers, continuation,
  source descriptor, runtime/configuration context, status, counts, and the last
  successful durability checkpoint.
- `chunks.npy`: consecutive NumPy arrays containing each plugin-produced raw
  matrix, including every source signal channel and the final trigger row,
  before Signal Processing channel mapping, filtering, epoching, or decoding.
- `chunks.jsonl`: one index entry per successfully written chunk, with its byte
  offset, sample count, sequence ID, acquisition timestamp, and configuration
  revision. Built-in acquisition timestamps are host Unix nanoseconds, not
  hardware sample timestamps.

Pausing the stream flushes the data and index while retaining the same open
recording. Ending a session stops and drains acquisition before finalizing its
recording. Creating a session while another is active drains and ends the old
session first, opens a separate recording, and restores a previously running
stream. An unfinished epoch is discarded at this boundary, while its original
samples remain in the old raw file. A shutdown or write failure leaves an
interrupted or failed status; an abrupt process exit can leave `recording` status
and partial trailing data. Only a successfully finalized recording reports
`completed`. The default database preserves the Session and Trial history across
restart and reconciles its associated raw-file receipts.

While recording, acquisition requests a flush approximately once per second;
pause and finish also force flushing. A successful checkpoint requires data
flush/fsync, index flush/fsync, and an atomic manifest update, in that order.
This guarantees the successfully checkpointed prefix under the filesystem's
durability semantics, not a fixed one-second maximum loss after power failure.
Synchronous disk operations can delay both acquisition and checkpoint scheduling.

Read indexed chunks lazily through the infrastructure reader:

```python
from omnibci.infrastructure.acquisition.recording import iter_recorded_chunks

for chunk in iter_recorded_chunks("/absolute/path/to/recording-uuid"):
    raw = chunk.raw_data  # Original dtype and trigger row are preserved.
```

The strict reader follows the chunk index; a single `numpy.load` call does not
read an entire multi-chunk file. `inspect_recording(path)` reports the valid
indexed prefix, checkpoint, and unindexed or invalid trailing bytes.
`iter_valid_recorded_chunks(path)` explicitly reads that validated prefix of a
partial recording. `recover_recording(path)` requires the writer's OS lease and
marks an abandoned active file interrupted without truncating its arrays or index.

Recovery rejects corruption inside the durable prefix. Complete indexed chunks
beyond the checkpoint can be readable, but were not covered by that checkpoint's
durability guarantee. Unindexed arrays cannot recover their original timestamps,
sequence IDs, or configuration revisions, so recovery does not invent those
values. These checks validate structure and boundaries; they are not bit-level
integrity checksums. Keep all three original files together.

Storage implementations follow `omnibci.domain.ports.recording.SignalRecorder`:
`start`, `append`, `flush`, and `finish`. AcquisitionRuntime receives this port;
AcquisitionWorker chooses the implementation through `recorder_factory` and
constructs it inside the worker process. Factories must be importable and
serializable under multiprocessing spawn; do not pass open writers or file
handles across processes. The default remains `LocalSignalRecorder`, which
implements the NumPy/JSON format. To add a format, implement this contract and
wire its factory at worker construction; Session and processing logic remain
independent of encoding and filenames.

Receipts and metadata now identify `format="omnibci.numpy-chunks"` and
`format_version=1`. A new implementation must use a distinct format identifier,
and incompatible layout changes must increment its version. The current reader
accepts original v1 manifests without a format identifier and rejects unknown
formats/versions before reading data. Legacy v1 files without checkpoints remain
readable, but do not acquire a retrospective durability guarantee.
`RecordedSignalReader` and `open_recording_reader` in
`omnibci.infrastructure.acquisition.recording_reader` isolate playback from the
file layout. Add the matching reader and recovery support when adding a storage
implementation; changing the default writer does not migrate old files.

## Immutable configuration history

`GET /api/v1/sessions/{id}/configurations` returns archived configuration snapshots
and their application attempts in the standard API response envelope. Snapshots
contain ordered plugin bindings, exact selected versions, materialized parameters,
decoder channel mappings, and selected runtime settings. They are keyed by
`config_revision` and do not depend on mutable Profile files. Trial records retain
the revision actually used for each result.

Session links select which revisions the endpoint returns. Each snapshot's
`applications` list is the global history of that revision, including its use in
other engine runs; it is not a per-session event timeline. Inspect each attempt's
`generation`, `session_id`, and `context` when interpreting it. Engine-start attempts
have no session ID because the engine becomes ready before a Session is created.

A candidate is archived before workers may apply it. Each application attempt
records its own generation, context, timestamps, status, and acknowledged receipts.
`pending` means the outcome has not been confirmed; a command timeout alone does
not make it `applied`. Known failures remain `failed`. If a candidate really applied
and a later step required rollback, history retains that applied candidate and a
separate rollback attempt. A rebuild associates the new revision with the new
continuation session, rather than claiming the old session used it.

Profile IDs/revisions and engine generations are excluded from immutable runtime
content identity. Unrelated host settings are not copied. Parameters declared
`x-scope="secret"` are redacted, with their paths listed in the archived binding.
Reading history does not require the original Profile or plugin to remain installed.
The archive records runtime inputs and receipts; it does not bundle plugin binaries,
capture filtered waveforms, or promise reproduction across changed core code and
implicit defaults.

## Replay of local recordings

Select the built-in Acquisition plugin `recording_replay` (display name
**Recording Replay**). Its `recording_path` accepts an absolute recording directory
or that directory's `metadata.json`. A completed recording is accepted by default.
Set `allow_partial=true` only to explicitly replay the validated prefix of an
inactive `interrupted` or `failed` recording. A file still marked `recording`
requires recovery first; a live writer prevents opening it for replay.

Replay preserves the stored dtype, all source channels and trigger row, and the
source sample rate. It paces delivery using that sample rate. Acquisition creates
new host timestamps for the replay run; these do not replace the original timestamps
retained in the source index. A replay run creates a new Session and recording,
with `source_recording` provenance identifying the original recording. Original
signal files remain unchanged.

The replay source supports stop/join/checkpoint/restore. A checkpoint identifies
the recording and the next undelivered chunk; changed files invalidate it. Explicit
checkpoint restoration resumes without repeating delivered chunks. Starting without
a restored checkpoint begins at the recording's first chunk.

## Raw recording performance validation

Writing currently runs synchronously in the acquisition process. Measure acquisition
intervals, append/flush latency, memory, and input-versus-file equality for the target
device and storage. The local soak workload is 128 signal channels plus a trigger
row at 2,048 Hz for 30 minutes. Synthetic local storage results do not establish
behavior for other disks or physical devices.
Inject slow writes and write failures when verifying that errors are reported and
shutdown remains explicit. A writer thread or queue should be introduced only when
those measurements justify the additional concurrency.

Run the reproducible writer-only workload with:

```sh
python scripts/benchmark_recording.py --seconds 1800 --channels 128 --sample-rate 2048 --output recording-benchmark.json
```

The [2026-10-17 local result](benchmarks/recording-local-2026-10-17.json) used
macOS 14.8.3, Python 3.14.2 and NumPy 2.4.4. Backend regression tests also ran on
the machine during this measurement. It verified all 57,600 chunks, 3,686,400
samples per channel, timestamps, float64 values and trigger rows after completion;
the original payload total was 3,804,364,800 bytes. Temporary signal files were
removed after verification.

| Measurement | Result |
| --- | ---: |
| Chunk interval / budget | 31.25 ms |
| Write plus periodic checkpoint, p99 | 3.50 ms |
| Write plus periodic checkpoint, maximum | 129.03 ms |
| Writes exceeding the chunk budget | 12 / 57,600 (0.021%) |
| Maximum scheduling lateness | 125.66 ms |
| Peak traced allocation | 3.10 MiB |

Timing includes synthetic array construction and synchronous append/checkpoint
work. Traced allocation is not process RSS. The test checks the writer's complete
output, not hardware driver buffers, IPC throughput or processing latency.

The default remains synchronous: p99 is below the chunk interval, and this
measurement does not isolate disk latency from scheduling, allocation and other
host work. It does expose occasional pauses beyond the interval, so the result
does not establish a hard real-time bound or lossless physical-device capture.
Before deploying a source that cannot tolerate such pauses, measure its buffer
and stop latency and introduce a bounded writer queue with explicit overflow
failure if needed. Queue capacity and shutdown deadlines require that device's
budget; an unbounded background writer would only conceal overload.

## Local commands

The 2026-10-17 architecture validation ran the full headless backend suite
(3,176 initial passes), followed by 98 passing tests in the affected command,
OpenAPI and configuration contract files after their four outdated expectations
were updated. Production code was unchanged between those runs. Real-process
integration tests reported 26 passed and one skipped. The frontend's 152 tests,
TypeScript typecheck, generated API type comparison and all eight architecture
import contracts passed.

```sh
python -m pytest -m "not gui and not hardware and not integration"
python -m pytest -m "integration and not gui and not hardware"
lint-imports
python scripts/export_openapi.py
npm --prefix frontend run codegen
npm --prefix frontend test
npm --prefix frontend run build
```
