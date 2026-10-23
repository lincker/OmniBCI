# Signal processing construction simplification

Date: 2026-10-23 (Asia/Shanghai)

Branch: `refactor/signal-processing`

Baseline: `d5bf7f55c02e7b01831918eff89cfc53a6be288f`

Status: proposal for user review; no implementation changes have been made.

## Confirmed scope

The user clarified that epoch segmentation, filters and decoding remain. Simplify pipeline construction, processing configuration transactions and trial-boundary hot updates. Channel mapping remains the central input contract.

The earlier audit classified transaction functions as live under the existing feature set. This proposal deliberately retires that feature; it does not reclassify reachable code as unused. The previous A1/A2 snapshot removals, B1 decode-result change and B2 Worker snapshot contract change are separate proposals and are not included automatically.

## Recommended behavior

Use one construction path after source metadata and channel mapping are known. Keep processing configuration fixed within a runtime; applying a processing configuration change uses the existing complete engine rebuild. Construct the pipeline for a prepared source connection/plan, and reuse it when the preparation receipt is unchanged. A new connection still requires preparation after the previous stream has drained.

| User action | Proposed behavior |
| --- | --- |
| Save a draft/profile | Persist the draft/profile; do not modify the running pipeline. |
| Apply selected configuration while the engine is stopped | Keep `select_on_start`; use the selection at the next start. |
| Apply a decoder/filter selection or parameter change while running | Use the existing `rebuild` operation. |
| Apply a filter insertion, removal or order change | Use `rebuild`; order is still meaningful. |
| Apply detector or processing-input mapping changes | Use `rebuild`, as required by the fixed input contract. |
| Add a decoder to an already running source-only standby | Rebuild, then prepare; remove the same-Worker activation path. |
| Repeat preparation for the same receipt | Reuse decoder, detector, filters and history rather than reload the model. |
| Reconnect the source | Drain old data, validate the new connection and prepare before starting the stream. Changed source facts still require explicit review under the existing checks. |
| Apply output-only stimulation/actuation changes | Keep the existing module apply path and its lightweight revision synchronization. |
| Apply no effective change | Keep `no_change`, including the existing revision-consistency fallback. |

This proposal does not introduce a processing-only process restart, new pipeline builder, new transaction abstraction or replacement bundle class.

## Target execution path

```text
SignalSetupService.prepare()
  -> read actual acquisition descriptor
  -> compile_processing_plan(descriptor, accepted ProcessingInput)
  -> PrepareSignalProcessing(plan, receipt)
     -> SignalProcessor._prepare_signal()
        -> validate the received plan and its current runtime/connection binding
        -> signal_context(snapshot, plan)
        -> load_signal_pipeline(snapshot, plan)
           -> load selected decoder
           -> load selected epoch detector
           -> load selected filters in binding order
        -> install the prepared objects and pre-trigger buffer
        -> acknowledge the preparation receipt

StartStream(receipt)
  -> Acquisition validates this receipt for the connected source
  -> SignalProcessor.next_chunk()
     -> ResolvedSignalPlan.apply(chunk), when row mapping is required
     -> existing epoch state machine -> filters -> decoder -> TrialOutcome
```

Loading occurs after source preparation, not at Worker startup: actual sample rate and channel layout may only be available after connecting a device or opening a file. Initial construction remains local-variable construction followed by installation after success; a failed plugin constructor must not install a partial pipeline.

Keep decoder profile ownership of `ProcessingInput` and the existing mapping editor/API. There is no reason to migrate profile storage or introduce a separate mapping configuration subsystem for this scope.

## Production deletion and consolidation list

### Signal-processing module

| Current file or symbol | Proposed change |
| --- | --- |
| `configuration_transactions.py` | Delete the file after inlining the remaining output-context receipt dictionary. |
| `ConfigurationTransactions` | Delete pending candidates, request fingerprints, replayed terminal receipts, stage/cancel/tombstone logic and all methods. These exist to manage the retired hot-update operation. |
| `configuration_receipt()` | Inline its four-field dictionary at the remaining output-context caller; preserve the external receipt shape. |
| `pipeline_configuration.py` | Delete the file after relocating its still-needed initial construction/output-validation helpers. |
| `ConfiguredPipeline` | Delete; no replacement candidate bundle is needed. |
| `build_pipeline_update()` | Delete; new processing configuration is loaded by initial preparation in the rebuilt runtime. |
| `_require_trial_changes()` | Delete; processing updates no longer choose a boundary from `x-apply`. |
| `load_filters()` | Move into `signal_configuration.py` and keep its ordered per-slot instance construction. It is initial construction functionality, not a hot-update feature. |
| `_binding_content()` / `validate_output_context()` | Move into the existing `signal_configuration.py`; keep the checks that an output-only update does not modify source or processing inputs/algorithms. |
| `SignalProcessor._configuration_transactions` | Delete the field and import. |
| `_stage_configuration()` / `_cancel_configuration()` | Delete. |
| `_install_pending_configuration()` / `_cancel_pending_configuration()` | Delete. |
| `_install_configuration(snapshot, pipeline=None)` | Remove the generic installer. Move its remaining snapshot/revision/plan/receipt metadata updates into `_update_output_context()`, its sole remaining production caller. Do not lose those updates. |
| `SignalProcessorCommand.stage_configuration` / `.cancel_configuration` | Delete, including their dispatch branches. |
| `new_epoch_processor()` / `transition_to_awaiting()` | Remove only their pending-install calls. Keep decoder isolation and epoch lifecycle behavior. |
| `serve()` / stop handling | Remove pending-cancellation cleanup and the now-empty cleanup wrapper. Keep the loop and stop-before-next-data-read behavior. |
| `_prepare_signal()` / `_update_output_context()` | Remove pending guards; retain the active-epoch guard and all applicable identity/input checks. |

The module changes from 11 to 9 Python files. The target files are:

```text
signal_processing/
  __init__.py
  worker.py
  signal_processor.py
  signal_configuration.py
  processing_state.py
  epoch/
    __init__.py
    epoch_processor.py
    pipeline.py
    pretrigger_buffer.py
```

`signal_configuration.py` owns prepared plugin construction and the small output-context validation. It has no staged candidate, pending state, cancellation cache, or dependency cycle with another configuration file. The three existing initial construction helpers are sufficient; no factory class is needed.

### Application, IPC and UI

| Location | Proposed change |
| --- | --- |
| `RuntimeConfigurationService._strategy()` | Simplify routing: unchanged -> `no_change`; host settings changed -> `rebuild`; output-only slots -> `module`; other changes -> `rebuild`. Remove unused old/new arguments after updating callers. |
| `RuntimeConfigurationService.plan()` | Remove the trial-only processing-input fallback. Preserve filter-order detection, no-change/revision reconciliation, external-node checks and stopped-engine `select_on_start`. |
| `RuntimeApplyCoordinator.supported_strategies` / `execute()` | Remove `trial_boundary` support/dispatch. |
| `RuntimeApplyCoordinator._stage_pipeline()` | Delete stage, timeout cancellation and trial-specific reconciliation. |
| `RuntimeApplyCoordinator._apply_pipeline()` | Delete trial-boundary apply/save/rollback. Reuse `_rebuild()` and its existing failure handling instead. |
| `RuntimeApplyCoordinator._pipeline_receipt()` | Its validation still serves `_output_context()`. Inline that check into `_output_context()` if removing this single-caller helper; preserve generation/revision/status validation. |
| `shared/events.py` | Remove `StageProcessingConfiguration=211` and `CancelProcessingConfiguration=212`; leave all remaining event numbers unchanged. |
| `NeuroEngine._command_routes()` | Remove the stage/cancel routes. Preserve prepare, output-context and shutdown routes. |
| `RuntimePlanResponse.strategy` | Remove `trial_boundary` from the public response enum. |
| OpenAPI / frontend generated types | Regenerate the schema and TypeScript definitions from source. |
| `RuntimeConfiguration.tsx` / runtime locale entries | Remove the trial strategy label; processing Apply uses the existing rebuild description. Remove obsolete waiting/rollback-at-trial test fixtures. |

Keep the current HTTP plan/apply/operation endpoints. There is no separate stage/cancel HTTP endpoint to replace. Keep application operation IDs, admission, runtime-transition serialization, checkpoint handling and recovery reporting: these still protect rebuild and output-module operations.

## Mapping behavior and checks that remain

Keep `ChannelBinding`, `ProcessingInput`, `ResolvedSignalPlan`, `compile_processing_plan()` and `ResolvedSignalPlan.apply()` in the domain layer. Mapping continues to select/order/name source rows and declare compatible types without changing sample values, sample rate, sequence IDs or timestamps. Preserve the trigger row at the end.

Required checks remain:

- Known source channel IDs, unique source selections/output names, contiguous output indices and exclusion of trigger/stim/ground channels from mapped signal channels.
- Compatibility between declared and source-reported channel types.
- Validation of a serialized plan against its source descriptor and accepted processing input.
- generation, config revision, connection ID, source fingerprint and plan fingerprint in the preparation receipt, plus the plan's own revision.
- Same-receipt idempotence and rejection of replacement while an epoch is active.
- Actual observed sample rate and distinct acquired/mapped channel descriptions in `PluginContext`.
- Exact plugin selection, configuration version, parameter schema, input-contract declaration and decoder channel requirements at loading.

The long preparation receipt conditional may be rewritten as a comparison with an expected dictionary, retaining the nonempty connection and plan-revision checks. This is a local simplification of existing checks, not a new validator abstraction or a change to the protocol.

The preview and Worker checks serve different boundaries. Keep draft validation for immediate user feedback and keep final loader validation for the actual selected plugin. Removing a two-line preparation precheck is not worth changing error timing/type in this pass.

## Output-context synchronization remains necessary

Output-module apply currently drains, replaces the relevant output modules, updates Processing revision, updates Acquisition revision, updates the parent engine snapshot, persists and resumes. The rollback path also synchronizes these identities.

`UpdateProcessingContext` does not replace decoder, detector or filters. Its remaining scope is to validate output-only changes and update the snapshot, revision, signal plan and preparation receipt. Acquisition's StartStream gate consumes that receipt; dropping synchronization while retaining output-module apply would leave inconsistent process identities.

Removing this path too would require routing all output changes through full rebuild and retiring the `module` strategy. That is a broader lifecycle change and is excluded from the recommended proposal.

## Deliberately unchanged processing behavior

Keep marker handling, first-chunk decode timing, geometric buffer growth, pre-trigger capture, filter order, decoder readiness, forced decoding, latest-samples semantics, Prediction validation and result publication order. Keep `decoder.for_new_epoch()` for state isolation.

Keep stream barriers and drain receipts. Preparing a new plan requires the existing stop/drain/prepare/start order; `current_epoch is None` alone does not prove that the data queue is drained.

Keep same-generation trial numbering across ordinary stream pause/resume. A processing configuration rebuild creates a new generation and new processor, so its trial count naturally starts again. Do not reset counters or sequence tracking merely because prepare was retried.

## User-visible tradeoffs

Processing Apply no longer waits for a natural trial boundary without interruption. It uses the existing complete engine graph rebuild, including Acquisition and Processing workers, queues and gateway, with a new generation. Active output processes may also restart as part of that existing operation.

An unfinished epoch may be interrupted at drain. File playback restoration depends on checkpoints; live-source restoration depends on its declared resume capability. A source without confirmed safe resumption can remain paused and require recovery/manual action. Therefore this proposal does not promise gap-free live acquisition when changing decoder/filter parameters.

If the user prefers an even stricter UI policy later, processing Apply can be allowed only while stopped. That is not required to implement this proposal and is not selected by default.

## Schema and documentation policy

Remove all execution decisions based on `x-apply=trial` for processing. Retain the general schema field as accepted plugin metadata so unrelated acquisition/output schemas and third-party declarations do not become invalid. Update documentation to state that the host applies processing changes through rebuild even when a plugin suggests a narrower boundary. Built-in decoder/example annotations can be updated to `restart` to match host behavior; parameter layouts and saved profile ownership remain unchanged.

Update configuration ownership, channel mapping, manifest/schema and decoder-development documentation where they describe trial updates. Preserve author headers and use English for new or revised repository text. Treat the earlier audit as a historical baseline and link this explicitly approved scope change rather than silently rewriting its evidence.

## Implementation steps and acceptance criteria

| Step | Work | Verification |
| --- | --- | --- |
| 1 | Change processing plan strategy and public UI/schema expectations to rebuild. | Planner tests cover decoder/filter selection, parameters, order, mapping, stopped engine, no-change/revision and output-only module updates. |
| 2 | Consolidate initial construction/output validation; remove processing stage/cancel transactions, handlers and IPC routes in the same change. | Preparation, Worker spawn, mapping, output-context and command-routing tests pass. No retired symbols remain in production code or generated contracts. |
| 3 | Remove application trial apply/reconciliation and migrate its tests to the new behavior. | Real process integration verifies new Worker identities/generation, preparation before resumed stream, correct revision and existing rebuild failure recovery. |
| 4 | Finish docs and generated-contract updates. | OpenAPI generation, frontend codegen consistency, typecheck, frontend tests and import-linter pass. Run the backend regression suite, excluding tests requiring unavailable hardware/display as appropriate. |

The steps describe implementation order inside one coherent refactor; do not expose an intermediate version where the planner emits retired commands or the UI promises trial-boundary application.

Essential regression cases:

1. Channel selection/order/rename/type declaration and identity mapping preserve expected samples and trigger values.
2. Missing/duplicate/forbidden channels, stale runtime/connection receipts and mismatched plans are rejected.
3. Observed sample rate, mapped input metadata, ordered filters and decoder requirements reach actual constructors correctly.
4. Same-receipt retries preserve objects; reconnects re-prepare after drain; failed construction does not install partial objects.
5. Changing decoder/filter/mapping during a run produces a rebuild, not a pending trial operation; filter reordering is not treated as no-change.
6. A rebuilt runtime prepares the new source/mapping before restoring acquisition, with correct generation/revision and recovery behavior.
7. Output-only apply and rollback preserve processing objects and synchronize both workers' revisions; another prepare remains idempotent.
8. Existing epoch boundaries, baseline handling, decoder state isolation and drain behavior remain covered unchanged.

## Test retirement and migration

- Retire dedicated stage/cancel tests in `test_processing_configuration_boundary.py` and trial-update spawn behavior in `test_processing_configuration_spawn.py`; replace the relevant end-to-end assurance with rebuild integration cases.
- Move still-used fixture helpers before deleting a test file: `test_processing_output_context.py` imports helpers from the trial-configuration test modules.
- Split `test_pipeline_configuration.py`: retire candidate/hot-update policy tests, migrate remaining output-context/input-rejection cases to the retained helpers or preparation path.
- Preserve `test_processing_output_context.py` apart from its pending-transaction busy branch.
- Preserve marker tests in `test_epoch_boundaries.py`; retire only the case applying a pending update between epochs inside a chunk.
- Update same-Worker source-only-to-decoder activation tests to rebuild-based activation.
- In `test_runtime_narrow_apply.py`, retire only trial-specific cases/fixtures; preserve output-module recovery, platform availability, checkpoint and live-resume coverage.
- Update `test_runtime_apply_integration.py` to expect new workers/generation for decoder changes rather than same-worker trial apply.
- Preserve the tests of standalone Epoch value objects and the current decode-result protocol until A1/A2/B1 are separately approved.

## Review decision

Approve this proposal as one scope: retain mapping and the entire epoch/filter/decoder data path; retire processing trial hot updates; use existing full rebuild for processing Apply; retain lightweight output-context synchronization. No production implementation or test execution has been performed for this proposal yet.
