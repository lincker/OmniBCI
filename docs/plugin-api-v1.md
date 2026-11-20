# Plugin API v1 Domain Contracts

Status: target API v1 contract. S1a/S1b, S1c processing methods and S1e Effector methods are implemented and verified. Stimulation/Paradigm/Renderer and trigger lifecycle work (S1d), including its withdrawn first batch, remains deferred. S1f and the coordinated constructor/SDK/manifest switch remain pending. See the [execution record](plugin-implementation-plan.md) and [Stimulation handoff](stimulation-interface-design.md#8-follow-up-branch-handoff).

Updated: 2026-11-20. Implementation baseline: `0bdd1dd2a296fea4b919d7f1a186a6fb76477b8e` on `refactor/module-interface`.

This draft refines the [architecture proposal](plugin-marketplace-design.md#4-public-api-sdk-and-module-hosts) and [backend review](backend-interface-review.md). API v1 names the system's unified pre-launch contract. Implementation must update existing plugins, hosts and callers together and preserve current functional behavior. No historical interface, constructor adapter or parallel execution path needs to remain.

This document owns Python signatures, value invariants and plugin lifecycle semantics. The architecture proposal owns manifest/distribution rules; the backend review owns code findings and implementation sequencing. Those documents reference this contract rather than defining alternative signatures.

The [Stimulation interface and rendering design](stimulation-interface-design.md) applies these contracts to module ownership, cleanup, trial admission and trigger timing.

Use the [canonical plugin terminology and module names](plugin-marketplace-design.md#3-concepts-and-architectural-boundaries): a Package declares Contributions; a Module Host constructs their Instances. The seven public role classes are contracts implemented by Contribution entry-point classes, and their methods are invoked on Instances. Package/catalog/profile/binding identities stay outside the SDK; these names add no public base class or export.

## 1. Scope and simplification rules

- Keep the public API sufficient for all seven existing contribution kinds. Reducing exposed types does not remove checkpoint/resume, preview, channel mapping, baseline correction, trial feedback, output settlement or package/configuration management.
- Pass only the plugin's parameters, its own resource directory and the signal layout needed by processing roles. Keep module/revision/session/connection identities and global configuration in the host.
- Use ordinary keyword arguments instead of generic construction contexts and single-purpose call wrappers. Retain typed values where their fields and invariants form a meaningful contract.
- Use normal Python exceptions. The host owns error categories, command receipts and operational identity; no plugin error hierarchy or SDK error-code registry is needed.
- Preserve existing array-based execution and module ownership. Do not add a general plugin lifecycle base, service locator, per-plugin process or compatibility dispatcher.
- Change existing implementations in place when implementation begins. A new export list or changing `api_version` alone does not complete the refactor.

`omnibci.sdk` remains a thin public export path for canonical domain contracts. Application plans and infrastructure runtime objects stay private. A separate SDK distribution is unnecessary.

## 2. Public export inventory

The target has **18 public names**. Each is required by a role, a structured boundary value, or a narrow injected capability.

| Group | Public names | Purpose |
| --- | --- | --- |
| Contribution roles | `SignalSource`, `Filter`, `Decoder`, `EpochDetector`, `Paradigm`, `TriggerEmitter`, `Effector` | Implement the seven existing contribution kinds. |
| Signal layout | `SignalInfo`, `ChannelDescriptor` | Describe sample rate and ordered channels without host identity. |
| Acquisition results | `SourceObservation`, `SourceCheckpoint` | Keep metadata inspection and resumable source state complete. |
| Processing results | `Prediction`, `EpochBoundary` | Return validated predictions and ordered sample boundaries. |
| Drawing | `RenderSurface`, `Color`, `Rect`, `TextureHandle` | Draw and manage textures without controlling the window or submission loop. |
| Trigger capability | `TriggerOutput` | Allow a paradigm to send/clear through the selected trigger without owning its lifecycle. |

The SDK exports the same objects the host uses, not duplicate public models. Canonical definitions stay with the corresponding domain ports/values. Standard-library and NumPy types keep their own import paths.

At the inspected baseline, the [SDK](../omnibci/sdk/__init__.py) had 17 exports. Relative to that baseline, the target retains 12 names, removes `Renderer`, `SignalChunk`, `FilterContext`, `PluginContext` and `PluginParameters`, and adds six required values/capabilities. S1c replaces only the `FilterContext` export with `EpochBoundary`; publishing the full inventory and removing `PluginContext` remain S1f. Generic contexts, a checkpoint subclass, decode-attempt/frame-phase wrappers, SDK-specific JSON aliases and an exception hierarchy are unnecessary for this contract.

The following remain private: internal `SignalChunk` timestamps/counters, retained `Epoch`, host-enriched `Classification`, source/connection/generation IDs, configuration revisions, mapping plans, fingerprints, command envelopes, runtime receipts, artifact locks, event buses, workers and repositories. Removing a public export does not remove its host functionality.

`RenderSurface` and `TriggerOutput` are narrower views of capabilities already required by the host. The same adapters/instances can implement them directly; no compatibility wrapper, RPC proxy or additional runtime layer is needed. They specify supported use, not a Python security sandbox.

## 3. Construction and allowed inputs

The agreed target removes `PluginContext`. Its original explicit dependency injection and metadata snapshots are preserved by these arguments. The remaining inputs are few, and processing can declare `input_signal` as required without a shared optional-field record. This decision applies to built-ins, marketplace packages, local imports and development snapshots; origin does not select an API variant or become a constructor field. See the [three source workflows](plugin-marketplace-design.md#91-three-entry-workflows-and-one-runtime-contract).

Use two explicit signatures selected by the contribution's declared kind:

```python
# Acquisition, paradigm, trigger and effector implementations.
def __init__(self, *, parameters: Mapping[str, object], resource_root: Path):
    ...


# Filter, decoder and epoch-detector implementations.
def __init__(
    self,
    *,
    parameters: Mapping[str, object],
    resource_root: Path,
    input_signal: SignalInfo,
):
    ...
```

`Mapping` comes from `collections.abc`; `Path` comes from `pathlib`. These are concrete authoring signatures, not a requirement for domain ports to import application construction types. The loader selects the signature from the kind before calling it. It does not probe signatures by catching constructor exceptions.

| Input | Required boundary |
| --- | --- |
| `parameters` | Only this contribution's schema-validated JSON object. The host copies it; plugins treat it as a snapshot. Values must be JSON-compatible with finite numbers and bounded size/depth, even though the annotation uses `object`. |
| `resource_root` | The selected plugin package's own verified resource directory on its execution node. It is not the installation inventory or a writable configuration directory. |
| `input_signal` | Required only for processing roles. It describes their actual mapped input; rate and channel metadata have one authority. No separate sample-rate field or original acquisition descriptor is supplied. |
| Runtime capabilities | Drawing and trigger operations enter paradigm setup. They are not placed in a constructor context or global service map. |

The constructor initializes configuration and local state. Source connection, model preparation, GPU/device access and resource release follow the relevant role lifecycle. Module import has no such side effects. An input-layout change requires a newly prepared binding.

Before import, application preparation resolves the exact artifact on its execution node and retains a pending reference. The host consumes that private binding without another catalog search; actual artifact/environment pins, readiness receipts and mutation guards stay outside the SDK. See the [preparation handoff](plugin-marketplace-design.md#98-preparation-handoff-and-mutation-authority). Code is imported once per artifact/process, while each binding gets a fresh instance; normal nested-package imports follow the [loader ownership rules](plugin-marketplace-design.md#47-code-loading-and-instance-ownership). Generic loading does not own channel mapping, device readiness or repository refresh.

The [current processing host](../omnibci/infrastructure/signal_processing/signal_processor.py) maps rows before detector/epoch execution. The [loader wiring](../omnibci/infrastructure/signal_processing/signal_configuration.py) must pass that mapped descriptor to all three processing constructors. CCA/FBCCA read `input_signal.sample_rate`; filters receive `pre_trigger_data` at each call. During S1c this layout is carried only in `context.input_signal`; the two constructor signatures above take effect together in S1f. The [logger effector](../omnibci/infrastructure/plugins/effectors/logger/effector.py) currently stores a module revision without using it in execution; host logging/receipts retain that identity instead of exposing it to every plugin.

## 4. Shared values and ownership

### Signal layout

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelDescriptor:
    channel_id: str
    index: int
    name: str
    signal_type: str = "unknown"
    unit: str | None = None
    name_provenance: Literal["generated"] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalInfo:
    sample_rate: int
    channels: tuple[ChannelDescriptor, ...]

    @property
    def signal_channel_count(self) -> int: ...

    @property
    def trigger_row_index(self) -> int: ...

    @property
    def output_row_count(self) -> int: ...
```

Use the standard-library `dataclass` and `Literal`. Preserve the existing [metadata invariants](../omnibci/domain/signal_info.py): positive integer rate excluding booleans, nonempty channels, contiguous ordered indices, unique stable channel IDs, normalized signal-type labels and explicit/unknown units. Native names can repeat; mapped-name and decoder-requirement checks remain in the host. Counts and final event-row index are derived from channels.

Remove `source_id`, `connection_id`, native-event description and provenance from the layout supplied to processing plugins. Source-native descriptions/provenance remain in `SourceObservation`; host identities remain in application bindings/receipts. Preserve the current fingerprint, stale-connection and mapping checks when updating those consumers. Do not create a second copy of `SignalInfo` to carry hidden host fields through the SDK.

### Arrays and predictions

Acquisition and detector matrices have shape `(C + 1, N)` with a final event row. Filters/decoders receive `(C, N)` signal rows. Validate rank and real numeric dtype; exclude boolean, complex and object arrays. Samples can contain NaNs independently of metadata validity. Normalized event values are finite, exact nonnegative integers; zero means no event.

Source reads transfer ownership of returned storage. The host lends read-only views to plugins; a plugin copies anything it needs to retain or mutate. Filters may return the unchanged input or relinquish new storage. No unconditional full-array copy is needed at every call.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Prediction:
    target_id: int
    confidence: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EpochBoundary:
    offset: int
    kind: Literal["start", "end"]
    event_id: int | None
```

Normalize Python/NumPy integer targets to Python integers, rejecting booleans and negative/fractional values. A target is an identifier; preserve the existing classic decoders' `1..40` convention. Confidence is absent or a finite non-boolean real value in `[0, 1]`. `None` from a decoder means no prediction; target zero is not a universal no-result sentinel. The host adds event/forced/time/correlation metadata to its internal result.

Keep chunk timestamp/sequence validation and transport boundaries inside the host. Correct the current nanosecond timestamp annotation and validate ingress without publishing counters or adding new stream IDs/sample-offset protocols merely to support this plugin API. The detector needs sample data and positions, not host transport metadata.

## 5. Role contracts

The following are required method signatures, with `ABC`/`Protocol` from the standard library and `np` denoting NumPy. Ellipses describe the proposed contract rather than method implementations. There is no universal activation base class.

### 5.1 Acquisition

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class SourceObservation:
    native_text: str
    signal_info: SignalInfo | None
    native_event: str
    provenance: Literal[
        "device-reported", "file-reported", "protocol-defined", "user-declared"
    ]
    layout_error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceCheckpoint:
    resource_id: str
    resource_revision: str
    state: Mapping[str, object]


class SignalSource(ABC):
    @property
    def trigger_mode(self) -> Literal["events", "level"]: ...

    @property
    def resume_mode(self) -> Literal["restart-only", "live-current", "checkpoint"]: ...

    async def connect(self) -> None: ...
    async def observe(self) -> SourceObservation: ...
    async def start_stream(self) -> None: ...
    async def read_chunk(self) -> np.ndarray | None: ...
    async def stop_stream(self) -> None: ...
    async def disconnect(self) -> None: ...
    def checkpoint(self) -> SourceCheckpoint | None: ...
    def restore_checkpoint(self, checkpoint: SourceCheckpoint) -> None: ...
```

The host owns lifecycle state. Successful transitions return `None`; exceptions report failure. Replace the current paired metadata calls with one observation and remove public connection/streaming flags. Connect before observing or starting; observe/refresh and checkpoint/restore only while connected and stopped, with no pending read. Observation is not a prerequisite for streaming. Restore stages the next start; it does not itself start reading.

If native metadata is readable but no usable layout can be produced, return its text with `signal_info=None` and a bounded, nonempty `layout_error`. A valid layout requires `layout_error=None`. This preserves the existing [partial-inspection behavior](../tests/infrastructure/test_source_metadata.py), including its explanation, without another result wrapper or exception hierarchy. Native text is display information, never a substitute for typed layout. An exception from `observe()` means the observation itself failed.

**Preserve acquisition preview without a typed descriptor.** Reads and preview can operate with `signal_info=None`; basic array/event validation still applies. Processing attachment requires validated layout and input preparation. When a descriptor exists, validate row count before fan-out. The existing [preview integration test](../tests/infrastructure/test_acquisition_preview_api_integration.py) exercises this behavior; do not remove it to simplify metadata handling.

A failed metadata refresh invalidates the cached descriptor and reports a failed inspection. It does not by itself prove the otherwise healthy source must disconnect or lose preview; processing remains unprepared until a valid observation exists.

One read is active at a time. A return is a nonempty matrix or clean EOF `None`; waiting for live samples remains an await. Cancellation/bounded read timeouts must allow the reader to settle before stop, checkpoint or disconnect. Cleanup tolerates partial connection and repeated calls; an unresolved read does not authorize concurrent replacement or teardown.

Both mode properties are required, available immediately after construction without I/O, and fixed for that configured instance. The host validates them before connecting; there are no inferred defaults or duplicate capability flags. They stay available when observation is skipped or fails. Putting them inside `SourceObservation` would incorrectly make metadata inspection a prerequisite for preview and recovery.

`trigger_mode` selects the existing host normalization of held levels versus discrete events. Preserve state across chunks and reset it at the existing stream-start boundary. The detector does not repeat normalization.

`resume_mode` is the single resume declaration. File/checkpoint sources must return a `SourceCheckpoint` while connected and stopped after the read is joined; `None` in this mode is a contract failure. Live-current sources resume without replay but may have a gap; restart-only sources cannot satisfy automatic resume. Non-checkpoint sources return `None` from checkpoint and reject unsupported restoration. The host checks the declaration before calling restore; there is no optional-method discovery or checkpoint subclass.

Checkpoint state is bounded, copied JSON opaque to the host. Required resource ID/revision identifies the input, not a host connection. The plugin verifies its resource when restoring; the host also verifies contribution/configuration pins. Preserve no-replay and changed-file checks in [FileReader](../omnibci/infrastructure/plugins/acquisition/file/reader.py) and [checkpoint selection](../omnibci/application/source_checkpoint.py).

**Adjustment:** update the [source port](../omnibci/domain/ports/acquisition.py), every built-in source, acquisition worker, metadata responses and runtime apply together. No boolean fallback or old constructor remains.

### 5.2 Filter

```python
class Filter(ABC):
    def apply(self, data: np.ndarray, *, pre_trigger_data: np.ndarray | None) -> np.ndarray: ...
```

Construction provides the prepared layout/rate. Each call supplies the accumulated post-trigger window and `pre_trigger_data`: the original mapped samples captured before the opening trigger, with the same channel order, rate and units. `None` or zero width means no usable reference. Capturing these samples does not apply baseline correction automatically. Every filter receives the same original reference, not the preceding filter's output. The argument names the data's timing; baseline correction is one possible use.

Preserve channel count/order/meaning, sample count, rate and units. Numeric dtype conversion is allowed. Keep ordered filters and validate each output's rank/shape/dtype. A v1 filter transforms a whole window; repeated calls are not incremental sample delivery. Immutable coefficients/local caches are sufficient; no external-resource lifecycle is added to pure filters.

**S1c implementation:** `FilterContext` is removed; the [baseline filter](../omnibci/infrastructure/plugins/algorithms/filters/baseline_correction.py) receives `pre_trigger_data` directly. The [pipeline](../omnibci/infrastructure/signal_processing/epoch/pipeline.py) validates every output before the next stage and lends read-only views without copying full matrices. Channel selection remains host-owned; rate/layout construction switches in S1f. Shape checks cannot prove that an algorithm preserved units or channel meaning; that remains an implementation obligation.

### 5.3 Decoder

```python
class Decoder(ABC):
    def setup(self) -> None: ...
    def begin_epoch(self) -> None: ...
    def should_decode(self, raw_window: np.ndarray) -> bool: ...
    def decode(
        self, filtered_window: np.ndarray, *, latest_raw_segment: np.ndarray
    ) -> Prediction | None: ...
    def teardown(self) -> None: ...
```

One prepared instance owns reusable model resources through setup/teardown. Calls are serialized on one execution owner: setup once, then begin/reset and execution for each epoch, then teardown after pending calls settle. Required `begin_epoch()` resets working state for every epoch, even repeated event IDs, while retaining the model. Stateless implementations explicitly do nothing. It receives no ground-truth event label or host identity. Preserve one active epoch; remove `for_new_epoch()` cloning/implicit reuse rather than introduce a session framework. Reuse the existing processing state machine, epoch processor and pipeline; an additional `ProcessingSession` layer is unnecessary.

Setup must complete before an input preparation or configuration application is acknowledged. A cancelled/rejected candidate releases partial resources; replacing a model first closes the old binding outside an active epoch. Reset cancels pending configuration before releasing the binding. Stop and exceptional exit release remaining models. Teardown is attempted once per instance, including partial setup failure; a failed attempt blocks reuse and faults the processing owner while other safe releases continue. There is no automatic cleanup retry or compatibility adapter.

The cheap gate sees the raw accumulated post-trigger window before filters. It returns a Python or NumPy boolean scalar; arrays, integers, `None` and other truthy values are rejected. Decode sees that window after filters plus the latest appended mapped segment before filters. The latter is not all samples received since the previous decode and can be an empty `(C, 0)` array. Neither input contains the event row or baseline. Input metadata was already supplied at construction, so it is not repeated inside a `DecodeAttempt` object.

The host still bypasses the gate at a closing marker and records a forced attempt. A decoder handles short windows and may return `None`; the host decides whether to continue collecting or close without a result. Preserve normal/drain duplicate-attempt protection. A prediction closes collection. No callback runs for an empty accumulated window.

**S1c implementation:** [Decoder](../omnibci/domain/ports/algorithm.py), CCA/FBCCA, [EpochProcessor](../omnibci/infrastructure/signal_processing/epoch/epoch_processor.py) and the existing processing owner use these methods together. CCA templates and FBCCA filter coefficients prepare once in setup; begin/reset retains them. Short-window behavior and target IDs remain. Pure filters do not acquire resource lifecycle methods.

### 5.4 EpochDetector

```python
class EpochDetector(ABC):
    def detect(self, raw_data: np.ndarray) -> tuple[EpochBoundary, ...]: ...
```

Pass the mapped matrix including its normalized event row, without a `SignalChunk` transport wrapper. Return all boundaries once per chunk. Offsets are integers in `[0, N)`, excluding booleans. A start carries a positive event ID; an end carries `None`. Return ascending offsets, end before start on a tie, with at most one boundary of each kind at an offset and no more than `2*N` results.

The host closes the old epoch before opening a new one, includes the start sample and excludes the closing sample. A repeated start closes the active epoch; an end while idle is ignored. Keep pre-trigger buffering and transitions in the host. Recreate detector state when an input is replaced so state cannot leak across streams.

**S1c implementation:** the two first-marker methods are removed. [ProcessingState](../omnibci/infrastructure/signal_processing/processing_state.py) invokes `detect` once and validates the entire batch before changing epoch state. The built-in detector scans the event row once and returns every nonzero marker as a start, preserving its marker meaning. Detection plus result construction/validation is O(N + K); sparse/empty chunks can have fixed overhead, so batch return does not imply every case is faster. See measured median/p99 results in the [execution record](plugin-implementation-plan.md).

### 5.5 Drawing and trigger capabilities

```python
class RenderSurface(Protocol):
    def upload_texture(self, image: np.ndarray) -> TextureHandle: ...
    def release_texture(self, handle: TextureHandle) -> None: ...
    def clear(self, color: Color = Color(0.0, 0.0, 0.0)) -> None: ...
    def draw_texture(self, handle: TextureHandle, rect: Rect) -> None: ...
    def draw_rect(self, rect: Rect, color: Color) -> None: ...


class TriggerOutput(Protocol):
    def send(self, event_id: int) -> None: ...
    def clear(self) -> None: ...


class TriggerEmitter(ABC):
    def setup(self) -> None: ...
    def send(self, event_id: int) -> None: ...
    def clear(self) -> None: ...
    def teardown(self) -> None: ...
```

Retain the existing small `Color` and `Rect` values: finite RGBA components in `[0, 1]`, and finite normalized-device coordinates with positive width/height. Keep the existing NDC origin at the center and upward y-axis; `Rect.x/y` identifies the rectangle's lower-left corner. A `TextureHandle` is opaque, surface-owned and valid until release/shutdown. Reject foreign/stale handles; repeated release of an already released owned handle is harmless. Upload consumes a positive-size `(H, W, C)` uint8 image with `C` in `1, 3, 4` before returning.

The host alone owns window initialization, event polling, frame submission and shutdown. All drawing/texture calls use its owner thread. Do not export the full [Renderer](../omnibci/domain/ports/renderer.py) or make plugins select concrete GPU adapters.

The trigger plugin owns device implementation; Stimulation owns its setup/clear/teardown order. Paradigms receive only send/clear operations. Send completes a driver call, not a measured hardware-delivery observation. Preserve the selected driver's event-code range, zero/clear semantics and pulse timing; callers must be updated consistently rather than silently changing free-run behavior. Explicitly configured no-trigger mode remains supported and is not a fallback for failed hardware.

**Adjustment:** move [Curry8Trigger](../omnibci/infrastructure/plugins/acquisition/neuroscan/curry8/trigger.py) hardware acquisition out of construction and add complete cleanup. Preserve/document its blocking clear behavior while measuring timing; changing threads is not a timing-neutral simplification.

### 5.6 Paradigm

```python
class Paradigm(ABC):
    def setup(self, *, renderer: RenderSurface, trigger: TriggerOutput) -> None: ...
    def present_trial(self, parameters: Mapping[str, object]) -> None: ...
    def present_result(self, result: Mapping[str, object]) -> None: ...
    def render_frame(self, elapsed_seconds: float) -> None: ...
    def is_presenting(self) -> bool: ...
    def teardown(self) -> None: ...
```

All methods run serially on the renderer owner thread. Setup receives required capabilities together; teardown uses retained references and tolerates partial initialization. The host validates separate constructor/trial/result schemas declared in the [manifest](plugin-marketplace-design.md#51-package-contents), retains operation/trial correlation and rejects stale work before calling the plugin.

Keep classic cued trials, explicit free-run trials and result feedback. Express payloads as plugin-owned JSON objects and update all callers: for example, cued requests can carry `event_id`, free-run requests a defined mode, and results `target_id`. Missing/malformed payloads do not silently select a different operation.

Declare the frame callback and readiness methods already required by the runtime. `is_presenting()` returns a Python boolean: true while a trial or feedback is active, false when another trial may be admitted. An exception or invalid return faults the instance; it cannot become synthetic readiness. Elapsed time is a monotonic duration since the rendering session began. Accepted requests, generic frame submissions and completion remain distinct host observations. No public `FramePhase` type or new claim of exact first-stimulus/physical-display timing is needed for existing functionality.

**Adjustment:** update [Paradigm](../omnibci/domain/ports/paradigm.py), [Classic40SSVEP](../omnibci/infrastructure/plugins/paradigms/classic40SSVEP/ssvep.py), Stimulator, render loop and dispatch schemas/callers together. Preserve cue/stimulus/feedback behavior and target numbering while removing duck-typed readiness and the separate hidden trigger-binding step.

### 5.7 Effector

```python
class Effector(ABC):
    def setup(self) -> None: ...
    def execute(self, command: Mapping[str, object]) -> None: ...
    def quiesce(self) -> None: ...
    def teardown(self) -> None: ...
```

The host validates its internal command envelope, then checks the device payload against the plugin's [manifest schema](plugin-marketplace-design.md#51-package-contents). It retains correlation/identity and passes only the device payload. Preserve the classification-driven payload and logger smoke-test loop. Setup, execute, quiesce and teardown use one serialized execution owner. Device operations must settle within their declared bounds.

Execute returning does not imply physical stop. Required quiesce confirms prior output has finished or been cancelled. Stop admission, settle pending/active calls, then quiesce and release. Cancelling an await or observing a timeout/process exit does not establish physical completion. Do not race teardown with a live device call or replay old commands. Preserve latest-pending replacement and explicit stale/superseded outcomes.

**Adjustment:** rename `play` to `execute`, require quiescence, and update [Actuator](../omnibci/infrastructure/actuation/actuator.py), its application loop, payload validation and all effectors together. Failed setup still receives partial cleanup; failed quiescence stays visible even if resource release succeeds.

## 6. Failures and resource ownership

No SDK exception hierarchy is required. Use `ValueError`/`TypeError` for invalid construction values and ordinary implementation exceptions for failures. The host supplies structured errors based on the operation and admission stage. Before invocation it can report schema/admission rejection; after an execution call begins, an exception is a failure with potentially uncertain effects, not proof that nothing happened. An exception's Python class alone must not turn runtime failure into a safe rejection.

Record contribution, configuration revision and operation IDs in the host. Never pass that entire context into a plugin merely for logging. Reject invalid results at the role boundary, including non-boolean gate/readiness returns; do not use truthiness or a missing method to select a fallback. Cleanup attempts all safe, applicable steps while preserving the original cause. It tolerates partial initialization and repeated release, but failed/unresolved cleanup cannot be reported as success. Teardown must not emit new work, and it cannot race an unsettled call that still owns the resource.

| Owner | Required execution discipline |
| --- | --- |
| Acquisition | One asynchronous read at a time; join it before stop, checkpoint or disconnect. |
| Processing | Ordered detector/filter/decoder calls; one active epoch; model setup/reset/teardown with one resource owner. |
| Stimulation | Marshal trial/result input, readiness, drawing and trigger operations to the renderer owner thread. |
| Actuation | One execution owner for setup, commands, quiescence and teardown; keep transport/heartbeat work responsive. |

Stimulation preparation initializes the renderer, sets up and clears the selected trigger, then sets up the paradigm with both capabilities. Shutdown stops admission and settles callbacks before paradigm teardown, trigger clear/teardown and renderer shutdown, in that order. Failed partial setup still gets matching cleanup; only clear a trigger whose setup completed, while its teardown handles any partial acquisition. A cleanup exception must not skip other safe releases. Keep one cleanup owner in the existing Stimulation host; do not add a generic lifecycle manager or let both the loop and supervisor independently tear down the same instance.

## 7. Complete replacement of existing code

API v1 is complete only after the existing runtime uses it end to end. Implementation must update the following together; these are planned changes, not work performed in this documentation step.

Follow the [implementation plan](plugin-implementation-plan.md) for ordering, affected files and verification. This table is the completeness checklist, not a separate interface-first implementation sequence. The plan changes each role contract with its actual host and plugin consumers and keeps new developer documentation until the implementation works.

| Existing path | Required update | Functionality to preserve and verify |
| --- | --- | --- |
| Domain ports/values and SDK | Publish the 18 canonical exports; narrow layout/capabilities; declare required methods; remove superseded public contexts/wrappers. | Type invariants, array ownership, mapped-channel identity, integer target rules and existing domain behavior. |
| [Loader](../omnibci/infrastructure/plugins/loader.py), catalog, manifests and runtime configuration | Dispatch the two explicit constructor signatures by kind, consume the prepared artifact, cache its code with fresh instances, accept only the new API `"1"`, and update all package declarations/schemas. | Static discovery, exact selection, configuration validation, relative imports and resource loading. No constructor retry, global re-resolution or old API dispatch. |
| Acquisition sources/worker and [runtime apply](../omnibci/application/services/runtime_apply_service.py) | Read fixed mode properties independently of observation; adapt checkpoints and host identity records. | File resume without replay, changed-file rejection, live-source restart, partial-inspection diagnostics, preview without a descriptor and independent processing attachment. |
| [Signal configuration](../omnibci/infrastructure/signal_processing/signal_configuration.py), pipeline, epoch worker and built-in algorithms | Pass mapped input at construction, direct baseline/array arguments and required decoder reset; consume ordered boundaries. | Named-channel selection, optional baseline, forced/short-window decoding, multiple markers, pre-trigger history and target IDs. |
| Stimulation manager, loop, paradigm and trigger drivers | Inject narrow capabilities, declare frame/readiness calls, marshal owner-thread work and update trial/result schemas. | Cued/free-run modes, result display, explicit no-trigger mode, trigger timing, pause/resume and partial cleanup. |
| Actuation translator/application, actuator and effectors | Keep typed envelopes private, project the payload and call execute/quiesce through one owner. | Correlated acknowledgments, latest-pending replacement, stale outcomes, no replay and the hardware-free logger loop. |
| Application services, DTOs/codecs and presentation callers | Update fields moved out of public values; keep host identity, metadata provenance, revisions and fingerprints in their proper records. Update generated contracts and actual callers if wire shapes change. | Existing validation/admission, profile apply/history/recovery, local/remote execution and client-visible information. |
| Installer/market/package references | Use one package declaration and installer for market and local artifacts; retain origin/admission in host records, scope mutation ownership per node/store and remove ordinary unmanaged source discovery. | Offline local import and installed-package preparation, signed repository checks at installation, immutable installs, idempotency/conflicts and pending-to-active/recovery reference protection. |
| Tests and author guides | Update fixtures/examples to the actual signatures, provide static validation/packaging and explicit development snapshots using the same loader/hosts. | Preserve existing scenario coverage; check all source workflows, development isolation, stop/restart and no imports during static operations. |

An internal renderer adapter or transport codec serves a current runtime boundary and may remain. A wrapper that exists only to keep the previous plugin contract working is unnecessary. Remove superseded paths after their actual callers have been updated.

The target accepts only `package.api_version = "1"`. `manifest_version = 2` remains the existing container-format discriminator; it does not preserve API 2 execution. Do not change unrelated wire/storage versions merely to make their numbers match. Host/version/platform eligibility checks remain useful and are not historical API compatibility layers.

During implementation, run the existing domain, source/resume/preview, processing, stimulation, actuation, plugin/market and architecture checks, plus external author examples for all seven kinds. Cross-platform/device claims still require relevant target checks. Current [manifest](plugin-manifest.md) and [decoder](decoder-plugin-development.md) guides continue to describe the current code until the coordinated implementation updates them.
