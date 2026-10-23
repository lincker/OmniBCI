# Configuration and plugin validation

## Automated checks

The `tests` workflow configures Python 3.14 jobs on GitHub's Linux, macOS, and
Windows runners. Each OS runs the headless unit suite and a separate integration
suite using real subprocesses, local IPC, and checked-in signal fixtures. The
headless suite includes runtime failure injection for drain, result publication,
checkpoint, startup, configuration persistence, output settlement, and unknown resume outcomes.

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

The Curry 8 path is TCP NetStreaming → source adapter → `SignalChunk` → IPC
queue → processing channel mapping → epoch collection → ordered filters → decoder
→ `TrialOutcome` → results view and WebSocket. The source adapter preserves
auxiliary channels and extracts the low eight bits of the native event row;
processing selects only configured signal inputs. No Session lifecycle or storage
writer is required.

| Boundary | Processing and checks | Runtime implication |
| --- | --- | --- |
| Connection | Negotiate protocol 802 or 803; read a 20-byte header and bounded body. | Unsupported versions, unknown packet identifiers and oversized packets fail with a protocol error. |
| Device metadata | Read sample rate, row count, sample size and 144-byte channel records; exclude the final event row from signal-channel descriptors. | Actual device dimensions feed processing. Labels are retained; missing labels receive generated names. Curry channel types remain `unknown`. |
| Experiment expectations | Compare only explicitly configured sample rate and output-row count with observed metadata. | Missing expectations use the device values. A provided mismatch still blocks preparation, even when the other field is absent. Profiles are not rewritten. |
| Processing preparation | Resolve channel IDs, ordering, aliases and declared types; check the decoder's channel requirements and acknowledge the plan for the current connection/configuration. | CCA/FBCCA require EEG inputs. Select actual EEG channels and declare `eeg` in processing input when Curry reports no types. Coordinates and a montage are not required. Stale plans and unavailable channels remain errors. |
| EEG packets | Decode supported int16/float32 payloads into channels × samples; retain only the low eight bits of each decoded value in the final trigger row, so 65280 becomes 0. Repeated trigger samples are retained. Ignore separate event-list and impedance packets. | Truncated/misaligned EEG bodies fail. Compressed streaming is currently unsupported and must be disabled on the server. No amplitude threshold, resampling or unit conversion is applied here. |
| Acquisition queue | Add host acquisition time and an increasing chunk sequence, then enqueue the original matrix. | The trigger row stays part of each chunk. There is no storage write or writer-ownership gate on this path. |
| Mapping and epochs | Select/reorder signal rows while preserving trigger values; detect boundaries and collect post-trigger samples plus separate pre-trigger history. | The default detector treats each nonzero trigger sample as a new start. No trigger means no decoded Trial, while raw acquisition can continue. |
| Filters and decoding | Apply configured filters in order to the accumulated window, then call the decoder. Baseline data is supplied separately to filters. | Baseline collection alone does not apply baseline correction. Closing boundaries can force a decode before the normal duration gate. A decoder may decline an unusable window with `None`. |
| Results and stopping | Validate decoder output and publish `TrialOutcome` directly; stop joins acquisition and drains processing. | Results are numbered within the engine generation. No classification is invented for a declined epoch. Publication or drain failures remain visible. |

For a minimal Curry run, select **Neuroscan CURRY 8**, set its host and TCP port
(default `4455`), start the engine and connect, then read metadata. Keep the
connection-only binding to use observed dimensions, or save and apply the
observed values to establish an experiment baseline. In the decoder
Profile, select the EEG channels used by the experiment and declare their type
as `eeg`; preview, save and apply that mapping before starting the data stream.
The prepared decoder uses the observed sample rate. Neither electrode positions
nor a manually duplicated sample rate is required.

Select the hardware source explicitly; a File Reader binding cannot connect to Curry.
Acquisition/decoding does not require launching visual stimulation. If stimulation
is also needed, the bundled Scan45 and CURRY 8 parallel triggers are Windows-only; select
**None (no trigger output)** for a preview without hardware markers, a suitable
installed trigger, or run stimulation on a compatible Windows host.
Synthetic signal tests do not validate physical trigger timing or device settings.

The CURRY 8 trigger tests use a simulated InpOut DLL to verify process-bitness
selection, byte/output mode, event codes, the send/clear sequence (including
repeated codes and a zero-code free run), and error propagation. Catalog tests
verify separate reader/trigger selection and platform restrictions. No DLL is
loaded during discovery. Real Windows port output, voltage, cable wiring,
marker capture and display-to-marker timing still require hardware verification.

The simulated TCP test is executable with:

```sh
python -m pytest tests/infrastructure/test_curry8_runtime_integration.py
python -m pytest tests/infrastructure/test_classic40_short_epochs.py
```

The TCP test uses 500 Hz and 700 float32 samples across five signal channels and
one event row. It loads the configured Curry plugin, acknowledges preparation
without expected source dimensions, verifies unchanged signal rows and low-byte
trigger values with both zero and 0xFF00 trigger baselines, selects O2/O1/Oz as EEG,
and produces target 1 for a simulated 8 Hz input through the real CCA
and default epoch detector. It exercises the runtimes in one process; the CNT
mapping integration test separately covers spawned workers, direct stream control,
reconnection and channel preparation without source expectations.
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

## Minimal runtime and result lifetime

`POST /api/v1/acquisition/stream/start` drains any previous read while the source
is stopped, prepares the selected Signal Processing pipeline against the connected
source, then starts reading. This clears partial epochs and pre-trigger history
after file EOF or a disconnect before new samples arrive. With no Decoder
selected, Acquisition can read on its own and the standby processor consumes the
queue. Connect does not load unrelated processing or output plugins.

The processor publishes each decoded `TrialOutcome` directly, without a Session
start command or a persistent Trial projection. `generation` identifies the
engine runtime and `trial_number` increases within that generation. Stopping and
starting a stream keeps the counter; rebuilding the engine creates a new
generation and a fresh counter. Each result retains its epoch's configuration
revision.

`POST /api/v1/acquisition/stream/stop` stops and joins acquisition, then places an
ordered barrier after the final chunk. Processing publishes all completed results
before acknowledging that barrier and clears partial epochs and pre-trigger
history. A drain failure cannot be reported as successful completion. Physical
output settlement remains a separate concern during runtime reconfiguration.

`GET /api/v1/results` exposes a bounded in-memory view of the latest 200 decoded
results for the current engine generation. WebSocket results use the same
`generation` and `trial_number` identity. These results are transient: the minimal
runtime has no Session API, SQLite trial store, automatic raw recording, result
export, configuration archive or Recording Replay plugin. Existing files on disk
are not deleted or migrated by this code removal. File Reader still supports
external CNT input.

The direct-processing regression tests cover publication without lifecycle
commands, monotonically increasing result numbers across stop/drain/start,
configuration boundaries, failed publication and ordered drain acknowledgments.
The Scan45 regression parses nine EEG channels plus the native event word,
maps CH1–9 to decoder channels 1–9, and verifies real CCA output reaches the
result view and WebSocket payload. The local Curry TCP simulator, CNT worker,
and EOF/reconnect restart tests also validate software behavior;
physical EEG hardware and marker timing still require device testing.

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
