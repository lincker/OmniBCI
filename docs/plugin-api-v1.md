# Plugin API v1 Domain Contracts

This document specifies the implemented **Plugin API v1** Python contracts: the canonical 18-name `omnibci.sdk` exports, two constructor signatures, value invariants, and role lifecycles. See the [manifest guide](plugin-manifest.md) for `manifest.toml` and schema rules, the [developer guide](plugin-development.md) for author workflows (`validate`, `package`, `develop`), and [validation](validation.md) for automated and physical-device verification.

Terminology follows the [manifest guide](plugin-manifest.md): a **Package** declares **Contributions**; an owning **Module Host** constructs their **Instances**. The seven public role classes are contracts implemented by Contribution entry-point classes, and their methods are invoked on Instances. Package, catalog, profile, and binding identities stay outside the SDK; these names add no public base class or export.

## 1. Scope and simplification rules

- Keep the public API sufficient for all seven contribution kinds. Reducing exposed types does not remove checkpoint/resume, preview, channel mapping, baseline correction, trial feedback, output settlement, or package/configuration management.
- Pass only the plugin's parameters, its own resource directory, and the signal layout needed by processing roles. Keep module, revision, connection, and generation identities and global configuration in the host.
- Use ordinary keyword arguments instead of generic construction contexts and single-purpose call wrappers. Retain typed values where their fields and invariants form a meaningful contract.
- Use normal Python exceptions. The host owns error categories, command receipts, and operational identity; no plugin error hierarchy or SDK error-code registry is needed.
- Preserve array-based execution and module ownership. Do not add a general plugin lifecycle base, service locator, per-plugin process, or compatibility dispatcher.

`omnibci.sdk` is a thin public export path for canonical domain contracts. Application plans and infrastructure runtime objects stay private. A separate SDK distribution is unnecessary.

## 2. Public export inventory

The [SDK](../omnibci/sdk/__init__.py) has **18 public names**. Each is required by a role, a structured boundary value, or a narrow injected capability.

| Group | Public names | Purpose |
| --- | --- | --- |
| Contribution roles | `SignalSource`, `Filter`, `Decoder`, `EpochDetector`, `Paradigm`, `TriggerEmitter`, `Effector` | Implement the seven contribution kinds. |
| Signal layout | `SignalInfo`, `ChannelDescriptor` | Describe sample rate and ordered channels without host identity. |
| Acquisition results | `SourceObservation`, `SourceCheckpoint` | Keep metadata inspection and resumable source state complete. |
| Processing results | `Prediction`, `EpochBoundary` | Return validated predictions and ordered sample boundaries. |
| Drawing | `RenderSurface`, `Color`, `Rect`, `TextureHandle` | Draw and manage textures without controlling the window or submission loop. |
| Trigger capability | `TriggerOutput` | Allow a paradigm to send/clear through the selected trigger without owning its lifecycle. |

The SDK exports the same objects the host uses, not duplicate public models. Canonical definitions stay with the corresponding domain ports/values. Standard-library and NumPy types keep their own import paths. Generic contexts, a checkpoint subclass, decode-attempt/frame-phase wrappers, SDK-specific JSON aliases, and an exception hierarchy are omitted.

The following remain private: internal `SignalChunk` timestamps/counters, internal epoch buffers, host-enriched `Classification`, source/connection/generation IDs, configuration revisions, mapping plans, fingerprints, command envelopes, runtime receipts, artifact locks, event buses, workers, and repositories.

`RenderSurface` and `TriggerOutput` are narrower views of capabilities already required by the host. The same adapters/instances implement them directly without a compatibility wrapper, RPC proxy, or additional runtime layer. They specify supported use, not a Python security sandbox.

## 3. Construction and allowed inputs

Construction uses ordinary keyword-only arguments with no `PluginContext` or API-version dispatch. This applies identically to built-ins, marketplace packages, local ZIP installs, and `omnibci-plugin develop` snapshots; package origin does not select an API variant or become a constructor field. See the [developer workflow](plugin-development.md).

Two explicit signatures are selected by the contribution's declared kind:

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

`Mapping` comes from `collections.abc`; `Path` comes from `pathlib`. These are concrete authoring signatures, not a requirement for domain ports to import application construction types. The loader selects the signature from the kind before calling it and never probes signatures by catching constructor exceptions.

| Input | Required boundary |
| --- | --- |
| `parameters` | Only this contribution's schema-validated JSON object. The host copies it; plugins treat it as a snapshot. Values must be JSON-compatible with finite numbers and bounded size/depth, even though the annotation uses `object`. |
| `resource_root` | The selected plugin package's own verified resource directory on its execution node. It is not the installation inventory or a writable configuration directory. |
| `input_signal` | Required only for processing roles. It describes their actual mapped input; rate and channel metadata have one authority. No separate sample-rate field or original acquisition descriptor is supplied. |
| Runtime capabilities | Drawing and trigger operations enter paradigm `setup`. They are not placed in a constructor context or global service map. |

The constructor initializes configuration and local state. Source connection, model preparation, GPU/device access, and resource release follow the relevant role lifecycle. Module import has no such side effects. An input-layout change requires a newly prepared binding.

Before import, application preparation resolves the exact artifact on its execution node and retains a pending reference. The host consumes that private binding without another catalog search; actual artifact/environment pins, verified source-byte digests, readiness receipts, execution history, and store-mutation guards stay outside the SDK. Code is imported once per artifact/process while each binding receives a fresh instance. Generic loading does not own channel mapping, device readiness, or repository refresh.

The [processing host](../omnibci/infrastructure/signal_processing/signal_processor.py) maps rows before detector/epoch execution, and [signal configuration](../omnibci/infrastructure/signal_processing/signal_configuration.py) passes that mapped `SignalInfo` to all three processing constructors. CCA/FBCCA read `input_signal.sample_rate`; filters receive `pre_trigger_data` at each call. The [logger effector](../omnibci/infrastructure/plugins/effectors/logger/effector.py) does not receive a module revision; host logging and receipts retain that identity.

## 4. Shared values and ownership

### Signal layout

```python
@dataclass(frozen=True)
class ChannelDescriptor:
    index: int
    name: str
    channel_id: str = field(kw_only=True)
    signal_type: str = "unknown"
    unit: str | None = None
    name_provenance: str | None = None


@dataclass(frozen=True)
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

[SignalInfo and ChannelDescriptor](../omnibci/domain/signal_info.py) enforce positive integer sample rates (excluding booleans), nonempty channels, contiguous zero-based indices, unique stable `channel_id` values, normalized lowercase `signal_type` labels, optional `unit`, and `name_provenance` in `(None, "generated")`. Native names can repeat; mapped-name and decoder-requirement checks remain in the host. Counts and final event-row index are derived from `channels`.

`source_id`, `connection_id`, native-event description, and provenance are excluded from the layout supplied to processing plugins. Source-native descriptions and provenance remain in `SourceObservation`; host identities, fingerprints, stale-connection checks, and mapping plans remain in host records.

### Arrays and predictions

Acquisition and detector matrices have shape `(C + 1, N)` with a final event row. Filters and decoders receive `(C, N)` signal rows. Rank and real numeric dtype are validated; boolean, complex, and object arrays are rejected. Samples can contain NaNs independently of metadata validity. Normalized event values are finite, exact nonnegative integers; zero means no event.

Source reads transfer ownership of returned storage. The host lends read-only views to plugins; a plugin copies anything it needs to retain or mutate. Filters may return the unchanged input or relinquish new storage. No unconditional full-array copy is performed on every call.

```python
@dataclass(frozen=True)
class Prediction:
    target_id: int
    confidence: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EpochBoundary:
    offset: int
    kind: Literal["start", "end"]
    event_id: int | None
```

Python/NumPy integer targets are normalized to Python `int`, rejecting booleans and negative/fractional values. A target is an identifier; built-in SSVEP decoders use `1..40` (Classic40) or `1..10` (Command10). Confidence is absent or a finite non-boolean real value in `[0, 1]`. `None` from a decoder means no prediction; target zero is not a universal no-result sentinel. The host adds event, forced, timestamp, and correlation metadata to its internal `Classification` and `TrialOutcome`.

Chunk timestamp/sequence validation and transport boundaries remain inside the host. Detectors receive sample data and positions, not host transport metadata.

## 5. Role contracts

The following are required method signatures, with `ABC`/`Protocol` from the standard library and `np` denoting NumPy. There is no universal activation base class.

### 5.1 Acquisition

```python
@dataclass(frozen=True, kw_only=True)
class SourceObservation:
    native_text: str
    signal_info: SignalInfo | None
    native_event: str
    provenance: Literal[
        "device-reported", "file-reported", "protocol-defined", "user-declared"
    ]
    layout_error: str | None = None


@dataclass(frozen=True, kw_only=True)
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

The host owns lifecycle state. Successful transitions return `None`; exceptions report failure. Connect precedes observing or starting; observe/refresh and checkpoint/restore run only while connected and stopped, with no pending read. Observation is not a prerequisite for streaming. `restore_checkpoint` stages the next start without starting reading.

If native metadata is readable but no usable layout can be produced, `observe()` returns its text with `signal_info=None` and a bounded, nonempty `layout_error` (at most 4096 characters). A valid layout requires `layout_error=None`. This supports [partial-inspection diagnostics](../tests/infrastructure/test_source_metadata.py) without another result wrapper or exception hierarchy. Native text is display information, never a substitute for typed layout. An exception from `observe()` means the observation itself failed.

**Acquisition preview operates without a typed descriptor.** Reads and preview can run with `signal_info=None` while basic array/event validation still applies (see the [preview integration test](../tests/infrastructure/test_acquisition_preview_api_integration.py)). Processing attachment requires a validated layout and input preparation. When a descriptor exists, row count is validated before fan-out.

A failed metadata refresh invalidates the cached descriptor and reports a failed inspection. It does not by itself disconnect an otherwise healthy source or disable preview; processing remains unprepared until a valid observation exists.

One read is active at a time. `read_chunk()` returns a nonempty matrix or clean EOF `None`; waiting for live samples remains an await. Cancellation and bounded read timeouts allow the reader to settle before stop, checkpoint, or disconnect. Cleanup tolerates partial connection and repeated calls; an unresolved read does not authorize concurrent replacement or teardown.

Both mode properties are required, available immediately after construction without I/O, and fixed for that configured instance. The host validates them before connecting; there are no inferred defaults or duplicate capability flags. They stay available when observation is skipped or fails.

`trigger_mode` selects host normalization of held levels versus discrete events. Normalization state persists across chunks and resets at stream start; the detector does not repeat normalization.

`resume_mode` is the single resume declaration. Checkpoint sources must return a `SourceCheckpoint` while connected and stopped after the read has settled; `None` in this mode is a contract failure. Live-current sources resume without replay and may have a gap; restart-only sources cannot satisfy automatic resume. Non-checkpoint sources return `None` from `checkpoint()` and raise `NotImplementedError` from `restore_checkpoint()`.

Checkpoint state is bounded (at most 65536 JSON bytes), copied JSON opaque to the host. Required `resource_id`/`resource_revision` identify the input, not a host connection. The plugin verifies its resource when restoring; the host also verifies contribution and configuration pins. See [FileReader](../omnibci/infrastructure/plugins/acquisition/file/reader.py) and [source checkpoint selection](../omnibci/application/source_checkpoint.py).

### 5.2 Filter

```python
class Filter(ABC):
    def apply(self, data: np.ndarray, *, pre_trigger_data: np.ndarray | None) -> np.ndarray: ...
```

Construction provides the prepared layout/rate. Each call supplies the accumulated post-trigger window and `pre_trigger_data`: the original mapped samples captured before the opening trigger, with the same channel order, rate, and units. `None` or zero width means no usable reference. Capturing these samples does not apply baseline correction automatically. Every filter receives the same original reference, not the preceding filter's output.

Preserve channel count, order, meaning, sample count, rate, and units. Numeric dtype conversion is allowed. Ordered filters are validated stage by stage for rank, shape, and dtype. A v1 filter transforms a whole window; repeated calls are not incremental sample delivery. Immutable coefficients and local caches are sufficient; pure filters have no external-resource lifecycle.

The built-in [baseline filter](../omnibci/infrastructure/plugins/algorithms/filters/baseline_correction.py) receives `pre_trigger_data` directly, and the [epoch pipeline](../omnibci/infrastructure/signal_processing/epoch/pipeline.py) validates every output before the next stage and lends read-only views without copying full matrices. Shape checks cannot prove that an algorithm preserved units or channel meaning; that remains an implementation obligation.

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

One prepared instance owns reusable model resources through `setup`/`teardown`. Calls are serialized on one execution owner: `setup` once, then `begin_epoch` and execution for each epoch, then `teardown` after pending calls settle. Required `begin_epoch()` resets working state for every epoch, even repeated event IDs, while retaining the model. Stateless implementations explicitly do nothing. It receives no ground-truth event label or host identity. One epoch is active at a time, and there is no per-epoch cloning hook.

`setup()` must complete before an input preparation or configuration application is acknowledged. A cancelled or rejected candidate releases partial resources; replacing a model first closes the old binding outside an active epoch. Reset cancels pending configuration before releasing the binding. Stop and exceptional exit release remaining models. `teardown()` is attempted once per instance, including after partial setup failure; a failed attempt blocks reuse and faults the processing owner while other safe releases continue.

`should_decode()` sees the raw accumulated post-trigger window before filters. It returns a Python or NumPy boolean scalar; arrays, integers, `None`, and other truthy values are rejected. `decode()` sees that window after filters plus `latest_raw_segment`: the most recently appended mapped segment before filters (which is not all samples received since the previous decode and may be an empty `(C, 0)` array). Neither input contains the event row or pre-trigger baseline.

The host bypasses `should_decode()` at a closing marker and records a forced attempt. A decoder must handle short windows and may return `None`; the host decides whether to continue collecting or close without a result. Duplicate attempts without new samples are suppressed during normal streaming and drain. A `Prediction` closes collection. No callback runs for an empty accumulated window.

[Decoder](../omnibci/domain/ports/algorithm.py), the built-in CCA/FBCCA decoders, and [EpochProcessor](../omnibci/infrastructure/signal_processing/epoch/epoch_processor.py) follow this contract: CCA templates and FBCCA filter coefficients prepare once in `setup()` and are retained across `begin_epoch()` resets.

### 5.4 EpochDetector

```python
class EpochDetector(ABC):
    def detect(self, raw_data: np.ndarray) -> tuple[EpochBoundary, ...]: ...
```

`detect()` receives the mapped matrix including its normalized event row (no `SignalChunk` transport wrapper) and returns all boundaries in that chunk. Offsets are integers in `[0, N)`, excluding booleans. A `"start"` boundary carries a positive `event_id`; an `"end"` boundary carries `None`. Results must be ordered by ascending offset, `"end"` before `"start"` on a tie, with at most one boundary of each kind at an offset and at most `2 * N` entries.

The host closes any active epoch before opening a new one, includes the start sample, and excludes the closing sample. A repeated start closes the active epoch; an end while idle is ignored. Pre-trigger buffering and state transitions live in the host. Detector state is recreated when an input is replaced so state cannot leak across streams.

[ProcessingState](../omnibci/infrastructure/signal_processing/processing_state.py) invokes `detect()` once per chunk and validates the full returned batch before changing epoch state. The built-in detector scans the event row once and returns every nonzero marker as a `"start"` boundary.

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

`Color` validates finite RGBA components in `[0, 1]`, and `Rect` validates finite normalized-device coordinates with positive width and height. The NDC origin is at the viewport center with `+y` upward; `(Rect.x, Rect.y)` is the lower-left corner and may extend outside the visible viewport. A `TextureHandle` is opaque, surface-owned, and valid until release or shutdown; foreign and stale handles are rejected, while repeated release of an already released owned handle is harmless. `upload_texture()` consumes a positive-size `(H, W, C)` `uint8` array with `C` in `(1, 3, 4)` before returning.

The host alone owns window initialization, event polling, frame submission, and shutdown. All drawing and texture calls run on the renderer owner thread. The full [Renderer](../omnibci/domain/ports/renderer.py) protocol stays private to the host.

The trigger plugin owns device I/O; Stimulation owns `setup`/`clear`/`teardown` ordering. Paradigms receive only `TriggerOutput` (`send`/`clear`). A returning `send()` means the driver call returned, not that hardware delivery was optically or electrically verified. Explicitly configured no-trigger mode is supported and is never a silent fallback for failed hardware. [Curry8Trigger](../omnibci/infrastructure/plugins/acquisition/neuroscan/curry8/trigger.py) acquires its InpOut driver during `setup()`, releases it during `teardown()`, and preserves its blocking 20 ms clear behavior.

### 5.6 Paradigm

`render_frame()` receives `elapsed_seconds` from a monotonic clock relative to the start of the rendering session, while the host owns event polling and VSync frame submission. Paradigms that require a fixed monitor refresh rate may declare `required_refresh_rate_hz: float | None = None` for host startup, trial-admission, and runtime validation.

```python
class Paradigm(ABC):
    required_refresh_rate_hz: float | None = None

    def setup(self, *, renderer: RenderSurface, trigger: TriggerOutput) -> None: ...
    def present_trial(self, parameters: Mapping[str, object]) -> None: ...
    def present_result(self, result: Mapping[str, object]) -> None: ...
    def render_frame(self, elapsed_seconds: float) -> None: ...
    def is_presenting(self) -> bool: ...
    def teardown(self) -> None: ...
```

All methods run serially on the renderer owner thread. `setup()` receives both required capabilities together; `teardown()` uses retained references and tolerates partial initialization. The host validates separate constructor, trial, and result schemas declared in the [manifest guide](plugin-manifest.md#manifest-version-1), retains operation/trial correlation, and rejects stale work before calling the plugin.

[Classic40SSVEP](../omnibci/infrastructure/plugins/paradigms/classic40SSVEP/ssvep.py) uses `{"event_id": 1..40}` for cued trials, `{"event_id": 0}` for explicit free run, and `{"target_id": 1..40}` for result feedback. Host validation copies the declared JSON object without applying defaults; missing or malformed payloads never silently select a different operation. REST/IPC batches retain `n_trials` and require an equally sized `trial_params` object list (1–10000 trials, aggregate JSON at most 1 MiB). Numeric frontend controls declare `payload_key`; acknowledgements return `trial_params`, while operation identity stays host-owned.

`is_presenting()` returns a Python `bool`: `True` while a trial or feedback is active, `False` when another trial may be admitted. An exception or invalid return faults the instance.

### 5.7 Effector

```python
class Effector(ABC):
    def setup(self) -> None: ...
    def execute(self, command: Mapping[str, object]) -> None: ...
    def quiesce(self) -> None: ...
    def teardown(self) -> None: ...
```

The host validates its internal command envelope, then checks the device payload against the plugin's [manifest command schema](plugin-manifest.md#manifest-version-1). It retains correlation and identity and passes only the device payload to `execute()`. [Actuator](../omnibci/infrastructure/actuation/actuator.py) serializes `setup`, `execute`, `quiesce`, and `teardown` on one execution owner. Device operations must settle within their declared bounds.

`execute()` returning does not imply physical stop. Required `quiesce()` confirms that prior output has finished or been cancelled. Shutdown stops admission, settles pending/active calls, then calls `quiesce()` and `teardown()`. Cancelling an await or observing a timeout or process exit does not establish physical completion. Failed setup still receives partial cleanup, and failed quiescence remains visible even if resource release succeeds.

## 6. Failures and resource ownership

No SDK exception hierarchy is required. Use `ValueError`/`TypeError` for invalid construction values and ordinary implementation exceptions for failures. The host supplies structured errors based on the operation and admission stage. Before invocation it can report schema or admission rejection; after an execution call begins, an exception is a failure with potentially uncertain effects, not proof that nothing happened.

Contribution, configuration revision, and operation IDs stay in host records and are never injected into plugins merely for logging. Invalid returns at role boundaries (including non-boolean readiness/presenting values) fault the instance. Cleanup attempts all safe, applicable steps while preserving the original cause; it tolerates partial initialization and repeated release, and failed/unresolved cleanup is never reported as success. Teardown must not emit new work or race an unsettled call that still owns the resource.

| Owner | Required execution discipline |
| --- | --- |
| Acquisition | One asynchronous read at a time; join it before stop, checkpoint, or disconnect. |
| Processing | Ordered detector/filter/decoder calls; one active epoch; model setup/reset/teardown with one resource owner. |
| Stimulation | Marshal trial/result input, readiness, drawing, and trigger operations to the renderer owner thread. |
| Actuation | One execution owner for setup, commands, quiescence, and teardown; keep transport/heartbeat work responsive. |

Stimulation preparation initializes the renderer, sets up and clears the selected trigger, then sets up the paradigm with both capabilities. Shutdown stops admission and settles callbacks before paradigm teardown, trigger clear/teardown, and renderer shutdown, in that order. Failed partial setup gets matching cleanup: only clear a trigger whose setup completed, while its teardown handles any partial acquisition. A cleanup exception never skips other safe releases, and one host owns the full cleanup sequence.
