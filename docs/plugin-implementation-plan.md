# Plugin API v1 Implementation Plan

Status: implementation plan and execution record. S0, S1a–S1c and S1d's first batch are complete. Later S1d batches, S1e–S1f and subsequent steps remain pending.

Updated: 2026-11-20. Original inspection baseline: `0bdd1dd2a296fea4b919d7f1a186a6fb76477b8e` on `refactor/module-interface`. S1d's first batch starts from `e81f825`, which commits the design and S1a–S1c work.

This document owns implementation order, change boundaries and acceptance gates. The [architecture proposal](plugin-marketplace-design.md#3-concepts-and-architectural-boundaries) owns terminology and module responsibilities; [Plugin API v1](plugin-api-v1.md) owns Python contracts; the [backend review](backend-interface-review.md) owns findings; the [Stimulation design](stimulation-interface-design.md) owns detailed rendering/lifecycle requirements. Do not maintain a second set of public signatures here.

## 1. Scope and completion criteria

Complete the existing backend's transition to the single pre-launch Plugin API v1. Market, local archives and development snapshots use the same package definition, loader and module hosts. Preserve acquisition preview/resume, processing input mapping and epoch behavior, stimulation feedback/timing semantics, actuation settlement, configuration/history and package operations.

The core work is complete only when:

- All seven Contribution kinds execute through the 18 canonical SDK exports and two constructor signatures. Built-ins, actual callers, manifests, examples and tests agree. `PluginContext` and superseded contracts have no compatibility aliases or dispatch paths.
- Plugin Management owns Packages; Configuration owns profiles/bindings; Module Hosts own Instances. Static operations execute no plugin code.
- Preparation fixes the actual local artifact and environment, publishes its pending reference, and hands that record to the host. Pending, active and unresolved ownership remain protected through replacement and failure.
- Existing local and external-node execution works. Market download and local ZIP installation retain one installer. Development uses isolated snapshots and normal hosts.
- Affected clients and generated contracts remain functional. The headless, integration, architecture and installed-wheel checks pass on their applicable targets. Physical-device/display claims have the separate evidence described in [validation](validation.md).
- Current implementation guides describe the completed code. The new developer guide is written only after the implementation and its examples work.

Building a publisher service, marketplace portal or general plugin framework is not part of the core completion gate. Existing marketplace client functionality must still work. Section 5 gives the separate distribution and backend follow-up sequence; deferred findings remain open.

## 2. Simplifications adopted

| Decision | Concrete simplification | Reason and behavior retained |
| --- | --- | --- |
| Change a contract with its consumers | Combine each role's domain interface, built-in implementations, host calls and tests into one change unit. | A declaration-only change leaves the running system inconsistent. This removes a separate later adapter-repair pass. |
| Keep one API transition | Replace current constructors and API dispatch together; retain `manifest_version = 2` and unrelated wire/storage versions where their meaning is unchanged. | The system is unreleased. Parallel old/new implementations, aliases and migration dispatch add work without a supported consumer. |
| Reuse existing modules | Narrow existing services and ports. Move only misplaced responsibilities or definitions; do not first reorganize the entire directory tree. | `infrastructure/plugins` contains several owners. Changing directory names alone does not fix dependencies. |
| Model actual boundaries only | Reuse selections, bindings, descriptors, receipts and command outcomes. Add fields or a small private record only where ownership/validation requires it. | Package, Contribution and Instance are terminology, not a requirement for three new class hierarchies. Private dictionaries do not all need mechanical conversion. |
| Keep one static validation and installation path | Extract shared manifest rules once; keep the current schema validator, installer, inventory and integrity receipts. | Market and local installation already converge. A second installer, source-provider framework or author-maintained hash inventory duplicates working behavior. |
| Keep current runtime coordination | Retain the event bus, apply/recovery coordinator, module workers and pending/active/recovery protection. Use one mutation owner per node/store. | Narrow ports and explicit handoff address the boundary problems without a new runtime, distributed lease service or transaction across devices. |
| Separate code loading from instances | Use artifact-scoped Python import caching and fresh Instances; use normal stop/restart for changed code. | A hot-reload/unload framework is unnecessary and makes resource ownership harder. |
| Keep the fixed host environment | Verify installed dependencies and record the actual environment. Do not install arbitrary dependencies or introduce selectable plugin environments. | Separate environments require additional process/protocol work; the existing dependency policy already supports current workloads. |
| Limit first distribution support | Keep one selected artifact per package version on a node and existing supported platform constraints. Add multi-target release selection/storage when distribution needs it. | Exact local hashes and environment checks are needed now; a general multi-artifact repository/storage redesign is not. Unsupported target formats must fail explicitly. |
| Keep execution independent of the market | Verify repository proof at installation; retain local acceptance/integrity/denial checks for startup. | Starting an installed package needs neither a network activation check nor a separate offline-approval token. |
| Keep author tooling small | One command entry point for static validation, packaging and explicit development runs; reuse validated examples. | A project generator, watcher, debugger service and editable production source path are unnecessary for the three source workflows. |
| Bound frontend work | Update actual affected wire consumers and the Plugins terminology; leave layout/product redesign separate. | A backend refactor must preserve working clients, but it does not require rebuilding the frontend. |

These simplify implementation, not the ownership guarantees. Keep exact selection, required role methods, input/schema validation, immutable artifacts, reference protection and uncertain-stop handling.

## 3. Implementation steps

The order is **S0 → S1 → S2 → S3 → S4 → S5 → S6**. S1's internal rows are one coordinated API replacement, not separately supported API releases. Work can be split into focused commits on the refactor branch; do not add temporary compatibility code merely to make an incomplete intermediate state deployable. Mark a step complete only when its listed changes and checks are finished.

### S0 Establish the baseline and change inventory

**Change boundary:** planning and verification; no functional refactor yet.

1. Record the actual branch/commit and existing user changes. Do not reset the working tree or recreate the branch.
2. Inventory all seven kinds, built-in and example manifests, loader callers, public SDK imports, configuration codecs and external-node startup paths. Record current constructor and role-method consumers before removing a name.
3. Run the current headless and integration suites and import rules using the project environment. Record pre-existing failures separately; historical test counts in the review are not a fresh baseline.
4. Add focused failing cases for confirmed contract defects immediately before the relevant S1–S4 implementation. Use isolated files/configuration and fake devices; test observable behavior rather than internal class layouts.

**Gate:** the change map identifies every affected caller; existing failures are understood; tests do not read or modify real user configuration. Use the command groups in section 4.

**Execution record (2026-11-19):** S0 baseline and inventory are complete; see the [S0 report](plugin-s0-baseline.md). The controlled local runs passed 3425 headless tests, 36 integration tests and all 8 import contracts, with one Windows-only integration skip. Three first-run loopback HTTP failures passed after isolating the system proxy. At that baseline, source, tests and configuration were unchanged and S1 had not started.

### S1 Replace domain contracts and their execution callers

**Change boundary:** domain first, then each owning module and its built-ins. This includes the shared constructor/manifest change needed to make the seven roles runnable together.

| Internal order | Concrete changes and existing locations | Required acceptance |
| --- | --- | --- |
| S1a Domain values | The approved first batch updates [SignalChunk](../omnibci/domain/signal.py), [Epoch](../omnibci/domain/epoch.py), [Prediction](../omnibci/domain/ports/algorithm.py) and [Classification](../omnibci/domain/classification.py), plus necessary callers. Correct integer nanosecond timestamps, numeric metadata and real-array validation. Adjust [SignalInfo](../omnibci/domain/signal_info.py) and its host-only fields with S1b/S1c consumers; introduce source/boundary result types in those steps. | Invalid booleans/fractional target IDs, non-finite metadata and inconsistent layouts fail at the proper boundary. Preserve NaN signal samples, target zero, NumPy integer targets, signal ownership and epoch snapshots. Do not publish a partially updated SDK. |
| S1b Acquisition | Update [SignalSource](../omnibci/domain/ports/acquisition.py), [Acquisition](../omnibci/infrastructure/acquisition/acquisition.py), [worker](../omnibci/infrastructure/acquisition/worker.py), File Reader and device drivers together. Implement fixed trigger/resume properties, one lifecycle/error model and partial observations/checkpoints. | Preview without a full descriptor, partial observation diagnostics, file resume without replay, live-source restart, cancellation/EOF and independent Processing attachment work. |
| S1c Processing | Update [algorithm ports](../omnibci/domain/ports/algorithm.py), [signal configuration](../omnibci/infrastructure/signal_processing/signal_configuration.py), [pipeline configuration](../omnibci/infrastructure/signal_processing/pipeline_configuration.py), [pipeline](../omnibci/infrastructure/signal_processing/epoch/pipeline.py), [epoch processor](../omnibci/infrastructure/signal_processing/epoch/epoch_processor.py), detector, filters and CCA/FBCCA. Pass mapped input explicitly; enforce layout-preserving filters, direct array/`pre_trigger_data` calls, one decoder model lifecycle with epoch reset, and ordered boundaries. | Named mapping, multiple markers, raw/filtered window semantics, baseline, pre-trigger samples, forced/short attempts and target IDs remain correct. Shape-changing filters fail before decoding. No new ProcessingSession wrapper is needed. |
| S1d Stimulation | Update paradigm/renderer/trigger domain ports, [Stimulator](../omnibci/infrastructure/stimulation/stimulator.py), [RenderLoop](../omnibci/infrastructure/stimulation/render_loop.py), [application](../omnibci/infrastructure/stimulation/application.py), Classic40 and trigger drivers. Require frame/readiness methods, inject narrow drawing/trigger capabilities and use the existing async execution owner. Remove the alternate thread path after checking its callers. | The [Stimulation acceptance scenarios](stimulation-interface-design.md#7-concrete-refactoring-sequence-and-acceptance) pass, including partial setup, failed cleanup, blocked callbacks, no-trigger mode and feedback. Preserve both renderer adapters and the frame-pack format. |
| S1e Actuation | Update [Effector](../omnibci/domain/ports/effector.py), [Actuator](../omnibci/infrastructure/actuation/actuator.py), [application](../omnibci/infrastructure/actuation/application.py), validation and effectors. Replace `play` with `execute`, require quiescence and serialize device lifecycle/calls under one owner. | Latest-pending replacement, stale/unknown outcomes, logger execution, partial cleanup and bounded settlement work. Timeout/cancellation does not claim physical completion or permit overlapping replacement. |
| S1f Complete the shared API switch | Update [SDK](../omnibci/sdk/__init__.py), [loader](../omnibci/infrastructure/plugins/loader.py), [runtime configuration](../omnibci/infrastructure/plugins/runtime_configuration.py), all constructors/factories, manifests, fixtures and executable examples. Publish exactly the draft's 18 names and two constructor signatures; accept only API `"1"`. Extract shared static manifest validation for the changed declarations instead of copying rules into catalog and installer. | All seven kinds run through the actual loader and owning host. No PluginContext/FilterContext constructor path, old API dispatcher, constructor retry or internal-import alias remains. `manifest_version = 2` stays independent of Plugin API v1. |

**S1a first batch complete (2026-11-19):** The four existing domain files now enforce the approved constructor invariants. Existing integer validation is reused; Prediction and Classification share two private normalization functions. SignalChunk retains its original array storage, and Epoch retains its copied read-only snapshot. Classification requires a positive event ID, a boolean forced flag and an aware datetime; its default timestamp remains UTC. No compatibility layer, new domain type or dependency was added. SignalInfo, SDK exports and plugin manifests are unchanged.

[EpochProcessor](../omnibci/infrastructure/signal_processing/epoch/epoch_processor.py) retains return-type validation and adds decoder/event/revision context to pipeline exceptions through traceback notes without replacing the original exception. Existing result-conversion error handling remains. Timestamp fixtures and the benchmark use integer nanoseconds; malformed Prediction fixtures now construct inside the test call. The preview test still checks malformed input independently of valid domain construction.

The new [signal-value](../tests/domain/test_signal_values.py) and [prediction-value](../tests/domain/test_prediction_values.py) tests, together with the extended error-context cases, were run before implementation: **125 failed, 38 passed**. After implementation and caller updates, the focused group passed **520 tests**; the full headless group passed **3586 tests** with 37 deselected; integration passed **36 tests** with one Windows Job Objects skip and 3586 deselected; all **8 import contracts** passed. Integration retained the two existing WebSocket deprecation warnings from S0. The isolated configuration and localhost proxy bypass match the S0 baseline; real GUI/hardware and other OS targets were not exercised. Commands, logs and JUnit reports are under the local temporary directory `/private/tmp/omnibci-s1a-i2azfbxk`. This completes the approved first batch of S1a; the coordinated S1 API replacement is not complete.

**S1b complete (2026-11-19):** [SignalInfo](../omnibci/domain/signal_info.py) stores only sample rate and channels; counts and the final event-row index are derived. Channel IDs are required instead of silently generated by the value constructor. [SignalSource](../omnibci/domain/ports/acquisition.py) now declares fixed trigger/resume modes, `None`-returning lifecycle operations, one `observe()` operation, and typed `SourceObservation` / `SourceCheckpoint` results. Partial observations retain native text and a bounded layout diagnostic. Checkpoint state is copied, bounded JSON with required resource identity and revision. The old plugin state properties, paired metadata methods and capability booleans are removed without aliases or fallback dispatch.

File Reader uses `events` / `checkpoint`; Curry8 and Scan45 use `level` / `live-current`. File restoration validates the file revision and next unread sample while connected and stopped. Curry8 can obtain its protocol metadata when starting without a prior observation. Scan45 reports its configured layout as `user-declared` and retains the explicit unsupported-device-query message. Constructors still use the existing parameters/context signature until S1f.

[Acquisition](../omnibci/infrastructure/acquisition/acquisition.py) owns connection/stream state and validates modes before connecting. It joins the reader before stop, observation, checkpoint or disconnect; EOF teardown and cancelled reads cannot overlap a replacement start. Unsettled reads and failed cleanup block reuse until cleanup succeeds. Returned matrices are checked before preprocessing or delivery for nonempty samples, observed row count and finite nonnegative integer event values. Existing trigger normalization, preview without a descriptor, no-replay checkpointing, stream boundaries and independent Processing attachment remain. Refreshing an observation invalidates an attached preparation only when its descriptor changes or observation fails. Terminal transport errors invalidate host identity; metadata timeouts retain the connection.

The new internal [source metadata functions](../omnibci/application/source_metadata.py) project observations into the existing host/HTTP records and validate those records when reading them. They do not add another public layout type or accept the old plugin interface. Acquisition and application bindings retain source/connection/fingerprint checks; Processing receives the pure layout and validates the plan, revisions and input receipt. The HTTP preview still projects its source descriptor at the application boundary. Runtime apply consumes one `resume_mode` and the typed checkpoint, preserving contribution/configuration pins and changed-file rejection. The worker's construction path already delegates to Acquisition, so no worker change is needed; actual worker and spawned-process tests cover this path.

The SDK export list, PluginContext, loader, manifests/API-version dispatch, frontend and OpenAPI document are unchanged in S1b. Their coordinated constructor/API switch remains S1f. Existing S1a changes are preserved. The initial new domain/owner cases failed **37 tests** before implementation; subsequent regressions separately reproduced failed cleanup, stale observation attachment, EOF/cancellation ordering and terminal socket error projection before their fixes. The focused group passed **785 tests**; the full headless run passed **3630 tests** with 37 deselected; integration passed **36 tests** with one Windows-only skip and 3630 deselected. A separate final transport/owner run passed **69 tests**, including four `EBADF` / `ENOTCONN` cases added after the full run had collected its tests; those four cases were first verified to fail. All **8 import contracts** and `git diff --check` passed. The counts overlap and must not be summed.

The controlled environment and localhost proxy bypass match S0/S1a. Integration retained the two existing WebSocket deprecation warnings. Real GUI/hardware and Windows/Linux execution were not exercised locally. Commands, logs, JUnit reports and the S1b-only change inventory are in `/private/tmp/omnibci-s1b-njo5w8wz`. At that checkpoint S1b was complete; S1c and the coordinated S1f API switch remained pending. No Git commit was created.

**S1c complete (2026-11-20):** The [algorithm ports](../omnibci/domain/ports/algorithm.py) now declare `EpochBoundary`, batch `detect(raw_data)`, `apply(data, *, pre_trigger_data)`, and the required decoder setup/begin_epoch/should_decode/decode/teardown methods. `decode` takes keyword-only `latest_raw_segment`; `FilterContext`, first-marker methods and `for_new_epoch` are removed without aliases or fallback paths.

The existing ProcessingState validates the complete ordered batch before applying any boundary. The built-in detector scans the event row once without flattening; pre-trigger buffering, opening/closing sample rules, simultaneous end/start and repeated-event behavior remain host-owned. Pipeline calls lend read-only views, preserve the original pre-trigger reference across ordered filters and validate every output's real numeric dtype and shape before decoding. The readiness gate accepts boolean scalars only. Forced short attempts, target IDs, empty windows and normal/drain duplicate protection retain their behavior.

One existing SignalProcessor owner prepares reusable model resources before acknowledging readiness/application. Every start resets working state without revealing its event label to the decoder. Failed setup cleans partial resources; cancellation, replacement, input reset, stop and exceptional exit release owned models. Reset cancels a pending candidate before transitioning. Failed cleanup rejects success and blocks reuse; shutdown still attempts other releases and does not retry the failed teardown implicitly. Epoch initialization failure records an error without retaining an uninitialized epoch. CCA templates now prepare in setup; FBCCA filter coefficients prepare there once instead of being rebuilt during decoding. No session, lifecycle base or compatibility layer was introduced.

Steady-state processing does not prepare models on epoch starts. Configuration preparation still executes on the existing serialized owner, so a costly third-party setup can pause processing. Use the existing drained/module apply path for such model changes; S1c adds no parallel preparation scheduler or deadline guarantee.

Processing construction supplies only mapped `context.input_signal` (the loader still supplies the package resource root). SDK changes only replace the `FilterContext` export with `EpochBoundary`. The PluginContext definition, constructor signature, loader, manifests, API-version dispatch, frontend, OpenAPI and `.gitignore` remain unchanged in this step. The executable decoder example and buffer benchmark use the new methods; the existing author guide is marked historical rather than expanded before S1f.

**S1c verification:** The initial 38 new contract tests failed before implementation. Additional cleanup cases reproduced three failure paths, and readiness validation reproduced seven failures, before the corresponding fixes. The final processing/domain/SDK group passed **574 tests**. Integration passed **36 tests**, with one Windows-only skip and the two existing WebSocket deprecation warnings. All **8 import contracts** passed. The first full run found one direct CCA test caller missing setup; it was migrated and passed in the final focused run. The final full headless run passed **3686 tests**, with 37 deselected. `git diff --check` also passed. These groups overlap and must not be summed.

**Detector performance:** After regression runs completed, the local benchmark compared captured pre-S1c method bodies with the actual new detector and host boundary validation. All **15 cases produced identical boundaries**, including C-order, F-order and strided matrices. Arrays were allocated before timing: 32 signal rows plus one event row, float64. Each path warmed up 100 times, then ran in alternating pairs 2500 times (600 for the 512-marker case). Value construction and read-only view/whole-batch validation are included; acquisition, IPC, epoch assembly, filters and decoding are excluded.

Environment: macOS-14.8.3-x86_64-i386-64bit-Mach-O; Python 3.14.2; NumPy 2.4.4. Times below are microseconds, for C-order matrices.

| Samples / markers | Old median | New median | Old p99 | New p99 |
| --- | ---: | ---: | ---: | ---: |
| 256 / 0 | 2.67 | 2.72 | 11.13 | 12.20 |
| 256 / 1 (middle) | 11.28 | 8.50 | 32.59 | 26.78 |
| 256 / 1 (end) | 4.18 | 6.72 | 8.35 | 21.64 |
| 256 / 8 | 53.09 | 19.20 | 118.61 | 43.22 |
| 256 / 64 | 412.37 | 96.83 | 904.47 | 226.83 |
| 1024 / 32 | 259.87 | 54.29 | 547.33 | 111.94 |
| 1024 / 512 | 4229.50 | 710.94 | 7463.44 | 1298.12 |

At 256 samples, 8/64-marker medians improve by about 2.8x/4.3x. The empty case is essentially unchanged, while a single marker at the final sample is about 2.54 microseconds slower: the old path avoids a second scan there, and the new path constructs a validated boundary value. Dense-path cost is O(N + K), without repeated suffix scans. This is a local microbenchmark, not an end-to-end latency or hard real-time guarantee. The script, captured baseline and complete median/p99 results are `compare_detector.py`, `before/` and `detector-performance.json` in the run directory below; the exploratory run during regression is kept separately.

Commands, captured pre-S1c files, logs, JUnit reports and the S1c-only change inventory are in `/private/tmp/omnibci-s1c-q2232j3s`. Runs use isolated configuration and localhost proxy bypass. GUI/hardware and Windows/Linux execution were not exercised locally. No Git commit was created. S1d–S1f remain separate steps.

**S1d first batch complete (2026-11-20):** [Paradigm](../omnibci/domain/ports/paradigm.py) now requires `render_frame(elapsed_seconds)` and `is_presenting()`. [Stimulator](../omnibci/infrastructure/stimulation/stimulator.py) calls readiness directly, requires a Python `bool`, rejects uninitialized paradigms, missing methods and invalid results, and propagates query exceptions. It no longer treats a failed query as idle.

[Stimulation](../omnibci/infrastructure/stimulation/application.py) checks whether rendering ended before querying readiness. A closed renderer rejects queued trials even if its last state was busy, and discards feedback without invoking the released paradigm. An open renderer still admits one trial at a time and processes feedback while busy. Readiness failure sets the existing stop flag, refuses further trial/result admission, and sends pending trial failures through the existing awaited peer write before propagating the error into the existing shutdown path. Delivery failures are attached to the original exception. No new lifecycle owner, state framework or compatibility path is introduced.

The first contract run reproduced **25 failures**; three additional receipt-delivery/shutdown tests also failed before implementation. After implementation, **270 focused tests**, **3718 full headless tests**, **36 integration tests** and all **8 import contracts** passed. Integration retained one Windows-only skip and the two existing WebSocket deprecation warnings. `git diff --check` passed. Test groups overlap and must not be summed. Runs used isolated configuration and localhost proxy bypass; GUI/hardware and Windows/Linux execution were not exercised. Commands, logs and JUnit reports are under `/private/tmp/omnibci-s1d-batch1-7qvx6tp2`.

This batch changes only three production files and their required tests. Classic40 already implements both required methods. Narrow capabilities, setup/teardown ownership, the alternate thread path, trigger drivers/timing, graphics resources and method renames remain for later S1d batches. SDK exports, PluginContext, constructors, manifests and payload schemas remain unchanged; the shared S1f switch is still pending.

Remove `signal_input_contract` dispatch while retaining decoder `channel_requirements`. Add the agreed local trial/result/command schemas and update the corresponding host validation and built-in payloads together. Package-wide domain restrictions no longer determine execution ownership; kind does. Keep unrelated transport and configuration functionality intact.

For the shared switch, update producers, consumers and tests in the same integration unit. Negative tests may still contain obsolete API strings to prove rejection. Do not use a text search for old names as a substitute for loading and executing every role. A new public signature is not complete while a built-in or process factory still uses the old call shape.

**Regression foundation:** existing domain tests; acquisition/source observation, checkpoint and preview tests; processing mapping/pipeline/epoch tests; Stimulation and Actuation suites; `test_plugin_sdk.py`, `test_builtin_contributions.py`, `test_plugin_api_versions.py`, `test_decoder_plugin_example.py` and explicit-parameter tests under `tests/infrastructure`.

**Gate:** one coherent role/constructor contract across built-ins and external-style test packages, with existing functional scenarios preserved. S2–S6 are still required before declaring the overall refactor complete.

### S2 Fix Plugin Management, Configuration and Marketplace ownership

**Change boundary:** application use cases and static package operations; no new execution host.

1. Move the existing `PluginSelection` and kind vocabulary from [Configuration models](../omnibci/application/configuration/models.py) into a small application leaf module, proposed as `omnibci/application/plugin_identity.py`. It has no Configuration, catalog or store imports. The existing [package contracts](../omnibci/application/plugin_packages.py) already import the configuration-catalog port, so putting shared identity there without separating dependencies risks another cycle. Move the definitions rather than copying them; update imports directly and add no alias module. Profiles, slots, revisions and input preferences remain Configuration-owned.
2. Move `propose_profile`, `create_version_profile` and their result value from [PluginPackageService](../omnibci/application/services/plugin_package_service.py) to Configuration. Expose narrow read-only saved/accepted reference queries; retain runtime reference callbacks. Bootstrap wires these providers and the existing shared admission guard.
3. Move local installation commit out of [PluginMarketRepository](../omnibci/application/plugin_market.py) and its adapter into the existing installation/package use case. The adapter still downloads and verifies evidence. Provide a narrow proof-revalidation operation at publication time; moving commit must not drop the current expiry/identity check or make application code implement cryptography.
4. Route catalog, installer and tooling through the shared static validator introduced with S1. Retain archive/path checks in the installer and JSON schema validation in its existing owner. Reuse descriptors rather than inventing a parallel public manifest model.
5. Inject one store directory, node and catalog scope into [catalog adaptation](../omnibci/infrastructure/plugins/configuration_catalog.py), [lifecycle store](../omnibci/infrastructure/plugins/package_lifecycle_store.py) and installation. Inspect disabled-package definitions separately from enablement. Replace ambiguous global availability with operation-specific eligibility/reasons for the selected node.
6. Extend the existing receipt/management records for host-recorded origin and accepted evidence. Keep the installer's hash inventory, immutable-version conflicts and idempotency. Review affected management DTOs without returning filesystem paths, proofs or runtime objects.

**Checks:** package composition/lifecycle/profile service tests; catalog and schema tests; market adapter/repository tests; package installer/store tests; existing package/market/configuration router tests. Add cases for an explicit non-default store, disabled metadata inspection, reference-query isolation, cross-source conflicting bytes and proof expiry between download and commit.

**Gate:** market and local archive installs use the same publication path; installation does not create Profiles or Instances; profile copying preserves its current checks; Plugin Management no longer reads another module's repository implementation. Existing market signatures and offline local import still work.

### S3 Complete artifact preparation, loading and reference handoff

**Change boundary:** the private record passed from preparation to a Module Host and its ownership lifetime.

1. Extend the existing private binding/preparation records with exact artifact digest, node/target, relevant environment identity, origin/admission, entry point and static declarations. Store common environment facts once where practical. Keep the editable selection small and these records outside the SDK.
2. Resolve and verify the selected artifact on its execution node. Managed artifacts use installation receipts; built-ins need host-distribution content identity, not a fictional upload receipt. Check the payload inventory, eligibility and local enablement/denial before import. Initial support can retain one selected artifact per package version per node; reject unsupported target layouts explicitly.
3. Under the existing admission guard, recheck final state and publish the pending reference before disable/removal can proceed. Retain candidate/old references until their owners settle. Transfer pending to active without a gap; retain unresolved cleanup through the existing recovery path.
4. Change workers and [runtime loading](../omnibci/infrastructure/plugins/runtime_configuration.py) to consume the resolved record without another global catalog search. Processing supplies actual mapped input; the generic loader does not inspect live Acquisition or perform repository access.
5. Make [PluginLoader](../omnibci/infrastructure/plugins/loader.py) cache modules by exact artifact within a process, initialize real nested packages normally, and create a fresh Instance per binding. Keep the synthetic root, global path isolation and failed-import cleanup. No per-origin loader or dynamic unload manager is added.
6. Enforce one package-mutation owner per node/store with local ownership and existing file locks. Presentation and any local administration use that owner. A second controller must not bypass runtime references; unknown resource ownership blocks destructive mutation. Worker processes are consumers, not inventory writers.

**Checks:** `test_runtime_configuration.py`, plugin contribution/loader tests, `test_plugin_lifecycle_admission.py`, `test_runtime_package_ownership.py`, package lifecycle tests and spawned-worker tests. Add a preparation-versus-disable race, mismatched artifact rejection, independent instances sharing code, normal nested initialization, import failure/concurrency and cleanup failure cases.

**Gate:** the bytes checked at preparation are the bytes selected for import; installed packages prepare offline; pending/running/recovery resources cannot be removed; changed development code requires a fresh snapshot/host. Preserve existing reference guarantees instead of replacing them with a lease framework.

### S4 Narrow application and external-node interfaces

**Change boundary:** runtime coordination, receipts and transport projections; keep the existing engine and apply coordinator.

1. Narrow [EnginePort/EngineHandle](../omnibci/application/ports/engine_port.py) around the operations actually used by application services. Remove direct worker/gateway/bus graph access from those consumers. Use one factory input with an explicit snapshot, including the empty-foundation case. Existing managers implement the needed output lifecycle port directly; no wrapper around every method is required.
2. Reuse module configurations, bindings and [event/receipt values](../omnibci/application/events.py). Add typed ownership records and one codec at duplicated preparation/input boundaries. Preserve copying/isolation. Do not mechanically replace every private dictionary or invent a general settings framework.
3. Replace numeric-range scope inference in [command correlation](../omnibci/application/services/command_result_service.py) with explicit scope/validation entries for dispatched commands. Keep event numbers, correlation, late-result handling and definitely-unsent versus uncertain outcomes. Existing module validators can supply the parsers; no extensible command registry is needed.
4. Preserve [runtime apply](../omnibci/application/services/runtime_apply_service.py), compare-and-swap, history and recovery. Use closed strategy values and explicit outcomes through existing records. A successful runtime change with failed history persistence remains distinguishable from runtime failure; no file/process/device transaction is claimed.
5. Extend [node configuration](../omnibci/application/node_configuration.py), [node client](../omnibci/infrastructure/plugins/node_client.py) and readiness exchange to carry actual artifact/environment pins with binding, operation and generation. Resolve paths locally and compare the returned identity before accepting readiness. Preserve node-local validation and stale-peer rejection; send no controller paths or Python objects.
6. Keep remote package mutation unavailable until that node has an authoritative configuration/runtime reference view and mutation owner. Existing external-node execution and local validation remain required; centralized remote install/remove is a separate capability, not a prerequisite for executing pre-provisioned packages.
7. Move genuinely shared peer mechanics below module orchestrators where needed, retain role-specific admission, preserve trial origin through actuation results, and give presentation codecs application-owned values. Replace ResultsService's whole-engine dependency with the existing generation value supplied through a callable; leave its retention policy to the explicit follow-up.

**Checks:** engine port/lifecycle, independent modules, runtime apply/failure/recovery, command result, input-boundary, node configuration/synchronization, peer identity/quiescence, actuation translation and HTTP/WS contract tests. Add mismatched node artifact/environment receipts, lost response/late result and module-snapshot isolation cases.

**Gate:** application services use declared operations, not runtime graph attributes. Current local/remote execution, compensation, history, no-replay and uncertainty semantics remain intact. Unavailable remote management is reported as unavailable rather than silently acting on the controller's store.

### S5 Finish the three plugin entry workflows

**Change boundary:** reuse the completed package pipeline and hosts for local author workflows.

1. Keep existing market selection/download and ZIP upload endpoints. Verify that their installed result follows the same enable/select/prepare path. Repository connection is unnecessary for a local package or an already accepted installed release.
2. Add one small tooling entry point with `validate`, `package` and `develop` operations. A proposed location is `omnibci/plugin_cli.py`, registered as `omnibci-plugin` in [pyproject.toml](../pyproject.toml); neither the file nor command exists at the inspected baseline. Use the standard argument parser and shared implementation functions, not a new CLI framework or SDK runtime.
3. `validate` and `package` operate on an explicitly selected plugin root without importing it. Reuse the manifest/schema rules and portable-path limits; package deterministically, exclude VCS/virtual environments/caches/previous outputs, and retain required resources. Do not infer entry points or run project build/install hooks.
4. `develop` creates an immutable snapshot through the same installer in an isolated store/configuration and launches the owning module host. Companion Contributions come from that development inventory or built-ins. Use fresh-host stop/restart; retain artifacts on uncertain cleanup. No per-request process-environment switching or normal-inventory override is allowed.
5. Remove unmanaged source-directory discovery from ordinary resolution in the same change that provides the explicit development path. Keep existing executable examples working and add small conformance fixtures for all seven kinds; defer a project generator and new developer prose until S6.

**Checks:** existing package installer, example and runtime tests plus focused tooling/source-workflow tests. Exercise offline local import; a shared repository ZIP requiring explicit plugin-root packaging; malformed paths and declarations; the same bytes arriving through both sources; an edit at the same development version; isolated companion selection; restart blocked by unresolved cleanup; and a normal installed inventory unaffected by development.

**Gate:** market users, recipients of packages/source and local authors can all reach the same API/host lifecycle. Static tools execute no plugin hooks, and no ordinary runtime silently loads mutable workspace code.

### S6 Integrate clients, verify the release and finish documentation

**Change boundary:** complete the coordinated implementation and make its actual behavior reviewable.

1. Run the full regression groups in section 4 after the focused checks. Exercise one end-to-end configuration with file acquisition, processing and the logger effector, plus headless Stimulation lifecycle checks. Preserve existing scenario coverage when rewriting fixtures.
2. Validate installed-wheel manifests, schemas and stimulus resources outside the editable checkout using the existing CI procedure. Run the supported OS matrix and applicable display/device checks; record actual results and remaining target limits.
3. If API response/request shapes changed, update presentation schemas, [OpenAPI](openapi.json), generated REST types and real frontend/node consumers together. Keep stable useful wire shapes where internal ownership changes do not require a new shape. Apply the agreed Plugins UI naming in labels, route callers and tests without an obsolete-name redirect; do not redesign the interface.
4. Remove superseded imports, methods, constructor dispatch and obsolete discovery paths after checking their callers. Keep required renderer adapters, codecs, resource files and unrelated formats. Recreate only deliberately affected pre-launch data with explicit operator action; do not automatically delete user configuration or add a historical migration framework.
5. Update existing manifest, configuration, decoder/example and validation documentation to the implemented v1. Only now write the new developer guide, using commands and complete examples already exercised by the tests. Mark implementation status accurately in the design/review documents.

**Gate:** all core completion criteria in section 1 are evidenced; no implementation item is marked complete merely because its design exists. Record changes, tests, actual platform results and intentionally deferred findings in the final review.

## 4. Verification groups

Use the project's Python 3.14 environment and declared development dependencies. The commands below are planned implementation checks, not commands executed while writing this plan. Run affected tests after each coherent change; run the full groups at integration, and repeat them only when later changes or failures justify it.

```sh
python -B -m pytest -p no:cacheprovider -m "not gui and not hardware and not integration"
python -B -m pytest -p no:cacheprovider -m "integration and not gui and not hardware"
lint-imports --no-cache
```

For affected REST/client contracts, regenerate the actual outputs and review their changes:

```sh
python scripts/export_openapi.py docs/openapi.json
npm --prefix frontend run codegen
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

Use [the existing test workflow](../.github/workflows/tests.yml) for Linux/macOS/Windows headless and integration results, import checks, and installed-wheel/resource validation. Use [the contract workflow](../.github/workflows/contract.yml) to ensure generated OpenAPI matches the committed implementation. Do not modify CI or skip tests merely to hide a refactor failure. A focused fake validates software semantics, not physical onset, timing or device stop.

## 5. Separate distribution and backend follow-up

These items have explicit scope and are not prerequisites for the first domain change. Preserve the current behavior until its replacement is implemented and verified.

| Work item | Concrete next implementation | Entry condition and acceptance |
| --- | --- | --- |
| M1 Curated publication | Extend the existing signed static repository with reviewed publisher authorization, monotonic revisions, publication/revocation state and operator key procedures. Start with maintained metadata and a controlled publishing process, not an account/portal service. | Core install/start behavior is complete. Wrong publisher, rollback/stale proof, modified bytes and conflicting releases fail; local denial is recorded; already accepted eligible packages prepare offline. |
| M2 Multiple target artifacts | Extend release metadata, selection and storage only when one version must distribute distinct target builds. Preserve exact artifact pins and deterministic eligibility. | Concrete native target artifacts and tests exist. Unsupported/ambiguous candidates fail; a node selects its own target without substituting the management server's environment. |
| M3 Remote package administration | Route installation and lifecycle mutations to the execution node's owner, with authoritative saved/accepted/runtime references and explicit operation outcomes. | Node ownership and reference authority are available. Disconnection or unknown references cannot permit unsafe removal; controller inventory is never presented as the remote inventory. No cross-node transaction is needed. |
| B1 Results retention | Decide whether queries promise bounded recent inspection or complete session history, then implement that contract in ResultsService. | The retention requirement is settled before changing storage. Pagination reports retained data honestly; do not add a database or silently evict promised history. The generation-only dependency reduction is already in S4. |
| B2 WebSocket delivery | Give each client one writer, a bounded queue and an explicit slow-client/disconnection policy using the existing broadcaster. | Implement as a separate client-delivery change after core runtime contracts stabilize. A stalled client cannot delay others; order and memory bounds hold; no replay promise exceeds retained data. |

Self-service publishing, richer search, arbitrary Python environments, frontend executable plugins, hot reload, plugin-to-plugin calls and a project generator remain outside this plan until a concrete need establishes their scope. S0–S6 do not acquire those capabilities implicitly by adding another abstraction.

## 6. Findings-to-work mapping

| Review findings | Implementation owner and disposition |
| --- | --- |
| D01–D02 | S1a: values, validation, host-only metadata and callers. |
| D03, R01 | S1b: Acquisition contract and runtime boundary. |
| D04–D06, R02 | S1c: processing contracts, mapping, state and consumers. |
| D07–D09, R03 | S1d: Stimulation contracts, one owner and cleanup. |
| D10, R04 | S1e: Actuation execute/quiesce and serialized ownership. |
| K01 | S1f: complete SDK/constructor/API replacement. |
| K02 | S1f/S2: one static validator and manifest meaning; S5 reuses it. |
| K03 | S2/S3: installation ownership, origin and exact local pins; M2 owns multi-target distribution. |
| K04 | S2/S3: contract ownership, explicit catalog/node scope and eligibility. |
| K05 | S3/S4: preparation/reference handoff, one mutation authority and node receipts; M3 owns remote administration. |
| K06 | S3: artifact code caching, ordinary nested imports and separate Instance lifetime. |
| K07 | S2/S3: retain one installer receipt and offline runtime admission; M1 supplies publication-policy updates. |
| A01–A04 | S4: narrow existing ports, boundary records, command scope and explicit apply outcomes. |
| A05 | S4 removes engine-graph coupling; B1 decides and implements retention. |
| T01–T02 | S4: shared peer mechanics and output causality without replacing transport. |
| T03 | S4/S6: application-owned outputs, presentation codecs and affected generated/client contracts. |
| T04 | B2: bounded WebSocket client delivery, tracked separately from plugin API completion. |

This mapping accounts for all 30 review findings. Splitting a finding does not close its deferred portion. During implementation, record evidence against these gates rather than adding more design-only layers.
