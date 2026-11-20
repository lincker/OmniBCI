# Backend Interface Review and Refactoring Recommendations

Status: baseline findings and target recommendations. Completed S1a–S1c and S1e implementation is recorded in the [implementation plan](plugin-implementation-plan.md). Stimulation work has resumed under the latest user instruction. D07 required methods/readiness are restored and verified; D08–D09/R03 narrow capabilities and lifecycle are implemented and verified; graphics validation, opaque handles and feedback replacement are implemented and verified headlessly. The first S1f batch implements and verifies constructor/SDK/API selection replacement. Static role declaration cleanup is also implemented and verified; runtime payload schemas and their host/client replacement are implemented and verified. S2e also records local/market origin and accepted evidence without widening plugin inputs. Exact artifact preparation and subsequent plan steps remain pending. See the [handoff](stimulation-interface-design.md#8-follow-up-branch-handoff). Historical findings below describe the inspected baseline unless marked resolved.

Baseline: `0bdd1dd2a296fea4b919d7f1a186a6fb76477b8e`, reviewed on 2026-11-18. This review follows the [plugin and marketplace proposal](plugin-marketplace-design.md): one new Plugin API v1, no historical compatibility paths, a thin SDK facade, and execution owned by backend modules.

Design updated on 2026-11-19: the public API defines role/data/capability contracts; the SDK publishes their canonical definitions and supports development; module hosts own construction, scheduling and lifecycle. The system has not launched, so the initial v1 can replace current contracts in one coordinated change without compatibility adapters.

The [Plugin API v1 domain draft](plugin-api-v1.md) now supplies the concrete export inventory, signatures and invariants for D01–D10/K01. Its single-active-epoch decoder uses explicit reset and model setup/teardown; the recommendations below follow that refinement.

The simplified target has 18 public names and no generic plugin context or compatibility layer. Removal of `PluginContext` is agreed, preserving its original explicit injection and configuration isolation. API v1 requires updating the existing implementation and every affected caller while preserving functionality; the [replacement checklist](plugin-api-v1.md#7-complete-replacement-of-existing-code) makes that work explicit. Host-only identities and internal DTOs do not become SDK exports. Market releases, local archives/shared source and user development follow the same [source-workflow contract](plugin-marketplace-design.md#91-three-entry-workflows-and-one-runtime-contract).

Names follow the [canonical plugin terminology and module names](plugin-marketplace-design.md#3-concepts-and-architectural-boundaries). Plugin Management owns Packages, Configuration selects Contributions, and the four Module Hosts own Instances. Marketplace integration supplies release evidence and bytes. Code references below preserve current identifiers, including `PluginMarketRepository`; naming decisions are not claims that implementation or UI renaming has occurred.

The [implementation plan](plugin-implementation-plan.md) turns these findings into ordered work and records simplifications, acceptance gates and deferred portions. This review remains the evidence inventory; it does not require a new class or service for every named boundary.

The 2026-11-20 branch closeout adds [cross-module acceptance requirements](#cross-module-acceptance-addendum-2026-11-20) to the existing findings. The [closeout record](plugin-implementation-plan.md#7-current-branch-closeout-2026-11-20) separates retained S1a–S1c code, historical test evidence and pending work. These additions change no public API or implementation scope.

## 1. Conclusions and review scope

The backend already has useful separation: pure signal models, module-specific workers, explicit configuration snapshots, exact plugin selections, correlated command results, and ordered input boundaries. The main problem is that several interfaces declare less than their callers require, while other boundaries expose implementation objects or dictionaries whose invariants are checked repeatedly in different places.

Start by completing the domain contracts and their validation, then make the application ports express those contracts. Preserve the existing lifecycle and recovery behavior while changing interfaces. A wholesale replacement of the engine, event bus, configuration repository, or plugin installer is unnecessary.

Findings use two priorities:

- **P1:** settle before freezing Plugin API v1 or admitting external plugins through the new marketplace.
- **P2:** a concrete simplification or operational improvement, sequenced after the core contracts unless its affected path is being changed.

“Confirmed” means the declaration/caller mismatch or behavior is directly visible in code, with small executable probes where useful. “Design risk” identifies a weak boundary without claiming an observed production failure. “Proposal” describes the intended replacement, not a capability already implemented.

| Module or boundary | Current public surface | Review sections |
| --- | --- | --- |
| Domain values | `SignalChunk`, `SignalInfo`, `Epoch`, `Classification`, channel mapping and decoder input requirements | D01–D02 and preservation notes |
| Acquisition | `SignalSource`, acquisition commands/status, source inspection/checkpoints, preview and data delivery | D03, R01 |
| Processing | `Filter`, `Decoder`, `EpochDetector`, input preparation, pipeline configuration and drain | D04–D06, R02 |
| Stimulation | `Paradigm`, `Renderer`, `TriggerEmitter`, trial dispatch and pause | D07–D09, R03; [module design](stimulation-interface-design.md) |
| Actuation | `Effector`, command validation, latest-command mailbox and settlement | D10, R04, T02 |
| Engine and use cases | `EnginePort`, `EngineHandle`, module lifecycle, command broker and admission | A01–A03 |
| Configuration | Profiles, module snapshots, plan/apply, revisions, history and recovery | A02, A04 |
| Results | Trial/epoch read models and result delivery | A05, T03–T04 |
| Plugin Management and marketplace integration | SDK, catalog, loader, configuration catalog, package store and repository | K01–K07 |
| Transport and presentation | Local queues, peer protocol, HTTP responses and WebSocket messages | T01–T04 |

### Cross-document decisions

The 2026-11-19 cross-review uses the API draft as the authority for Python contracts, the architecture proposal for manifest/distribution rules, and this review for implementation findings. The agreed constraints are:

| Boundary | Decision applied across all three documents |
| --- | --- |
| Acquisition inspection | Fixed trigger/resume properties are available before metadata queries; partial layout failure retains native text and its explanation. Metadata-free preview remains supported. |
| Public information | Keep 18 canonical SDK names. Constructor inputs are parameters/resources plus prepared layout for processing; identities, command envelopes and runtime plans remain in the host. |
| Plugin sources | Market, shared packages/source and authored projects use one v1 API and installer. Local releases need no market account; development runs use temporary artifacts and separate configuration. |
| Plugin module boundaries | Package management owns installed artifacts; Configuration owns profiles; module hosts own imports and instances. Preparation pins bytes and environment on the execution node and retains existing pending/active/recovery protection. |
| Distribution versus execution | Installed packages prepare from local integrity and admission state. Repository refresh and installation do not become runtime dependencies; the installer owns the file-hash receipt. |
| Processing | Update the existing state machine, epoch processor and pipeline. Do not require a new `ProcessingSession` wrapper or expose attempt/transport objects. |
| Lifecycle | Use the API draft's explicit ownership and cleanup order. Invalid gate/readiness results fail; an unresolved operation is never treated as stopped. |
| Manifest and dependencies | Retain existing channel-requirement fields and fixed host dependencies. Add only required runtime payload schemas; no decoding-mode selector, extra payload version or selectable environment mechanism. |
| Replacement scope | Update existing implementations and actual callers together. Market expansion and frontend redesign remain separate work; current management and runtime functionality stays complete. |

These are corrections to the proposed design, not claims that the current code implements it. Necessary renderer/transport/presentation adapters serve current boundaries; they are not reasons to retain obsolete plugin signatures.

## 2. Domain contracts

### D01 Signal chunks need explicit clock and stream semantics

**P1 — confirmed mismatch and design risk.** [SignalChunk](/Users/lincker/workspace/OmniBCI/omnibci/domain/signal.py:24) annotates `timestamp` as `float`, while [Acquisition.fetch](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/acquisition/acquisition.py:595) supplies `time.time_ns()`. The timestamp represents host observation after a read, not necessarily the first sample's acquisition time. A chunk has a sequence number but no sample offset or declared clock. Its constructor checks dimensions and minimum rows, not timestamp/sequence types or correspondence with `SignalInfo`.

This makes timing assumptions easy to import into downstream consumers. It does not prove current stream mixing: the existing start markers, receipts, and barriers already protect input attachments.

**Adjustment:** keep `SignalChunk` private to the host, correct its timestamp annotation/unit documentation and validate sequence metadata. Pass the detector only the required sample matrix. Preserve existing counter scope, receipts and barriers; new stream IDs or sample-offset fields are not prerequisites for the public API. Host observation time is not a hardware clock or proof of gap-free acquisition.

Preserve the last-row trigger convention and ownership transfer. Avoid copying every chunk merely to make it appear immutable. Validate metadata cheaply at ingress and expose borrowed arrays as read-only where possible.

**Acceptance:** timestamp units survive round trips; negative/invalid sequence metadata is rejected; a source layout mismatch is rejected before mapping; a detach/reattach cannot consume data from the previous input; buffer ownership tests prove producers do not reuse handed-off storage.

### D02 Domain value validation is inconsistent

**P2 — confirmed with constructor probes.** [SignalInfo](/Users/lincker/workspace/OmniBCI/omnibci/domain/signal_info.py:88) has strict integer, channel-order, identifier, and row invariants. [Classification](/Users/lincker/workspace/OmniBCI/omnibci/domain/classification.py:41) checks ranges but accepts `target_id=True` and `target_id=1.5`. [Epoch](/Users/lincker/workspace/OmniBCI/omnibci/domain/epoch.py:48) accepts `sample_rate=NaN` because its positivity comparison does not reject NaN. [Prediction](/Users/lincker/workspace/OmniBCI/omnibci/domain/ports/algorithm.py:43) delegates validation to its consumer.

The normal decoder path already checks integer target types in [EpochProcessor](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/signal_processing/epoch/epoch_processor.py:238). The issue is inconsistent invariants across direct construction paths, not evidence that those invalid targets reach current physical outputs.

**Adjustment:** define canonical constructors for prediction/classification metadata. Reject booleans where integers are required, reject fractional target IDs, normalize allowed NumPy integer scalars explicitly, and validate finite confidence and timestamp values. Use the prepared `SignalInfo` as the rate authority and align internal `Epoch` validation with it. No public filter/construction context needs another sample-rate field.

Keep signal-data validity separate from metadata validity: rejecting a NaN sample rate does not imply banning NaN signal samples. Preserve `Epoch`'s copied, read-only snapshot and value equality. Do not introduce a generic validation framework for a few small constructors.

Publish `ChannelDescriptor` with a minimal `SignalInfo` containing rate and ordered channels. Derive final-row/count properties; keep source/connection identity in host records and native metadata/provenance in `SourceObservation`. Preserve fingerprint and stale-connection checks when updating those records. `Epoch` and host-enriched `Classification` stay outside the SDK; plugins return `Prediction`.

**Acceptance:** direct construction and runtime conversion agree on valid/invalid metadata; valid NumPy integer predictions remain supported; malformed metadata fails before result publication; existing epoch equality and channel-mapping tests pass.

### D03 SignalSource exposes two answers to one lifecycle transition

**P1 — confirmed contract ambiguity.** [SignalSource](/Users/lincker/workspace/OmniBCI/omnibci/domain/ports/acquisition.py:22) exposes mutable connection/streaming flags and async methods returning booleans. [The acquisition host](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/acquisition/acquisition.py:337) ignores the return values and checks the flags. An author can return a result that disagrees with the state the host observes.

Inspection also depends on an implicit sequence: `get_metadata()` must first populate internal state, then `describe_output()` reads that observation without another query. Typed metadata is optional even though processing preparation requires it. Checkpoint support is represented by capability booleans plus methods that can raise `NotImplementedError`.

**Adjustment:** make successful lifecycle methods return `None` and use ordinary exceptions; the host owns lifecycle state and structured error results. Specify cancellation, timeout and partial cleanup. Replace paired metadata calls with `observe -> SourceObservation`, containing native text, optional layout, provenance and a bounded layout-failure explanation. Preserve the [existing partial-inspection result](../tests/infrastructure/test_source_metadata.py): unavailable layout must not hide readable native text. Preserve streaming/preview without typed metadata; only processing attachment requires a prepared descriptor.

Declare `trigger_mode` and `resume_mode` as required read-only `SignalSource` properties, fixed after construction and requiring no I/O. The [current host](../omnibci/infrastructure/acquisition/acquisition.py:111) already selects trigger normalization independently of metadata. Keeping those capabilities outside observation preserves preview/recovery when inspection is skipped or fails. Do not introduce a capability DTO or retain parallel booleans.

Keep `None` from `read_chunk()` as clean EOF; waiting remains an await. Require cancellation or bounded read timeouts and join the reader before cleanup. Keep checkpoint/restore on `SignalSource`, with one declared resume mode and a `SourceCheckpoint` carrying resource identity/revision and opaque JSON state. A source without checkpoints returns `None` and the host rejects unsupported restore before invocation. No extra checkpoint subclass or parallel support flags are needed. Preserve no-replay resume without claiming gap-free acquisition.

**Acceptance:** connection success cannot disagree with reported state; partial connection failure is cleaned up; repeated stop/disconnect is defined; one observation preserves text and either a valid descriptor or its failure reason; level-trigger preview works without observation; failed refresh invalidates processing preparation without removing fixed capabilities; stale checkpoints fail without rewinding delivered samples.

### D04 Filter output can invalidate the decoder input contract

**P1 — confirmed and probed.** [Filter.apply](/Users/lincker/workspace/OmniBCI/omnibci/domain/ports/algorithm.py:28) advertises downsampling and channel selection. [ProcessingPipeline.run](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/signal_processing/epoch/pipeline.py:26) forwards each returned array without checking shape or updating metadata. Its context keeps the original sample rate, and the decoder receives the latest raw segment separately.

A probe with a two-channel input and a filter returning one channel reached the decoder with window shape `(1, 4)` and raw-segment shape `(2, 4)`. The existing [decoder guide](decoder-plugin-development.md) requires named-channel layout preservation. The declaration and execution boundary therefore disagree.

**Adjustment:** implement the proposal's limited v1 filter contract: preserve channel count/order, sample count and sampling rate. Validate array rank and shape after every stage and identify the offending contribution in errors. Pass read-only views of borrowed input/baseline arrays and test accidental in-place mutation. Preserve ordered filter execution.

Pass prepared `input_signal` at construction and `pre_trigger_data` as an explicit keyword to `apply`; remove the public `FilterContext` wrapper. These are the mapped samples before the opening trigger; baseline correction is one use of that reference. Repeated calls contain accumulated windows, so a filter cannot assume incremental delivery or silently advance persistent streaming state.

If resampling or channel transforms become required, first define a result carrying both transformed data and transformed signal metadata, including baseline/latest-segment semantics. Do not advertise those transforms through the current array-only return type.

**Acceptance:** a shape-changing filter fails at its boundary; decoder input metadata remains accurate; baseline handling remains optional and explicit; existing valid filters incur no unconditional full-array copies.

**S1c resolved:** explicit `pre_trigger_data`, read-only borrowed arrays and per-stage rank/shape/dtype checks now enforce this boundary. The SDK no longer exports `FilterContext`; constructor replacement remains S1f.

### D05 Decoder needs explicit reset and window semantics

**P1 — design risk with documented current behavior.** [Decoder](/Users/lincker/workspace/OmniBCI/omnibci/domain/ports/algorithm.py:56) receives a raw accumulated window in `should_decode`, a filtered accumulated window in `decode`, and only the latest raw appended segment as `latest_samples`. A boundary may bypass the readiness gate. A returned prediction closes collection; `None` can still mean the decoder changed internal state.

These are defensible window-decoding semantics, but array names alone do not expose them. The default `for_new_epoch()` returns `self`, making state isolation an author responsibility. Existing documentation discusses overlapping epochs, while the current state machine owns one active collection.

**Adjustment:** retain window decoding with `decode(filtered_window, *, latest_raw_segment)` and a cheap gate on the raw window. Input layout is already provided at construction; event/forced/correlation metadata stays in the host. Require `begin_epoch()` reset for every epoch without passing the opening label. One decoder owns model resources through setup/teardown; stateless implementations supply no-op reset. Remove `for_new_epoch()` without adding a decode-attempt wrapper or session framework.

Specify one active epoch as the supported runtime behavior. Explicit reset is simpler under that constraint and avoids ambiguous resource ownership among clones. Guaranteed incremental delivery or overlapping epochs require separate domain design; renaming `latest_samples` cannot provide those guarantees.

**Acceptance:** gate-skipped samples still appear in the accumulated window; the latest segment is never advertised as a complete delta; forced attempts handle short windows; every epoch resets working state; model setup/teardown occurs once per binding with partial cleanup; no duplicate decode occurs on an unchanged drain boundary.

**S1c resolved:** required setup/begin_epoch/teardown replace implicit instance reuse; `latest_raw_segment` is keyword-only. The host prepares candidates, resets every epoch, validates readiness and releases partial/replaced/cancelled/stopped models. Failed cleanup stops processing and cannot acknowledge successful replacement/reset. No session or compatibility layer was added.

### D06 EpochDetector forces the host to reconstruct a boundary sequence

**P2 — confirmed interface cost, not missing multi-marker support.** [EpochDetector](/Users/lincker/workspace/OmniBCI/omnibci/domain/ports/algorithm.py:96) returns the first start/end through separate `(index, event_id)` tuples and a `(-1, 0)` sentinel. [ProcessingState._boundaries](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/signal_processing/processing_state.py:30) repeatedly slices and rescans a chunk to recover all boundaries. The host already handles multiple markers and checks out-of-range offsets.

**Adjustment:** expose `detect(raw_data) -> tuple[EpochBoundary, ...]`, passing the mapped matrix with its event row and no host timestamp/sequence wrapper. Validate bounded offsets, end-before-start tie ordering and no duplicate kind at an offset. A start carries its event ID; an end carries `None`. Preserve start inclusion, closing-sample exclusion and detector reset on input replacement.

Keep pre-trigger history and epoch state transitions in the host; the detector only identifies boundaries. A separate event-stream framework is unnecessary.

**Acceptance:** existing multiple-marker, repeated-start, simultaneous-boundary and pre-trigger tests retain their results; invalid ordering/offsets fail deterministically; the detector cannot prevent forward progress.

**S1c resolved:** one batch detect call replaces suffix rescanning. The host validates all boundaries before applying any, including end-before-start ties. Cost is O(N + K) for the built-in path; empty/sparse fixed costs and dense-marker gains are measured in the implementation record, not assumed.

### D07 Paradigm omits methods required by the host

**P1 — confirmed missing contract.** [Paradigm](/Users/lincker/workspace/OmniBCI/omnibci/domain/ports/paradigm.py:17) declares setup, teardown and trial/result input, but [RenderLoop](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/stimulation/render_loop.py:89) requires `render_frame`. [Stimulator.is_presenting](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/stimulation/stimulator.py:139) treats an absent or failing readiness method as false. [Trial dispatch](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/stimulation/application.py:258) then admits the next queued trial.

**Adjustment:** declare `render_frame(elapsed_seconds) -> None` and `is_presenting() -> bool`. A failed query or non-boolean result faults the instance; it cannot mean “ready.” Setup receives required `RenderSurface` and `TriggerOutput` together; teardown uses retained references. Update trial/result methods, schemas and callers together while preserving cued/free-run/feedback behavior. No new public frame-phase type is needed.

Current [success reporting](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/stimulation/application.py:295) occurs after `present_trial()` returns. Preserve that acceptance meaning and distinguish host frame submission/completion where observed. Without a content-specific callback, the host must not label a generic submission as the first stimulus frame or physical display time. Such additional telemetry is not necessary to preserve the current trial flow.

**Acceptance:** incomplete paradigms fail preparation; readiness exceptions cannot drain queued trials; acceptance/presentation/completion are distinguishable; payload validation happens before plugin calls.

### D08 Renderer gives plugins control over the host lifecycle

**P2 — interface ownership risk.** [Renderer](/Users/lincker/workspace/OmniBCI/omnibci/domain/ports/renderer.py:54) combines window/context initialization, event polling, texture operations, drawing, buffer swap and shutdown. The entire object is passed to the paradigm, although initialization, polling, presentation and teardown ordering belong to the host.

**Adjustment:** expose a narrow `RenderSurface` protocol to paradigms for texture resources and drawing. Keep lifecycle and frame submission on a host-facing renderer adapter. Both protocols can be implemented by the same adapter; no extra process or proxy is needed. Publish only `RenderSurface` and its value types through the SDK, removing the full `Renderer` export in the coordinated v1 change. Specify coordinate, texture-format, lifetime and owner-thread requirements once. Keep renderer implementations in infrastructure.

This limits accidental misuse, not malicious Python access. Thread/lifecycle implementation defects are addressed separately in R03.

**Acceptance:** a paradigm's declared dependency has no initialize/shutdown/swap methods; setup, drawing and resource release use the same owner thread; renderer adapter tests cover resource lifetime and cleanup failures.

### D09 TriggerEmitter has no resource lifecycle or precise success meaning

**P1 — confirmed missing lifecycle.** [TriggerEmitter](/Users/lincker/workspace/OmniBCI/omnibci/domain/ports/acquisition.py:117) only declares `send -> bool` and `clear`. [Curry8Trigger](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/acquisition/neuroscan/curry8/trigger.py:22) loads a native driver and clears hardware in its constructor. [The paradigm](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/paradigms/classic40SSVEP/ssvep.py:174) ignores `send`'s boolean result.

**Adjustment:** move resource acquisition into setup/teardown and make send/clear completion-or-exception operations. Successful return confirms the driver call, not measured delivery. Expose only `TriggerOutput.send/clear` to paradigms. Preserve existing event-code and zero/clear semantics, updating free-run callers consistently rather than silently changing their behavior.

Stimulation owns the trigger lifecycle. Keep timing and pulse requirements visible: the current driver sleeps during clear, so it can block the rendering call path. Measure that behavior and specify allowed latency; moving the call to an arbitrary background thread would change trigger/frame alignment.

**Acceptance:** construction performs no hardware writes; failed setup is cleaned up; rejected trigger output reaches the module result; stop clears output and releases resources; device-specific timing is tested on an appropriate target.

### D10 Effector does not require the quiescence behavior the host needs

**S1e implemented (2026-11-20):** required execute/quiesce and None-return validation are implemented, with lifecycle calls and caller fixtures updated together. Sequence/freshness stay in the host. The shared manifest payload-schema switch remains S1f; final verification is recorded in the implementation plan.

**P1 — confirmed contract gap.** [Effector.quiesce](/Users/lincker/workspace/OmniBCI/omnibci/domain/ports/effector.py:19) is a concrete method that raises `NotImplementedError`. An implementation can satisfy the abstract class without supporting settlement, although [the runtime](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/actuation/application.py:364) relies on it. The `play` docstring also describes process termination as normal stopping, which is weaker than the explicit quiescence behavior now required by the host.

**Adjustment:** require quiescence explicitly in API v1 and rename `play` to `execute`. Separate “execute returned” from “all physical output is stopped.” Keep the typed standard command envelope in the host and pass only its schema-validated device payload to the plugin. Define lifecycle serialization, cancellation and timeouts. A timeout or process exit cannot be converted into a successful stop receipt.

Keep `setup`, command execution, `quiesce`, and `teardown` small and role-specific. Do not add a general plugin lifecycle superclass. Thread ownership is specified in R04.

**Acceptance:** a driver without quiescence fails before activation; setup failure attempts cleanup; stop waits for or explicitly reports uncertain physical completion; old intents are never replayed after recovery.

### Domain behavior to preserve

[Channel mapping](/Users/lincker/workspace/OmniBCI/omnibci/domain/channel_mapping.py:157) already resolves stable channel IDs or explicit indices, preserves events, validates mappings, and separates a source fingerprint from a connection identity. [DecoderChannelRequirements](/Users/lincker/workspace/OmniBCI/omnibci/domain/decoder_input.py:20) validates the final ordered input. Preserve these rules and keep them in the domain.

Plain `to_dict`/`from_dict` helpers do not by themselves violate Clean Architecture. Avoid moving every value into a separate serialization framework. Focus on repeated transport-specific receipts and runtime configuration dictionaries at actual module boundaries.

## 3. Application ports and coordination

### A01 EngineHandle exposes its internal graph

**P1 — confirmed abstraction leak.** [EngineHandle](/Users/lincker/workspace/OmniBCI/omnibci/application/ports/engine_port.py:12) exposes `event_bus`, acquisition/processing workers and `peer_gateway` as `Any`, plus writable stop flags. [NeuroEngineService](/Users/lincker/workspace/OmniBCI/omnibci/application/services/engine_service.py:48) consequently knows bus registration, peer lookup, process ownership, configuration transitions and module coordination. The factory accepts either a snapshot or no arguments; [startup](/Users/lincker/workspace/OmniBCI/omnibci/application/services/engine_service.py:152) supports both paths.

[StimulationAppService](/Users/lincker/workspace/OmniBCI/omnibci/application/services/stimulation_service.py:23) also names an infrastructure process-manager class under `TYPE_CHECKING`. This is source-level coupling, not a failing runtime import rule: the current architecture checker explicitly excludes type-checking imports.

**Adjustment:** narrow the existing application-owned `EnginePort`/`EngineHandle` to the required operations: start/wait-ready, request-stop/wait-stopped, dispatch/subscribe, module status, and peer status. Use one factory signature requiring an explicit runtime snapshot, including the empty-foundation case. Reuse current module/binding values for that input rather than requiring another wrapper solely for the `RuntimeSnapshot` name. Replace output process-manager annotations/duck typing with the small application-owned lifecycle protocol actually needed by their consumers, implemented directly by the existing managers.

Keep foundation lifecycle in `NeuroEngineService`. Move orchestration into existing module/application services only where it removes knowledge of another module's internals. Do not create one new coordinator per method or replace the current engine implementation merely to satisfy the port.

**Acceptance:** application code no longer dereferences worker, gateway or bus objects; all engine construction uses a pinned snapshot; startup barriers and unresolved-shutdown ownership remain intact; focused fakes implement the same public protocols as production adapters.

**S4a implementation (2026-11-20):** EngineHandle now exposes runtime operations, one explicit snapshot factory and required shutdown confirmation. Application services no longer traverse the graph or annotate infrastructure process-manager types. Existing managers implement application-owned structural lifecycle protocols directly. Four focused failures were reproduced before replacement; full validation is recorded in the [implementation plan](plugin-implementation-plan.md#s4a-execution-runtime-operations-and-generation-access).

### A02 Flat snapshots and receipts obscure module ownership

**P1 — design risk visible at multiple seams.** [Processing binding helpers](/Users/lincker/workspace/OmniBCI/omnibci/application/processing_source.py:13), [SignalSetupService](/Users/lincker/workspace/OmniBCI/omnibci/application/services/signal_setup_service.py:248), the engine, and plugin loading all consume keys such as `PLUGINS.bindings` and `CONFIG.module_revisions` from dictionaries. Input receipt fields are reconstructed and validated in application services and [SignalProcessor](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/signal_processing/signal_processor.py:407).

This is not a lack of validation: the code performs substantial checking. The risk is drift between duplicated field sets and a broad snapshot granting each module access to unrelated settings.

**Adjustment:** use typed snapshots and preparation/input receipts at the application ownership boundaries where the same fields are repeatedly reconstructed. Reuse existing `ModuleActivation`, `PluginSelection`, `SignalInfo`, `ResolvedSignalPlan` and receipt values rather than creating duplicate models. Extend an existing record when it already owns the meaning; a new `RuntimeSnapshot` or `PreparedInputReceipt` class is only justified by a distinct boundary. Pass a module's own configuration and the minimum shared runtime identity to its factory. Give pending/prepared/running bindings explicit artifact and environment locks when K03 is implemented.

Keep persistence/IPC/API dictionaries and plugin-defined JSON behind their validation/codecs. Private dictionaries within one owner do not require mechanical replacement; the purpose is to remove duplicated cross-module contracts and unrelated configuration access. Treat frozen dataclasses containing dictionaries as shallowly frozen; either copy at an ownership boundary or expose immutable values. Preserve the defensive collection-copy behavior already implemented by [ModuleActivation](/Users/lincker/workspace/OmniBCI/omnibci/application/configuration/models.py:239).

**Acceptance:** receipts round-trip through one codec; missing/extra/invalid identity fields fail consistently; one module cannot mutate another module's configuration; generation/revision/input identity checks remain mandatory.

### A03 Command ownership is inferred from transport numbers

**P1 — confirmed hidden coupling.** [Event](/Users/lincker/workspace/OmniBCI/omnibci/shared/events.py:56) carries an optional event type and `Any` payload. [CommandRequest/CommandResult](/Users/lincker/workspace/OmniBCI/omnibci/application/events.py:110) add useful correlation, but do not declare each command's payload/result relationship. [command_scopes](/Users/lincker/workspace/OmniBCI/omnibci/application/services/command_result_service.py:36) infers module ownership from numeric event ranges with exceptions.

A new command can have a valid transport number and still acquire the wrong runtime scope. Tests and callers must also know which fields are expected in a particular result dictionary.

**Adjustment:** keep the existing correlation broker and event bus. Introduce an explicit command-contract table containing event code, owning scope, payload parser and result parser. Start with prepare/attach/drain, stimulation pause, and actuation settlement; keep the table closed for v1. Numeric IDs remain wire identifiers, not business ownership rules.

Make failure outcomes explicit in application values: rejection, execution failure, definitely unsent, execution unknown, and timeout. Preserve the broker's existing delivery-error distinction and late-result handling. A transport send returning true is not an execution receipt.

**Acceptance:** every dispatched command has an explicit scope and payload contract; invalid payloads fail before execution; no network loss triggers automatic replay; correlated late/duplicate/wrong-command results cannot complete another operation.

### A04 Configuration planning needs typed outcomes rather than more orchestration

**P2 — boundary clarity.** [RuntimeConfigurationService](/Users/lincker/workspace/OmniBCI/omnibci/application/services/runtime_configuration_service.py:498) selects strategies using string values and schema `x-apply` metadata. [RuntimeApplyCoordinator](/Users/lincker/workspace/OmniBCI/omnibci/application/services/runtime_apply_service.py:107) coordinates persistence, module transitions, compensation and history. A successful runtime application can correctly report `history_saved=False` if the later history write fails.

The current plan/apply split, compare-and-swap revisions, operation IDs, scope reservations and recovery quarantine are strengths. Replacing them with a generic settings update would lose essential behavior.

**Adjustment:** use closed strategy values and explicit runtime/configuration/history/recovery outcomes. Reuse or extend existing result records; introduce named `ApplyStrategy`/`ApplyOutcome` types only where their consumers need that contract. Make module transition capabilities available through the ports from A01 rather than incidental methods on the engine service. Validate plugin-declared apply hints against host-supported boundaries; a schema annotation alone cannot authorize live replacement of resources.

Keep pure persistence/configuration services independent of runtime services. Specify which operations can be compensated and which completed physical effects cannot be reversed. Keep history writes idempotent by operation ID and expose partial persistence outcomes; do not promise a transaction spanning files, processes and hardware.

**Acceptance:** stale plans fail before mutation; failed preparation leaves accepted state unchanged or reports recovery required; a history failure does not falsely report that runtime application failed; replaying one operation ID does not add duplicate history or execute physical output again.

### A05 ResultsService has an implicit retention contract

**P2 — confirmed unbounded collection, not a measured memory failure.** [ResultsService](/Users/lincker/workspace/OmniBCI/omnibci/application/services/results_service.py:9) bounds recent trials at 200 but retains every epoch summary in a dictionary until generation changes. Pagination sorts the full retained key set. It depends on the whole engine object merely to read generation.

**Adjustment:** give the read model a callable that supplies generation, rather than the engine graph; no additional interface is needed for one value. Define retention in the query contract: recent bounded inspection and a complete session archive are different requirements. For recent inspection, report retained range, total observed count and truncation. If all session records must remain accessible, retain that promise through an append-only session store with a bounded memory index; do not silently evict records while still claiming a complete list.

This is a retention decision to settle before changing storage, not a reason to add a database immediately. Preserve generation filtering and duplicate suppression.

**S4a implementation (2026-11-20):** the whole-engine dependency is replaced by a generation callable. The retention decision and storage changes remain B1; S4a preserves the existing trial and epoch behavior.

**Acceptance:** long sessions have an explicit resource bound or durable storage policy; pagination describes retained data accurately; changing generations cannot show stale results; results queries cannot mutate internal collections.

## 4. Backend runtime modules

### R01 Acquisition needs one validated source-to-stream boundary

**P1 — confirmed missing ingress check.** [Acquisition.fetch](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/acquisition/acquisition.py:608) wraps source data in `SignalChunk` and explicitly leaves descriptor/matrix validation as a TODO. The chunk constructor only checks rank/minimum rows. A wrong row count may be noticed later during append/mapping, while an identity mapping can pass it through without checking the prepared layout.

**Adjustment:** validate basic rank, numeric dtype, event-row and nonempty-chunk rules before fan-out, and validate row count whenever a descriptor is available. Preserve preview without typed metadata; processing attachment requires the prepared descriptor and full layout check. Invalid output is a source contract failure with private host identity in diagnostics. Do not silently relabel samples or block the existing metadata-free preview path.

Keep Acquisition responsible for source lifecycle, normalization and fan-out. Processing owns whether it accepts an input. Keep [DataManager](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/ipc/data_manager.py:30)'s bounded nonblocking handoff and the existing behavior that a full processing queue faults that input while acquisition preview can continue. Make acceptance/backpressure results explicit in its port rather than relying on undocumented falsey return values.

**Acceptance:** a malformed chunk never reaches a decoder; processing backpressure cannot block source cleanup indefinitely; preview failure remains isolated; EOF, cancellation and read failure produce different terminal observations; no stale fetch task touches a replacement connection.

### R02 Processing mixes scheduling with its pure algorithm contract

**P1 — interface risk, with important existing guarantees.** [SignalProcessor](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/signal_processing/signal_processor.py:55) owns command handling, input attachment, epoch state, pipeline replacement, result publication and transport receipts. Its [input loop](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/signal_processing/signal_processor.py:188) correctly consumes start/barrier markers in order and joins a blocking queue read when cancellation occurs.

**Adjustment:** update the existing worker, processing states, `EpochProcessor` and `ProcessingPipeline` in place. The worker retains queues, command IDs, preparation receipts and runtime generations; existing epoch/pipeline objects retain window execution and filter/decoder calls. Do not add a `ProcessingSession` wrapper merely to rename this boundary. Extract a small helper only where the actual implementation removes duplicated logic.

Use D04–D06's direct array/baseline calls, explicit decoder reset and ordered boundary values. Keep pipeline preparation outside active epoch execution, and install a staged configuration only at the declared boundary. The application input-preparation use case owns the handshake; the domain does not start Acquisition or query saved profiles.

**Acceptance:** the same chunk sequence produces the same boundaries/results in a transport-free test; attach/start precedes samples; drain acknowledges all prior accepted samples; cancellation cannot orphan a consumed barrier; one pipeline revision is used for an entire epoch; unavailable source metadata leaves Processing waiting rather than fabricated-ready.

### R03 Stimulation has incomplete cleanup and a second inconsistent thread path

**P1 for cleanup and declared callbacks; P2 for removing the alternate thread path.** [Stimulator._setup](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/stimulation/stimulator.py:85) acquires trigger, paradigm and renderer resources before creating its loop, without a matching partial-setup cleanup structure. [RenderLoop cleanup](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/stimulation/render_loop.py:92) calls paradigm teardown and renderer shutdown sequentially; a teardown exception skips renderer shutdown.

The synchronous `Stimulator.start()` initializes on the caller's thread and starts rendering on another thread. A headless probe confirms setup and teardown use different threads. A second probe confirms [RenderLoop.stop](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/stimulation/render_loop.py:64) clears its thread reference after a timed join even when that thread is still alive. These are real defects in that path, but [the current application](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/stimulation/application.py:110) uses `start_async()` on its main event-loop thread; they are not evidence that every current run violates thread affinity.

**Adjustment:** standardize on the existing main-thread async rendering path unless a concrete platform requirement justifies another implementation. Put initialization, setup, frames and teardown under one owner, using the [API draft's preparation/cleanup order](plugin-api-v1.md#6-failures-and-resource-ownership). Keep cleanup in the existing Stimulation host; do not let the loop and supervisor each own teardown. Attempt remaining safe releases even after a cleanup failure, retaining the original error. Keep thread/task ownership until termination is confirmed, and report stop timeout as unresolved ownership.

Combine D07's declared frame/state contract with clear request acknowledgments. Keep trigger/frame timing explicit and preserve renderer-independent domain types. Do not call synchronous frame submission a hardware timing guarantee.

**Acceptance:** partial setup and teardown failure both release remaining owned resources; a blocked loop cannot report stopped; a second start cannot overlap an unresolved instance; every GPU operation runs on the designated thread; accepted requests and actual presentation observations remain distinguishable.

### R04 Actuation needs a declared thread and cancellation model

**S1e implemented (2026-11-20):** a single-worker executor owns all device calls. Repeated cancellation waits for settlement, shutdown preserves cleanup failures, and unclean/forced child exits block replacement. Existing mailbox, correlation and heartbeat behavior are retained. See the implementation plan for verification and target limitations.

**P1 — design risk for external drivers.** [Actuator](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/actuation/actuator.py:47) already cleans up partial setup. The runtime calls setup/teardown on its event-loop thread, while [execute](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/actuation/application.py:260) and [quiesce](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/actuation/application.py:364) use `asyncio.to_thread`. Calls are sequenced, but the interface does not promise one owning thread. A thread-affine native driver cannot infer a supported lifecycle from `Effector` alone.

**Adjustment:** choose one documented v1 model. A dedicated single-worker executor can own setup, execute, quiesce and teardown without blocking the event loop. Route all lifecycle operations through it. If a device requires an emergency operation concurrent with a blocking execute, specify that capability separately; a thread timeout cannot stop physical work.

Preserve the latest-pending-command mailbox, explicit superseded/stale outcomes, heartbeat independence, cancellation shielding and no-replay policy. Do not convert Actuation to an unbounded FIFO or a generic retrying task runner. Timeout leads to unresolved ownership/recovery, with physical stopping governed by the driver/device contract.

**Acceptance:** a thread-affine fake sees one owner across its lifecycle; no teardown overlaps execution; latest-command replacement reports the displaced command; cancellation waits for the underlying work or reports uncertainty; reconnect never resends a previous intent.

## 5. Plugin construction and marketplace interfaces

### K01 Public API, SDK and host responsibilities must stay separate

**P1 — required-input and metadata-consistency gaps.** [The SDK](/Users/lincker/workspace/OmniBCI/omnibci/sdk/__init__.py:35) defines a metadata-only `PluginContext`. [Runtime loading](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/runtime_configuration.py:25) can construct it from flat snapshot keys; [prepared processing](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/signal_processing/signal_configuration.py:9) supplies the actual layouts. The type permits absent processing input and inconsistent separate sample-rate/acquired/input values, although the normal processing factory currently derives them consistently. Context originally provided useful explicit injection and serializable metadata; it is not itself a compatibility layer or service locator. Its SDK location alone does not establish a layering defect.

**Adjustment:** apply the [public API, SDK and host boundaries](plugin-marketplace-design.md#4-public-api-sdk-and-module-hosts). The API defines behavior and types; the SDK publishes them and supplies development checks; the host validates, constructs, schedules, and drives cleanup. Retain one `omnibci.sdk` import path and ship it with the host initially. A second API facade, independently versioned SDK package, universal `Plugin.activate()` base, and SDK-owned loader are unnecessary.

Apply the agreed removal with direct keyword inputs: copied `parameters` and the package's `resource_root`; the three processing kinds additionally require mapped `input_signal`. Remove public `PluginContext`, `PluginParameters` and proposed processing contexts without aliases or adapters. This is justified by the small required input set, not by treating all contexts as undesirable. Preserve snapshot/configuration isolation. Module/revision/source/connection identity remains in private host plans and diagnostics. Runtime capabilities enter through role setup. All package sources use the same contract and receive no origin/trust metadata.

Internal host code imports canonical owners; all plugin implementations, including built-ins, import the public facade. Export every OmniBCI-defined type required by a public signature without publishing all domain entities or host adapters. In particular, expose the narrow rendering capability from D08 rather than the full renderer. Plugin artifacts must not vendor or shadow `omnibci`; the selected host environment supplies one canonical runtime copy.

The [loader](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/loader.py:220) already avoids constructor-error fallback; preserve that behavior while selecting the new required keywords by contribution kind. Update all built-ins, runtime factories, manifests, schemas, callers and tests together. API v1 is a complete replacement, not an additional route beside unchanged old consumers; a historical API 1 package must meet the new contract rather than merely share its number.

**Acceptance:** SDK exports are the canonical objects and cover all OmniBCI-defined public signature types; importing the SDK does not initialize services, discover plugins, or load optional devices/GPU libraries; plugins use the public path for OmniBCI imports; missing processing input fails preparation; sample rate cannot disagree with input metadata; artifacts shadowing host namespaces and unsupported API values are rejected. Conformance checks use the real module host and cover partial setup and awaited cleanup as well as construction.

### K02 Catalog, configuration metadata and installer validation should share one definition

**P2 — maintainability and ecosystem design.** [PluginCatalog](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/plugin_catalog.py:147) is already metadata-only and rejects ambiguous exact selections. [The installer](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/package_installer.py:347) also checks manifest/package fields. [PluginConfigurationCatalog](/Users/lincker/workspace/OmniBCI/omnibci/application/ports/plugin_configuration_catalog.py:24) exposes author configuration metadata through an application port. Package-domain rules and processing input-contract versions add another classification layer beyond contribution kind.

**Adjustment:** use one pure manifest validator producing a validated descriptor. Archive/path checks stay in the installer; filesystem scanning stays in catalog adapters; Python import stays in the loader. Share the manifest definition with packaging/publication tools. Runtime constructors never run to populate a catalog or configuration form.

Apply the same static rules to market downloads, local ZIP uploads and packaging selected source directories. Reject arbitrary repository archives or Python files without the required package contract instead of inferring entry points or running build hooks. Explicit development launches validate/package temporary snapshots and use the ordinary loader and owning module host with separate configuration. Replace unmanaged user-directory discovery in ordinary runtime resolution; preserve editable development through this explicit workflow rather than keeping two production paths.

Derive execution ownership from contribution kind and make package categories descriptive metadata. Require the existing decoder `channel_requirements` under API v1 and remove input-contract version dispatch; preserve type/count/required-name/default-name semantics without another input schema or fixed decoding-mode selector. Add only the [role-specific trial/result/command schema paths](plugin-marketplace-design.md#51-package-contents) required by the runtime methods. Distinguish structurally invalid contributions from valid contributions unavailable on a particular execution target.

**Acceptance:** a manifest accepted by packaging cannot later fail because catalog and installer interpret the same field differently; malformed schema references fail without imports; platform-specific unavailability has a reason; multiple physical candidates never silently resolve by search order.

### K03 Package and market ports stop at package version rather than execution artifact

**P2 now; required before expanding the marketplace.** [PluginSelection](/Users/lincker/workspace/OmniBCI/omnibci/application/plugin_identity.py:15) pins package/contribution/version. [PackageVersion](/Users/lincker/workspace/OmniBCI/omnibci/application/plugin_packages.py:41) has one digest, and [market operations](/Users/lincker/workspace/OmniBCI/omnibci/application/plugin_market.py:46) select a package version. These interfaces do not yet represent several OS/architecture/interpreter artifacts for one release or a resolved runtime environment.

**Adjustment:** keep the author/user selection small, but record trusted origin, exact version, complete target, digest and actual host environment identity in the private resolved binding. Extend existing descriptor/binding values; a separate `ResolvedPluginArtifact` class is only useful if implementation reveals a shared boundary that needs it. The execution node determines eligibility; a browser or management server must not substitute its own platform for an external node's platform. Reuse the fixed host dependency set without a selectable runtime-profile registry.

**S2c implementation (2026-11-20):** the [market port](../omnibci/application/plugin_market.py) returns verified downloads and rechecks proof; its repository/adapter no longer installs or holds inventory. The [market service](../omnibci/application/services/plugin_market_service.py) supplies expected metadata to the same installation service used by local uploads. That owner revalidates proof under its guard before common publication. See the [execution record](plugin-implementation-plan.md#s2c-execution-2026-11-20). Durable origin evidence is still absent from [receipts](../omnibci/infrastructure/plugins/package_store.py), and [catalog roots](../omnibci/infrastructure/plugins/plugin_catalog.py) still mix unmanaged directories with enabled packages. Those remain separate S2/S5 work; runtime instances stay with module hosts.

**S2b implementation (2026-11-20):** [PackageService](../omnibci/application/services/plugin_package_service.py) now receives saved/accepted reference values through a callback; Configuration owns version-copy use cases and their result. Its two-method [metadata port](../omnibci/application/configuration/ports.py) supports disabled-target preview without exposing package mutations. [Bootstrap](../omnibci/bootstrap.py) supplies the same store and shared guard; pending and unresolved runtime providers are retained. The [execution record](plugin-implementation-plan.md#s2b-execution-2026-11-20) records verification. Downloads and static staging can occur outside the final mutation guard; S2c retains proof revalidation under the common installation guard. A new generic source-provider registry or lease framework is unnecessary.

Extend existing package references to prepared/running artifact locks when expanding target distribution. Keep the signed static repository, local staging installer, exact pins and explicit enable/select/start separation. Add publisher authorization, revocation state and metadata revision proof at the distribution boundary as described in the parent proposal; reuse existing records and services instead of requiring a class per concept. These market additions do not block completing local API v1. Arbitrary dependency installation and a self-service publisher portal remain later capabilities.

**Acceptance:** two artifacts for different targets can coexist without ambiguous runtime resolution; same-archive retries preserve state and different bytes under one identity fail across sources. Local import and development need no market account or network. Development edits affect only a new snapshot/run and cannot change normal inventory. Reference checks prevent removal of live or pending artifacts; local/external preparation proves the selected artifact and environment; repository failure does not interrupt live modules. Neither clients nor manifests can assert a verified origin.

### K04 Catalog identity, scope and eligibility need explicit owners

**S2a implemented and verified (2026-11-20):** identity and kind definitions now belong to [an application leaf](../omnibci/application/plugin_identity.py), with standard validation errors and direct caller imports. Configuration models and descriptors retain no identity import facade. See the [execution record](plugin-implementation-plan.md#s2-first-batch-review-2026-11-20) for verification. S2b separately narrows the service boundary described in K03; catalog scope and eligibility remain pending.

**S2d implementation (2026-11-20):** [installation](../omnibci/infrastructure/plugins/package_installer.py), [inventory/enablement](../omnibci/infrastructure/plugins/package_lifecycle_store.py) and Bootstrap's [configuration adapter](../omnibci/infrastructure/plugins/configuration_catalog.py) now share the explicitly selected directory and host version. Metadata uses the installer's target platform; explicit stores no longer scan an unrelated default directory. See the [execution record](plugin-implementation-plan.md#s2d-execution-2026-11-20).

**P2 — remaining node eligibility risk.** Static compatibility for a supplied platform is not evidence that an external node has the artifact, dependencies or prepared hardware. Operation-specific node preparation/readiness remains S3/S4; no such result is inferred from inventory availability.

Current [node materialization](/Users/lincker/workspace/OmniBCI/omnibci/application/node_configuration.py:197) already validates locally owned contributions, and [runtime apply](/Users/lincker/workspace/OmniBCI/omnibci/application/services/runtime_apply_service.py:51) distinguishes external output roles. Preserve those checks; this finding does not claim that all remote execution uses the server's platform.

**Adjustment:** give the existing package/contribution identity and kind vocabulary one application plugin-contract owner. Configuration retains profile IDs, slots, revisions and parameter snapshots; no duplicate public identity type or shared-framework package is needed. Inject one node/store/environment scope at composition and reuse it in installation, inventory, enablement and resolution. Expose static definitions independently of enablement and runtime readiness, including definitions needed to inspect disabled packages. Report target eligibility for an explicit execution node; absence of a current node result means unknown. Actual input/device readiness remains with its runtime owner.

**Acceptance:** an explicitly configured store never scans an unrelated default root; metadata inspection performs no import; a disabled package's definition remains inspectable; a remote node's supported package is not rejected using the server platform; installation and OS compatibility alone never produce a ready status. Package contracts do not depend on configuration repository/profile implementations.

### K05 Preparation needs an artifact handoff and one mutation authority

**P1 — unresolved design boundary, not a missing-reference claim.** [Runtime loading](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/runtime_configuration.py:32) resolves the selected package again from a catalog. [ProfileBinding](/Users/lincker/workspace/OmniBCI/omnibci/application/configuration/models.py:192) does not pin artifact bytes or environment; external [PeerIdentity](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/ipc/peer_identity.py:8) correlates role/revisions/generation, not executable content. These identities protect their existing configuration boundaries but are insufficient as the proposed artifact preparation receipt.

The current [pending-operation snapshots](/Users/lincker/workspace/OmniBCI/omnibci/application/services/runtime_configuration_service.py:107), [runtime ownership snapshots](/Users/lincker/workspace/OmniBCI/omnibci/application/services/engine_service.py:668) and [shared guard](/Users/lincker/workspace/OmniBCI/omnibci/bootstrap.py:153) already protect pending, running and unresolved resources. The focused lifecycle/ownership tests pass. Installer and registry file locks protect their own writes; they do not combine the live reference sets of independent application controllers.

**Adjustment:** follow the [preparation handoff](plugin-marketplace-design.md#98-preparation-handoff-and-mutation-authority). Resolve and verify the final local artifact, then publish its pending reference under the existing admission guard. The owning host consumes that exact private record without global re-resolution. Reuse existing preparation/readiness outcomes to correlate the actual artifact/environment with the binding, node, operation and generation. Transfer pending ownership to active ownership without a gap, retaining old/candidate references until cleanup settles. Configuration/runtime owners expose narrow read-only reference queries; Plugin Management must not parse their repositories or infer resource release from a timeout.

Use one mutation authority per node/store; reject competing owners rather than adding distributed leases. CLI and presentation operations use the same owner. External nodes validate and resolve locally, return their actual prepared pins and enforce their own references. Node-local administration can precede remote management; do not simulate remote installation by writing into the controller's inventory. Physical-output uncertainty survives transport/process loss until the owning recovery path resolves it.

**Acceptance:** a reviewed plan is rechecked at submission; disable/remove cannot overtake pending-reference publication; a worker cannot select different bytes; a stale or mismatched node preparation result is rejected; a replacement keeps both owners accounted for; failed setup/stop retains its artifact until cleanup is confirmed. Existing pending/recovery tests remain passing. A second controller cannot mutate the same store outside the reference authority.

### K06 Imported code and constructed instances have different lifetimes

**S3a implementation status:** code caching, normal nested-package imports, concurrent loading and failure ownership are implemented and verified. The first paragraph below records the old behavior. Exact preparation-time pins remain K05/S3 work; see the [execution record](plugin-implementation-plan.md#s3a-execution-code-caching-and-nested-imports-2026-11-20).

**P2 — confirmed loader behavior and design risk.** The [module namespace](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/loader.py:29) includes contribution and location, without a content digest. [_load_source_module](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/loader.py:130) replaces the `sys.modules` entry and executes source on every construction. [Parent package initializers](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/loader.py:159) are deliberately skipped. Repeated construction can therefore recreate classes/module state while previously imported helper modules remain cached; normal Python package initialization cannot be assumed. This is a contract ambiguity, not evidence of an observed production failure.

**Adjustment:** keep the loader as a shared infrastructure adapter called inside the owning host. Use one private namespace and ordinary import caching per exact artifact within that process, with normal initialization of real nested packages. The synthetic root remains a non-executing container. Create fresh contribution instances for each binding, pass only the two public constructor signatures and retain independent instance state. Keep `sys.path` unchanged. Resolve imports from the pinned artifact; source edits require a new development snapshot/host. Instance teardown must not evict code still used by another instance. Do not add another loader per source, import-mode selector or dynamic unload framework. See the [code/instance ownership contract](plugin-marketplace-design.md#47-code-loading-and-instance-ownership).

**Acceptance:** two instances share the intended class identity and one module initialization but have separate parameters/state; contributions in one artifact share relative helper imports consistently; nested initialization executes once after admission; failed/concurrent imports do not leave partial or duplicate modules; a new development snapshot cannot reuse old code; catalog and installation still execute nothing.

### K07 Distribution requirements must not expand runtime or author contracts

**P2 — correction to the design draft.** The earlier proposal required a fresh market check for activation, separate offline-deployment approval and an author-provided release file inventory. These requirements duplicate existing boundaries. The current [runtime helper](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/runtime_configuration.py:32) already loads locally without market access; [market installation](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/market_repository.py:196) revalidates retained proof before publication, and the [installer](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/plugins/package_installer.py:194) already creates file-hash receipts. This finding removes unnecessary proposed work; it is not a current online-startup bug.

**Adjustment:** require fresh repository proof for a new market installation, and let an explicit repository refresh update locally known denial/revocation state. Preparing an accepted installed package uses local integrity, enablement, environment and denial only; expiry of an old index does not itself revoke the package. Record the last repository check without claiming knowledge of later offline revocations. Keep the installation-generated digest/file inventory; remove the mandatory author `release-files.json`. Marketplace supplies bytes/evidence, Plugin Management records policy and publishes packages, and the runtime coordinator owns any controlled stop. No extra approval token, public trust type or runtime market client is needed.

**Acceptance:** an eligible retained market package prepares with the repository offline; fresh installs still reject stale proof; a known denial blocks preparation regardless of import origin; no package-supplied receipt is trusted; revocation never deletes files or bypasses runtime cleanup. Archive and extracted-content integrity remain checked through the one installer/store mechanism.

## 6. Transport and presentation boundaries

### T01 Shared peer mechanics belong below the module orchestrators

**P1 for payload contracts; P2 for placement.** [The IPC envelope](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/ipc/contracts.py:70) validates framing, protocol version and event ID, then leaves the payload generic. Role-specific checks exist, for example [actuation validation](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/actuation/contracts.py:64). This layered validation is appropriate; generic envelope acceptance must not be mistaken for complete command validation.

Both output applications import [PeerChannel](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/neuroengine/gateway/peer_channel.py:1) from inside the NeuroEngine gateway subtree even though connection management and bounded outbound delivery are shared transport mechanisms.

**Adjustment:** retain wire protocol v1 and bounded framing. Connect A03's command definitions to role-specific codecs/validation before dispatch. Preserve generic plugin JSON only inside schema-validated payloads. Move shared peer connection, queue and delivery ownership into the IPC module without copying the implementation. Keep role-specific admission and failure policy with the owning module or gateway adapter.

Preserve connection-token binding, generation/revision handshake checks, bounded outbound queues, and explicit definitely-unsent versus possibly-executed outcomes. Local queue payloads may remain efficient Python objects; external channels receive validated JSON, never arbitrary pickle objects.

**Acceptance:** malformed envelopes and valid envelopes with invalid payloads are both rejected at the correct boundary; stale queued messages cannot cross a reconnection; outgoing saturation faults the affected channel without blocking the whole event bus; neither output module needs engine-assembly details.

### T02 Classification-to-actuation translation loses useful causality

**P2 — confirmed information loss, not proven cross-generation execution.** [TrialOutcome](/Users/lincker/workspace/OmniBCI/omnibci/application/events.py:10) contains generation, trial number and configuration revisions. [ActuationCommandTranslator](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/neuroengine/actuation_translator.py:35) emits a new sequence, wall-clock issue time, target, event ID and confidence, omitting the original trial identity/revisions and `forced` status. It also accepts a bare classification through duck typing. The existing channel identity already protects connection ownership; the missing fields chiefly weaken end-to-end traceability and explicit policy inputs.

**Adjustment:** require the canonical `TrialOutcome` input and produce an application-owned `ActuationIntent` carrying a unique command identity and origin reference. Keep its standard envelope separate from device parameters. Preserve origin in acknowledgments/faults and record any configured decision about forced/low-confidence results explicitly.

Since historical compatibility is excluded, remove “older sender may omit identity” behavior from the new v1 standard command envelope. A wall-clock age check across machines remains conditional on the deployment's clock assumptions; do not present it as a universally reliable deadline.

**Acceptance:** every physical-output result can be joined to its source trial and artifact/configuration revision; malformed standard identity fails before the effector; no coercion converts booleans/fractional targets into valid device commands; local and external nodes use the same intent semantics.

### T03 HTTP and WebSocket adapters need application-owned output contracts

**P2 — confirmed adapter coupling.** [The WebSocket adapter](/Users/lincker/workspace/OmniBCI/omnibci/presentation/websockets/results.py:18) imports a mapper from [infrastructure.outbound](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/outbound.py:31), which also defines peer payload serialization. HTTP routes have useful typed Pydantic responses, but [configuration error translation](/Users/lincker/workspace/OmniBCI/omnibci/presentation/routers/plugin_config.py:26) is a private helper in one router imported by other routers. It broadly translates `ValueError` into validation failure.

**Adjustment:** define canonical result/command values in application contracts. Give HTTP/WS and peer adapters their own codecs from those values, sharing pure field conversion only where meanings match. Expose only fields needed by each client operation; keep full runtime plans, internal paths and stack traces in host diagnostics. Preserve existing metadata and correlation/admission fields that clients actually use. Keep Pydantic, HTTP codes and WebSocket connections outside the domain. Presentation depending directly on an application service is acceptable; an interface for every service is unnecessary.

Move common error mapping to the existing presentation error boundary. Services convert expected domain validation failures into declared application exceptions; unexpected implementation exceptions must remain distinguishable from invalid user input. Keep `/api/v1`, asynchronous operation IDs, and accepted-versus-applied status explicit.

**Acceptance:** HTTP, WS and peer serialization preserve the intended identity fields without depending on one another's transport format; OpenAPI describes actual response outcomes; malformed input is distinct from internal failure; catalog/status endpoints never load plugins.

### T04 WebSocket fan-out has no bounded slow-client policy

**P2 — confirmed scheduling shape and scalability risk.** [ResultsBroadcaster._schedule](/Users/lincker/workspace/OmniBCI/omnibci/presentation/websockets/results.py:118) schedules a broadcast coroutine for every message without tracking completion. Each broadcast awaits clients sequentially. A slow client can hold a broadcast open while further broadcasts accumulate, and multiple broadcasts may attempt writes to the same connection concurrently. No load or resource-exhaustion claim is made from the source inspection alone.

**Adjustment:** use one writer and a bounded queue per client, with an explicit send deadline and shutdown cleanup. Do not block the engine listener on a browser. For result events, disconnect a client that cannot keep up or expose a documented gap/resync mechanism; do not silently claim lossless delivery after discarding results. Any resync promise must match A05's retained data range.

**Acceptance:** one stalled fake client cannot delay a healthy one; per-client order is stable; queued messages/tasks remain bounded; shutdown joins/cancels writers; disconnected clients release resources; no result replay is invented for data that has not been retained.

### Cross-module acceptance addendum (2026-11-20)

These requirements refine the existing findings and their implementation gates. They do not identify four new broken subsystems or require additional modules, SDK exports or compatibility layers.

**Configuration queries — S2/S4, K02/A02/A04/T03.** S2b removes PackageService and Configuration router dependency traversal. Setup/node/runtime consumers remain for S4. The repository is already an application-owned port; using it is not inherently an infrastructure dependency violation. The issue is reaching through another service to its broad dependencies without specifying the caller's actual boundary.

| Existing callers | Required boundary |
| --- | --- |
| [PluginPackageService](../omnibci/application/services/plugin_package_service.py) | S2b implemented: reads saved/accepted package references through a callback; Profile creation/copying belongs to Configuration. |
| [Plugin configuration router](../omnibci/presentation/routers/plugin_config.py) | S2b implemented: queries catalog metadata, Profiles, module/engine settings and history through declared service methods; no `svc.repository` or `svc.catalog` access. |
| [SignalSetupService](../omnibci/application/services/signal_setup_service.py) and [NodeConfigurationService](../omnibci/application/services/node_configuration_service.py) | Obtain the selected configuration/layout inputs or role-specific snapshot without access to unrelated configuration operations. |
| [RuntimeConfigurationService](../omnibci/application/services/runtime_configuration_service.py) and [RuntimeApplyCoordinator](../omnibci/application/services/runtime_apply_service.py) | Retain explicit planning, compare-and-swap, compensation and history authority; inventory both reads and writes before narrowing dependencies. |

Use a few methods on the existing service for presentation queries, or inject the required application-owned port explicitly where the caller owns that use case. Do not wrap every repository method or force runtime coordination through a read-only interface. Return only needed values; preserve copying, revisions and the existing shared admission guard. Acceptance must cover these callers, not only the package service.

**Channel semantics — S4 and B1/B2, A03/R01/R02/T04.** [Bounded signal-channel tests](../tests/neuroengine/test_bounded_signal_channel.py) already cover overflow, ordered boundaries and reattachment. Preserve these behaviors and document separate delivery contracts:

| Channel | Owner and acceptance |
| --- | --- |
| Signal chunks and input boundaries | Acquisition reports a failed Processing attachment; Processing owns input/epoch reset. A full queue must not acknowledge an undelivered boundary or stop unrelated acquisition/preview. Reattachment rejects old input state. |
| Commands and terminal results | Application owns scope/correlation/outcome; transport reports delivery. Distinguish definitely unsent, execution unknown and timeout; never automatically replay physical effects. |
| Preview | Acquisition owns capture/sequence identity. Presentation may miss preview frames; that must not block signal processing or imply a complete recording. |
| Diagnostics | The existing [log forwarder](../omnibci/infrastructure/acquisition/logging.py) drops records when its bounded queue is unavailable. Diagnostics must not block or fail acquisition. |
| Result queries and WebSocket events | Results owns the retained range; the broadcaster owns client delivery. B1/B2 must make disconnection/gaps and resynchronization consistent with retained data. |

Retain the existing queues, broker and broadcaster. A universal delivery abstraction would obscure these different requirements. B1/B2 remain separate follow-ups; this addendum does not change retention or delivery behavior.

**Shutdown ownership — S4/S6, A01/A04.** Shutdown admission already exists: [RequestGate](../omnibci/presentation/security.py) rejects new HTTP writes while retaining reads and drains accepted writes; [server lifespan](../omnibci/presentation/main.py) and [desktop control](../omnibci/presentation/server.py) then invoke [ServerShutdownService](../omnibci/application/services/shutdown_service.py). That service waits for owned configuration work and calls the existing Engine stop. [RuntimeAdmission](../omnibci/application/runtime_admission.py) reserves module stop against overlapping operations.

Preserve this responsibility chain when narrowing ports: Presentation controls request admission; Application settles owned operations; Engine and Module Hosts release their resources. Keep failure reporting and retry explicit. Restoring the request gate must not bypass unresolved resource cleanup or recovery checks. Verify accepted writes and pending apply operations finish in order, stop failure cannot report completion, and resume cannot start overlapping owners. Reuse [shutdown tests](../tests/application/test_shutdown_service.py) and [request-gate tests](../tests/presentation/test_desktop_api.py); add only missing cases when changing the affected boundary. No new lifecycle manager is needed. Stimulation implementation now proceeds on the current branch under the updated scope in the implementation plan.

**Boundary enforcement — S2–S6.** The eight existing [import contracts](../pyproject.toml) check layers and selected module dependencies. Passing them does not establish complete module ownership. Extend static rules only for concrete import directions established by a change, such as keeping static catalog operations independent of executable loading and configuration persistence independent of runtime services. Use behavioral tests for reference-query isolation, Profile ownership and preparation handoff; import rules alone cannot detect `service.repository` access. Avoid blanket bans on all infrastructure cross-imports or a parallel architectural framework.

## 7. Target interface ownership

The names below are contract sketches for the refactor. They do not require one new file/class per row and do not describe implemented APIs.

| Boundary | Owner and target shape | Keep opaque |
| --- | --- | --- |
| Source inspection | Domain `SourceObservation`: native text, optional layout, provenance and layout-failure explanation from one observation | Device-native text is display-only; checkpoint state is opaque to the host |
| Source transitions | Domain `SignalSource`: fixed trigger/resume properties, explicit async lifecycle and checkpoint methods; ordinary exceptions normalized by the host | Driver handles, connection state ownership and native-library internals |
| Chunk delivery | Domain signal value plus application stream identity/boundaries | Queue and shared-memory implementation |
| Decoder call | Direct filtered/latest-raw array arguments; required epoch reset under one model lifecycle | Model weights, host event/correlation metadata and working state |
| Boundary detection | Domain ordered `EpochBoundary` values | Detector implementation |
| Presentation | Domain paradigm frame/state contract and narrow `RenderSurface` | GPU objects and window implementation |
| Physical output | Domain effector execution/quiescence contract | Device command fields validated by its schema |
| Public plugin API | The 18 required domain roles/values/capabilities, re-exported identically by `omnibci.sdk` | Host context, transport records and internal modules not explicitly published |
| Construction | Direct parameters/resource-root keywords and required signal layout for processing; private host plan selects inputs | Module/revision/connection identity, global configuration and resource handles |
| SDK development tools | Reference, templates, static validation/packaging and temporary development runs using canonical contracts and existing hosts | Normal inventory mutation, production scheduling and runtime state |
| Runtime control | Narrowed existing application engine port/handle and the output lifecycle operations required by consumers | Worker objects, thread handles and peer graph |
| Configuration | Application typed snapshot, plan/strategy, outcome and input receipt | Plugin parameter JSON |
| Command transport | Application contract definitions; infrastructure codecs | Transport connection implementation |
| Artifact resolution | Execution-node application preparation pins an artifact/environment and publishes its pending reference; the host consumes that record | Filesystem layout, repository proofs and download mechanics |
| Plugin Management | Shared installation, origin/admission, enablement, scoped catalog and mutation protection; marketplace access supplies verified downloads | Runtime instances, configuration repository implementation and source-specific loader variants |
| Code loading | Shared infrastructure adapter inside each module host; cached artifact code and fresh binding instances | Python classes, module namespaces and instance registries |
| Client output | Application result values; presentation response/message models | Framework response/socket objects |

Use one authoritative owner for each fact. A signal rate belongs to prepared signal metadata; a package digest belongs to artifact resolution; a command's scope belongs to its command definition; physical completion belongs to the device contract. Avoid independently mutable copies of those facts in every layer.

## 8. Refactoring sequence

Use the [concrete implementation steps](plugin-implementation-plan.md#3-implementation-steps) as the single work order:

| Step | Change boundary | Gate |
| --- | --- | --- |
| S0 | Establish the actual baseline and caller inventory. | Existing failures and affected paths are understood. |
| S1 | Domain values, then Acquisition, Processing, Stimulation and Actuation contracts with their implementations/callers; complete the SDK/constructor switch. | All seven kinds execute under one coherent v1 contract; existing functional scenarios remain covered. |
| S2 | Plugin Management, Configuration, static validation and marketplace installation ownership. | One installer/definition, scoped metadata and preserved package/profile behavior. |
| S3 | Exact artifact preparation, code/Instance lifetimes and pending/active/recovery reference handoff. | No re-resolution, ownership gap or competing unguarded mutation. |
| S4 | Narrow runtime ports, boundary records, command contracts and external-node readiness. | Local/remote coordination, stale-result rejection, history and uncertain-stop behavior survive. |
| S5 | Shared validation/packaging and isolated development entry. | All three plugin sources work through the same API and hosts. |
| S6 | Affected clients, full regression/platform checks and implementation-accurate documentation. | Core completion has runtime evidence; only then write the new developer guide. |

Combine each domain contract with its actual consumers instead of scheduling a separate later execution-adapter rewrite. Narrow existing records/ports rather than creating a class for each sketch name. Do not treat every P2 finding as a prerequisite for the first domain patch. The plan separately tracks curated publication, multiple targets, remote package administration, A05 retention and T04 bounded client delivery; those deferred portions remain open. Preserve current package/client functionality throughout the coordinated replacement.

The proposal does not authorize implementation yet. No automatic migration, historical constructor adapter, new plugin API version, service locator, general dependency-injection framework, per-filter process, or replacement event bus is needed for this plan.

## 9. Verification and limits of this review

The 2026-11-19 plugin-boundary review checked all four design documents: **229 local links/anchors/line references**, 12 Python examples, one TOML example, the 18-name export closure, two matching constructors, seven role contracts and all 30 unique findings. K04–K07 extend the original review; public exports and constructors are unchanged. These are documentation and syntax checks, not execution of the proposed API.

The plugin-boundary review additionally passed **112 existing tests** covering mutation admission, pending/active/recovery references, execution-node configuration, shared installation composition and package enablement/integrity. These are checks of the current behavior to preserve, not execution of the proposed new contracts. No implementation or test source changed. The command was:

```sh
venv/bin/python -B -m pytest -q -p no:cacheprovider \
  tests/application/test_plugin_lifecycle_admission.py \
  tests/application/test_runtime_package_ownership.py \
  tests/application/test_node_configuration.py \
  tests/application/test_plugin_package_composition.py \
  tests/infrastructure/test_package_lifecycle_store.py
```

At the 2026-11-18 code review baseline, the selected regression set passed: **236 tests** covering domain/channel mapping, processing windows and boundaries, prepared decoder input, rendering lifecycle and pause, actuation validation/cleanup/settlement, results, command correlation and SDK exports. These results describe the current implementation, not the proposed v1. The command used was:

```sh
venv/bin/python -B -m pytest -q -p no:cacheprovider \
  tests/domain \
  tests/neuroengine/test_processing_pipeline.py \
  tests/neuroengine/test_epoch_boundaries.py \
  tests/infrastructure/test_decoder_input_runtime.py \
  tests/stimulation/test_render_loop.py \
  tests/stimulation/test_application_lifecycle.py \
  tests/stimulation/test_pause_stimulation.py \
  tests/actuation/test_contracts.py \
  tests/actuation/test_actuator.py \
  tests/actuation/test_settle_actuation.py \
  tests/application/test_results_service.py \
  tests/application/test_command_result_service.py \
  tests/infrastructure/test_plugin_sdk.py
```

`venv/bin/lint-imports --no-cache` passed all **8 contracts**, inspecting 188 files and 524 dependencies. This verifies the configured import rules; it does not prove that dynamically typed ports are complete or that the current rules reject every source-level coupling discussed above.

Small read-only probes additionally confirmed:

| Probe | Observed result | Finding |
| --- | --- | --- |
| Construct classifications with boolean/fractional target IDs | Accepted directly | D02 |
| Construct an epoch with NaN sample rate | Accepted directly | D02 |
| Construct a chunk with a string timestamp and negative sequence | Accepted directly | D01 |
| Pass a channel-dropping filter through the pipeline | Decoder received mismatched filtered/raw channel counts | D04 |
| Inspect required paradigm/effector methods | `render_frame` absent; `quiesce` not abstract | D07, D10 |
| Run the synchronous stimulator path with headless fakes | Setup and teardown used different threads | R03 |
| Stop a deliberately blocked render thread with a short timeout | `is_running` became false while the worker remained alive | R03 |

No source/test files were edited for these probes. They establish narrow interface behaviors, not physical timing or hardware correctness. Real display/GPU/trigger/device tests and cross-platform execution remain acceptance work for implementation. Passing the current tests is a baseline to preserve, not evidence that the proposed contract gaps are already covered.
