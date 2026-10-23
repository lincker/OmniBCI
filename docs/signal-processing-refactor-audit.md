# Signal processing refactor audit

Date: 2026-10-22 (Asia/Shanghai)

Branch: `refactor/signal-processing`

Reviewed commit: `d5bf7f55c02e7b01831918eff89cfc53a6be288f`

Status: analysis and proposals only. No production code or tests were changed. Deletion and implementation await the user's selection.

## Scope and conclusion

The directory `omnibci/infrastructure/signal_processing` contains **11 Python files, 1,276 lines, and 63 explicit functions/methods**, including one property getter. Generated dataclass methods are not counted. Every explicit function is covered in the inventory below. References were checked across production code, tests, scripts, examples and documentation; relevant Git history was also reviewed.

There is one strong obsolete-function candidate: `EpochProcessor.build_epoch()`. No complete file inside this directory is currently safe to classify as obsolete. The configuration transaction machinery, prepared-signal path, decoder gates, stream-drain handling and pre-trigger history are all active.

The current runtime flow is:

```text
NeuroEngineBuilder
  -> SignalProcessingWorker.run / _run_processor
  -> SignalProcessor.run / serve
     -> queued commands: prepare / stage / cancel / context / stop
     -> next_chunk: mapping / sequence tracking / stream barrier
     -> processing states: inspect every start/end marker
        -> PreTriggerBuffer: history before each trigger
        -> EpochProcessor: collect window / check readiness / force at boundary
           -> ProcessingPipeline: ordered filters -> Decoder
        -> complete_trial: Classification -> TrialOutcome -> event bus
```

`build_epoch()` is absent from this production flow. Commit `e9154ad` removed its call from `SignalProcessor.complete_trial()` when session recording and `EpochInfo` were removed. Existing tests still exercise it.

## Decisions for approval

### A1 — Remove the obsolete Epoch snapshot builder (recommended first batch)

Confidence: high for repository-local use. This is a deletion proposal, not an assertion about untracked external callers.

Scope in `epoch/epoch_processor.py`:

- Delete `EpochProcessor.build_epoch()` at lines 148–176.
- Delete its `Epoch` import at line 17.
- Delete `self.sample_rate = sample_rate` at line 45, whose only reader in this class is `build_epoch()`.
- Update the descriptions at lines 7, 33 and 81–84 that present constructing an `Epoch` value object as part of normal execution.
- **Keep** the constructor's `sample_rate` argument and its forwarding to `ProcessingPipeline`: filters still use this metadata.

Evidence:

- [Current result publication](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/signal_processing/signal_processor.py:122) directly constructs `TrialOutcome` from `Classification` and the active epoch revision.
- [The state test](/Users/lincker/workspace/OmniBCI/tests/neuroengine/test_processing_state.py:289) explicitly asserts that `build_epoch()` is not called during publication.
- Repository search found callers only in tests. The benchmark uses buffering methods and does not call `build_epoch()`.
- `git show e9154ad -- omnibci/infrastructure/signal_processing/signal_processor.py` shows the removed production call and its former session-recording purpose.

Test impact must be handled deliberately:

| Test location | Required treatment if A1 is approved |
| --- | --- |
| `test_epoch_processor.py:144,157,163` | Preserve start slicing, buffer growth and boundary truncation coverage; observe captured decoder windows or the buffer behavior without constructing an Epoch snapshot. |
| `test_epoch_processor.py:180–219` (`TestBuildEpoch`) | Remove snapshot-only contracts. Preserve borrowed-window stability coverage, already also exercised by the append regression tests. |
| `test_baseline_window.py:149,160` | Remove or rewrite assertions tied to concatenating a snapshot; preserve the actual pre-trigger/filter contract. |
| `test_channel_mapping.py:403–410` | Preserve mapped channel order, baseline/window and observed sample-rate checks through the capture filter/decoder and runtime metadata. Do not remove the mapping test wholesale. |
| `test_processing_state.py:35,73,289` | Remove the `_epoch()` fixture, the unused `build_epoch` mock, the obsolete negative assertion and the now-unused Epoch import. |

Validation: epoch buffer, baseline, channel-mapping, state, pipeline and stream-drain regressions must continue passing. Search for remaining `build_epoch` references after edits. Preserve all existing author headers.

### A2 — Optionally remove the now-isolated domain Epoch object (separate scope approval)

[`omnibci/domain/epoch.py`](/Users/lincker/workspace/OmniBCI/omnibci/domain/epoch.py) is outside the requested module. Its sole production importer is currently `epoch_processor.py` for A1. After A1, repository runtime code would no longer import or construct this object. It is not exported by the current SDK.

If no standalone snapshot/export API is intended, deleting this file and its dedicated value-object tests is reasonable. This decision also requires updating [the decoder guide](/Users/lincker/workspace/OmniBCI/docs/decoder-plugin-development.md:77), which still describes `Epoch.pre_trigger_data`, `has_pre_trigger_data` and the combined snapshot layout.

If A2 is not selected, retain the domain object and its valid standalone tests, including `TestEpochPreTriggerWindow` and `TestEpochConstructionSideEffects` in `test_baseline_window.py`. A1 does not imply A2.

Validation: repository-wide imports and references, domain tests, decoder-guide consistency, and the Python regression suite. Existing users importing the internal domain path cannot be ruled out by repository search alone.

### B1 — Remove the redundant completion boolean from decode results

This is a coordinated interface simplification, not dead-code deletion. `_run_pipeline()` returns only `(False, None)` or `(True, classification)`; the boolean conveys no additional information. Use `Classification | None` consistently instead.

Affected methods:

- `EpochProcessor.process_chunk()` at line 191.
- `EpochProcessor.force_decode_at_boundary()` at line 203.
- `EpochProcessor.decode_if_ready()` at line 207.
- `EpochProcessor._run_pipeline()` at line 213.

Update tuple unpacking in `processing_state.py:56–58,70`, `signal_processor.py:172–173`, and the corresponding test doubles/assertions. Keep the readiness gate, forced flag, `_pending_decode`, validation and publication order unchanged.

Validation: ordinary completion, declined predictions, forced boundaries, drain with newly buffered data, no repeated decoder feed at drain, and failure before/after publication. The standalone epoch-buffer benchmark should still run if its assertions consume these results.

### B2 — Require an explicit Worker snapshot and remove the global fallback

Confidence: high that the normal production caller already satisfies this contract; user confirmation is needed to retire direct construction without a snapshot.

The only repository production constructor, [NeuroEngineBuilder](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/neuroengine/builder.py:137), always supplies `settings_snapshot`. The builder itself [rejects a missing snapshot](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/neuroengine/builder.py:77).

Proposed scope:

- Require a non-None `settings_snapshot` at the Worker boundary; keep parent-process copy isolation.
- Delete `SignalProcessingWorker._snapshot()` at lines 55–59 and pass the explicit snapshot to `SignalProcessor`, whose constructor already deep-copies it.
- Simplify the corresponding `None` fallbacks in Worker startup and identity extraction.
- Update tests that construct a Worker without a snapshot to supply explicit test configuration, and add a missing-snapshot rejection case.

This also removes a reproduced inconsistency: `_run_processor()` obtains revision from `self.settings_snapshot`, whereas `_snapshot()` can obtain configuration from global settings. With no explicit snapshot, an isolated probe using the real Worker/Processor and mocked global snapshot produced:

```text
processor_revision: None
snapshot_revision: audit-revision
processor_generation: audit-generation
```

The normal builder path is unaffected by this inconsistency. If implicit construction must remain supported, resolve identity from the same effective snapshot instead of deleting the fallback.

Do not bundle removal of `settings.use_snapshot()` into B2. Process-local settings installation is a separate startup contract, tested by `test_config_snapshot.py:53`; plugin discovery still has a global-settings lookup in `plugin_catalog.py:88–90`.

Validation: snapshot copy isolation, explicit rejection of missing snapshots, Worker construction/readiness failures, real spawn startup, prepared pipeline creation and shutdown. Relevant suites include `test_config_snapshot.py`, `test_signal_processing_worker.py`, `test_worker_readiness.py` and `test_processing_configuration_spawn.py`.

### B3 — Make configuration imports one-way (optional, lower priority)

`signal_configuration.load_signal_pipeline()` imports `pipeline_configuration.load_filters()`, while `pipeline_configuration.build_pipeline_update()` imports `signal_configuration.signal_context()`. Function-local imports prevent immediate circular-import failure, but the dependency cycle remains.

Move `load_filters()` into `signal_configuration.py`, next to the other construction helpers, and update the two consumers and test imports. Keep both files and all functionality. No new abstraction or generic loader is needed. This is an optional responsibility cleanup, not evidence that either file is obsolete.

Validation: prepared pipeline construction, ordered and repeated filters, trial updates, unprepared updates, and package imports in a spawned process.

## Existing inconsistency requiring a separate policy decision

The application planner and Worker do not evaluate `x-apply` identically:

- [RuntimeConfigurationService._strategy](/Users/lincker/workspace/OmniBCI/omnibci/application/services/runtime_configuration_service.py:281) checks first-level properties only.
- [The Worker's _require_trial_changes](/Users/lincker/workspace/OmniBCI/omnibci/infrastructure/signal_processing/pipeline_configuration.py:53) recursively checks nested objects and inherits/overrides policy.

Reproduced using the actual imported functions with a small in-memory schema: `nested` permits `trial`, but `nested.gain` requires `restart`; changing `gain` yields:

```text
planner_strategy: trial_boundary
worker_validation: Changed plugin parameters require a module restart
```

This demonstrates a disagreement for supported schema shapes, not a claim that a currently configured built-in plugin triggers it. No fix was made. Confirm whether recursive inheritance and child overrides are the intended policy before consolidating the rule in an application-level helper usable by both layers. Do not delete the Worker's validation as though it were redundant. A future fix needs regression cases for nested override, inherited policy and unchanged siblings, followed by planner/worker agreement checks.

## Complete file and function inventory

`KEEP` means the current responsibility has concrete callers or a required framework/contract role. `A1` is an obsolete-code proposal. `B1/B2/B3` are optional coordinated changes. Tiny wrappers are not classified as dead merely because they have a single caller.

### 1. `__init__.py` — 4 lines, 0 functions

Keep the package marker and existing author header. Its lack of executable functions does not make it an obsolete module.

### 2. `configuration_transactions.py` — 145 lines, 8 functions/properties

| Function | Decision | Responsibility and caller evidence |
| --- | --- | --- |
| `configuration_receipt` :11 | KEEP | Shared status/generation/revision receipt used by `_finish`, `cancel` and `SignalProcessor._update_output_context`. |
| `ConfigurationTransactions.__init__` :19 | KEEP | Owns a pending candidate, terminal receipt cache and payload fingerprints; instantiated by `SignalProcessor.__init__` :87. |
| `ConfigurationTransactions.pending` :27 | KEEP | Read by preparation/context guards and pending installation at `signal_processor.py:303,327,366`. |
| `ConfigurationTransactions.stage` :30 | KEEP | Called by `_stage_configuration` :344; validates identity, rejects conflicting reuse and stages one candidate. |
| `ConfigurationTransactions.complete_applied` :88 | KEEP | Called at `signal_processor.py:371` only after the owner installs the candidate. Success must not be acknowledged at staging time. |
| `ConfigurationTransactions.cancel_pending` :96 | KEEP | Called by explicit cancellation and processor shutdown; emits and caches cancellation. |
| `ConfigurationTransactions.cancel` :105 | KEEP | Called at `signal_processor.py:384`; distinguishes applied versus cancelled and blocks delayed stage commands with cancellation tombstones. |
| `ConfigurationTransactions._finish` :136 | KEEP | Shared receipt creation/cache path for successful application and cancellation. |

These caches implement generation-wide idempotency. Arbitrary expiry or deletion would change behavior for delayed commands. Tests cover retries, differing payloads under one ID, JSON key order, malformed duplicates, timeout before/after install and cancellation before staging.

### 3. `epoch/__init__.py` — 3 lines, 0 functions

Keep the package marker. Translate its Chinese description to English when documentation cleanup is approved; describe both filters and decoder. Preserve any file header. Do not delete the directory merely because its initializer has no logic.

### 4. `epoch/epoch_processor.py` — 248 lines, 11 functions

| Function | Decision | Responsibility and caller evidence |
| --- | --- | --- |
| `EpochProcessor.__init__` :37 | KEEP; A1 field cleanup | Created by `SignalProcessor.new_epoch_processor` :115. Pipeline needs sample rate; the separate `self.sample_rate` field is only for A1. |
| `EpochProcessor.initialize_epoch` :67 | KEEP | Called by `processing_state.py:85`; records trigger identity, initial window, baseline and pending work. |
| `EpochProcessor.append_chunk` :107 | KEEP | Called by `process_chunk` :198; a SignalChunk-facing wrapper around `_append_samples`. The benchmark subclass overrides it. |
| `EpochProcessor.append_until_index` :111 | KEEP | Called by `processing_state.py:69`; excludes the closing marker sample. |
| `EpochProcessor._append_samples` :122 | KEEP | Shared growth implementation; preserves dtype promotion and previously exposed window prefixes. |
| `EpochProcessor.build_epoch` :148 | A1 | Old snapshot construction. No production caller remains after session removal. |
| `EpochProcessor.should_decode` :182 | KEEP | Used by `process_chunk` and `decode_if_ready`; gates decoding on the accumulated unfiltered window. |
| `EpochProcessor.process_chunk` :191 | KEEP; B1 | Called by `processing_state.py:56`; appends and attempts ordinary gated decoding. |
| `EpochProcessor.force_decode_at_boundary` :203 | KEEP; B1 | Called by `processing_state.py:70`; bypasses the gate and marks a forced classification. |
| `EpochProcessor.decode_if_ready` :207 | KEEP; B1 | Called by `signal_processor.py:172` at drain; avoids repeating the previous decoder input. |
| `EpochProcessor._run_pipeline` :213 | KEEP; B1 | Shared decoding, Prediction validation and Classification conversion with contextual errors. |

Do not remove `_epoch_storage`, dtype handling or `_pending_decode`: these implement tested behavior and performance characteristics. The `_run_pipeline` docstring at lines 214–217 should describe ordinary and drain calls as well as forced boundary calls.

### 5. `epoch/pipeline.py` — 63 lines, 2 functions

| Function | Decision | Responsibility and caller evidence |
| --- | --- | --- |
| `ProcessingPipeline.__init__` :15 | KEEP | Created at `epoch_processor.py:48`; stores ordered filters, decoder and filter-context sample rate. |
| `ProcessingPipeline.run` :25 | KEEP | Called at `epoch_processor.py:221`; constructs FilterContext, applies filters in order and supplies explicit latest samples to Decoder. |

The `latest_samples is None` rejection is part of the current decoder contract. The class is a small, independently tested composition boundary; inlining it into EpochProcessor solely to remove a file is not recommended.

### 6. `epoch/pretrigger_buffer.py` — 55 lines, 4 functions

| Function | Decision | Responsibility and caller evidence |
| --- | --- | --- |
| `PreTriggerBuffer.__init__` :25 | KEEP | Created at `signal_processor.py:95,309`; bounds retained history by capture duration and observed sample rate. |
| `PreTriggerBuffer.push` :29 | KEEP | Called at `processing_state.py:60,89`; retains history from idle and active chunks. |
| `PreTriggerBuffer.take_pre_trigger_data` :38 | KEEP | Called at `processing_state.py:79`; combines previous history and this chunk's prefix without duplicating samples. |
| `PreTriggerBuffer.clear` :54 | KEEP | Called at `signal_processor.py:177` after drain so later streams do not inherit old history. |

The `legacy` wording refers to the setting name/history, not to an unused capability. Capacity zero, empty input and copy isolation all remain meaningful. The class currently stores a bounded array using concatenation; its description as a "ring" is imprecise, not a reason to add a new ring-buffer implementation without performance evidence.

### 7. `pipeline_configuration.py` — 131 lines, 5 functions

| Function | Decision | Responsibility and caller evidence |
| --- | --- | --- |
| `load_filters` :23 | KEEP; B3 relocation | Used by initial preparation and `build_pipeline_update`; preserves binding order and distinct instances per slot. |
| `_binding_content` :30 | KEEP | Reused by `validate_output_context` and `build_pipeline_update`; compares runtime-affecting fields including processing input. |
| `validate_output_context` :36 | KEEP | Called by `SignalProcessor._update_output_context` :329; permits output metadata/revision changes after drain without replacing the pipeline. |
| `_require_trial_changes` :53 | KEEP; policy decision | Called at :122; recursively validates changed parameters against declared trial policy. See the planner disagreement above. |
| `build_pipeline_update` :64 | KEEP | Called by `ConfigurationTransactions.stage` :67; validates fixed context, input, plugin/platform/schema policy and builds a complete candidate. |

`ConfiguredPipeline` at :17 has no explicit methods, but its decoder/filter/snapshot bundle is used by transaction staging and installation. `decoder=None` and the `signal_plan is None` branch at :124 are active: an unprepared worker may accept valid configuration while deferring plugin construction. Validation before that return is essential because plugin loading has not yet occurred.

### 8. `processing_state.py` — 112 lines, 7 functions

| Function | Decision | Responsibility and caller evidence |
| --- | --- | --- |
| `ProcessingState.__init__` :15 | KEEP | Inherited by the two states constructed in `SignalProcessor.__init__` :101–102. |
| `ProcessingState.run` :18 | KEEP | Defines the state contract and rejects direct base execution; concrete overrides are called polymorphically by `serve` :225. |
| `ProcessingState._boundaries` :21 | KEEP | Called by `_process_epoch_chunk` :53; enumerates all markers through the detector's first-marker interface and checks bounds. |
| `ProcessingState._process_chunk` :40 | KEEP | Shared wrapper called by both states; preserves duration/sequence/revision diagnostics around processing failures. |
| `ProcessingState._process_epoch_chunk` :52 | KEEP; B1 call-site update | Handles close/reopen ordering, multiple epochs in one chunk, pre-trigger data and pending-configuration boundaries. |
| `AwaitingEpochState.run` :100 | KEEP | Reads chunks while idle and tolerates the standby state with no detector. |
| `AssemblingEpochState.run` :109 | KEEP | Continues collection for the active epoch, including stream-barrier handling through `next_chunk`. |

The two concrete `run()` methods look similar, but encode different entry invariants. Collapsing the state hierarchy is a separate design choice with little benefit for the deletion pass. Preserve the boundary algorithm and telemetry; they are not obsolete scaffolding. Tests cover simultaneous end/start, multiple trials per chunk and publication failures.

### 9. `signal_configuration.py` — 29 lines, 2 functions

| Function | Decision | Responsibility and caller evidence |
| --- | --- | --- |
| `signal_context` :9 | KEEP | Called by `load_signal_pipeline` and `build_pipeline_update`; describes acquired signal separately from mapped decoder input. |
| `load_signal_pipeline` :23 | KEEP | Called by `SignalProcessor._prepare_signal` :305; constructs decoder, detector and ordered filters after source preparation. |

Initial preparation includes the detector; trial updates intentionally do not replace it. Do not merge these paths into an undifferentiated load operation. `PluginContext.channel_names` remains an SDK field describing acquired names, even when `input_signal` uses mapped/renamed channels.

### 10. `signal_processor.py` — 408 lines, 20 functions

| Function | Decision | Responsibility and caller evidence |
| --- | --- | --- |
| `SignalProcessor.__init__` :54 | KEEP | Worker construction plus direct test/integration construction; owns pipeline ingredients, state machine, transaction state, runtime identity and drain tracking. |
| `SignalProcessor.new_epoch_processor` :112 | KEEP | Called by `processing_state.py:84`; installs pending config before allocating a per-epoch decoder instance. |
| `SignalProcessor.complete_trial` :122 | KEEP | Called by ordinary, forced-boundary and drain completion paths; publishes before incrementing the trial count. |
| `SignalProcessor.next_chunk` :133 | KEEP | Called by both states; off-loop queue read, cancellation cleanup, prepared channel mapping, sequence tracking and barrier dispatch. |
| `SignalProcessor._on_stream_barrier` :159 | KEEP; B1 call-site update | Called by `next_chunk`; verifies generation/revision/sequence, drains eligible decoding and reports completed/interrupted state. |
| `SignalProcessor._emit_drain_result` :192 | KEEP | Called by barrier completion/failure and cancellation of a consumed barrier; emits the StopAndDrain contract. |
| `SignalProcessor.run` :200 | KEEP | Worker calls this synchronous adapter into the owned asyncio loop. |
| `SignalProcessor.transition_to_awaiting` :203 | KEEP | Called at epoch completion and drain; clears the active epoch and installs pending configuration. |
| `SignalProcessor.transition_to_assembling` :210 | KEEP | Called when an idle state opens an epoch; activates the collection state. |
| `SignalProcessor.serve` :214 | KEEP | Main queue/state loop; finally cancels configuration that cannot reach a boundary. |
| `SignalProcessor._execute` :229 | KEEP | Routes all five live commands. Naked stop commands remain the engine's actual shutdown protocol. |
| `SignalProcessor._prepare_signal` :281 | KEEP | Validates source/connection/plan/runtime identity, prepares plugins and rebuilds history capacity. |
| `SignalProcessor._update_output_context` :320 | KEEP | Validates drained output-only changes and installs revision/snapshot without replacing the processing objects. |
| `SignalProcessor._publish_configuration_result` :340 | KEEP | Reused by prepare, context, stage, install and cancellation paths. |
| `SignalProcessor._stage_configuration` :343 | KEEP | Delegates transaction validation; installs immediately only if no epoch is active. |
| `SignalProcessor._install_configuration` :354 | KEEP | Shared snapshot/revision/plan/receipt update, with optional replacement pipeline. |
| `SignalProcessor._install_pending_configuration` :365 | KEEP | Used after staging while idle, at return-to-idle, and before a new epoch within the same chunk. The calls cover different boundaries. |
| `SignalProcessor._cancel_pending_configuration` :376 | KEEP | Shared cancellation/result publication for explicit stop and exceptional loop exit. |
| `SignalProcessor._cancel_configuration` :383 | KEEP | Publishes terminal target receipt and cancellation acknowledgement in order. |
| `SignalProcessor._emit_command_result` :389 | KEEP for this pass | Used by `_execute` for enveloped stop/unknown commands. Normal engine shutdown sends a bare stop, but deleting this helper would narrow the command protocol and remove unknown-request failure replies; lack of a happy-path engine caller alone is insufficient evidence. |

All five enum members are live: `prepare_signal`, `update_context`, `stage_configuration`, and `cancel_configuration` are in `engine.py:452–455`; `should_stop` is sent at `engine.py:323`. The comment at :233 calling bare commands "legacy" should be corrected rather than used to justify deletion.

Do not remove the second pending-install location as duplicate: close-and-reopen within one chunk can skip an intermediate `transition_to_awaiting()`. Do not remove the `serve()` finally cleanup in favor of the explicit stop path: cancellation and exceptions also need it. Do not remove `_drain_failure`: a decoder/publisher may have mutated state before failing, and a repeated barrier must not silently turn that failure into success.

### 11. `worker.py` — 78 lines, 4 functions

| Function | Decision | Responsibility and caller evidence |
| --- | --- | --- |
| `SignalProcessingWorker.__init__` :25 | KEEP; B2 contract | Called by builder :137; initializes mp.Process and copies parent configuration. |
| `SignalProcessingWorker.run` :40 | KEEP; B2 branch cleanup | Multiprocessing entry point; installs process settings, reports startup/run failures and closes reporter resources. |
| `SignalProcessingWorker._snapshot` :55 | B2 | Explicit snapshot copy or global fallback. Remove only with the explicit-snapshot contract described above. |
| `SignalProcessingWorker._run_processor` :61 | KEEP; B2 identity cleanup | Constructs standby processor, reports ready, marks run stage and enters the loop. |

The Worker intentionally reports readiness before a source-specific pipeline is prepared. Readiness is process readiness, not proof of a connected/validated signal. `BaseException` handling and reporter close are tested process lifecycle responsibilities.

## Documentation cleanup, without changing behavior

- Rewrite `epoch/__init__.py:3` in English; preserve headers.
- Correct `signal_processor.py:233`: the bare stop enum remains current protocol.
- Update the Epoch snapshot descriptions only as part of A1/A2; retain the distinction between pre-trigger reference data and decoder window.
- Correct `_run_pipeline`'s incomplete forced-only description at `epoch_processor.py:214`.
- The future concurrent-epoch discussion at `signal_processor.py:50–51` is aspirational. It can be shortened, but `for_new_epoch()` is needed today for stateful decoder isolation.
- Limit claims in `docs/configuration-ownership.md:619`: current prepared processing gets channel names from observed `plan.source.channels`; the generic plugin fallback elsewhere still supports flat settings, so this does not justify removing that setting.

## Verification performed

The existing virtual environment uses Python 3.14.2 and pytest 9.0.3. The following baseline completed with exit code 0: **320 test cases passed**, with no skipped/failed cases in this selected run.

```text
venv/bin/python -m pytest \
  tests/neuroengine/test_processing_state.py \
  tests/neuroengine/test_signal_processing_worker.py \
  tests/neuroengine/test_processing_configuration_spawn.py \
  tests/neuroengine/test_processing_pipeline.py \
  tests/neuroengine/test_processing_output_context.py \
  tests/neuroengine/test_processing_configuration_boundary.py \
  tests/neuroengine/test_epoch_boundaries.py \
  tests/neuroengine/test_epoch_processor.py \
  tests/neuroengine/test_baseline_window.py \
  tests/neuroengine/test_pipeline_configuration.py \
  tests/neuroengine/test_channel_mapping.py \
  tests/neuroengine/test_neuroengine_processor.py \
  tests/neuroengine/test_worker_readiness.py \
  tests/infrastructure/test_stream_drain.py \
  tests/application/test_processing_source.py \
  tests/application/test_processing_mapping_contract.py \
  tests/infrastructure/test_decoder_input_runtime.py -q
```

Two additional isolated probes reproduced the Worker fallback revision mismatch and nested application-policy disagreement described above. They used actual imported functions, in-memory inputs and mocks, without starting hardware or changing repository code. These probes are evidence for follow-up regression tests, not new committed tests.

This was a targeted baseline on the available macOS host, including existing spawn tests. It does not claim full-suite, Windows/Linux, live hardware or external-plugin coverage. A repository reference search cannot prove that downstream code never imports an internal API.

## Suggested execution order after approval

1. A1 and its necessary test/documentation cleanup; independently confirm A2.
2. B1 as a separate small interface refactor, preserving all behavioral tests.
3. B2 after deciding the direct-Worker construction contract.
4. B3 only if desired; address the nested-policy disagreement as a separately agreed correctness change.

The first batch should not delete configuration transactions, prepared-signal validation, stream barriers, pre-trigger buffers, decoder isolation, package markers or state classes.
