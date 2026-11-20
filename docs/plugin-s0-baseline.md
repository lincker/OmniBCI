# Plugin S0 baseline and change inventory

Recorded on 2026-11-19 for [implementation step S0](plugin-implementation-plan.md#s0-establish-the-baseline-and-change-inventory). This is an inspection of existing code and tests, not an implementation of [Plugin API v1](plugin-api-v1.md). Package, Contribution, Instance and module names follow the [architecture proposal](plugin-marketplace-design.md).

## 1. Scope and code baseline

The S0 acceptance criteria are: identify the starting revision and preserve existing work; run the existing backend headless, integration and import checks in isolated configuration; record current interfaces, implementations and consumers; separate test-environment failures from product failures. No source, test, manifest, configuration or generated client file is changed. Focused failing tests belong immediately before the relevant S1–S4 implementation, not this inspection.

| Item | Observed baseline |
| --- | --- |
| Branch | `refactor/module-interface` |
| HEAD | `0bdd1dd2a296fea4b919d7f1a186a6fb76477b8e` |
| Local `dev` | Same commit as HEAD. The cached `origin/dev` comparison reports local `dev` ahead by 15 commits; no fetch was performed during S0, so this is not a claim about current remote freshness. |
| Platform | macOS 14.8.3, x86_64 |
| Project environment | `venv/bin/python`, Python 3.14.2; pytest 9.0.3; NumPy 2.4.4 |
| Implemented plugin contract | Loader accepts API `"2"`; all 10 inspected manifests declare API `"2"` and `manifest_version = 2`. API v1 is still a design. |
| Current inventory | 9 built-in Packages, 11 built-in Contributions, all 7 kinds; 1 external example Package with 1 decoder Contribution. |
| Public SDK | 17 exports; proposed v1 has 18. These counts describe different contracts, not two supported runtime versions. |

Before S0 there were no staged changes. The following tracked files already had user changes: `docs/decoder-plugin-development.md`, `docs/frontend-ui-ux-design.md`, `docs/module-config.md`, `docs/plugin-manifest.md` and `docs/plugin-profiles.md`.

Pre-existing untracked paths were `.agents/`, `.vscode/`, `AGENTS.md`, `docs/backend-interface-review.md`, `docs/plugin-api-v1.md`, `docs/plugin-implementation-plan.md`, `docs/plugin-marketplace-design.md`, `docs/stimulation-interface-design.md`, `frontend/tsconfig.tsbuildinfo` and `skills-lock.json`. They are preserved. A SHA-256 snapshot of 615 existing tracked and nonignored untracked files was captured before running the checks; it is used to detect incidental changes independently of Git's tracked-file diff.

## 2. Test baseline

The controlled runs completed successfully. Together the two disjoint pytest groups produced **3461 passed, 1 platform skip, 0 failures**. These results are the S0 baseline, not evidence that the proposed API v1 has been implemented.

| Check | Result | Scope |
| --- | --- | --- |
| Headless backend | **3425 passed, 37 deselected**, no skips/failures, exit 0; 404.47 s | `not gui and not hardware and not integration` |
| Integration backend | **36 passed, 1 skipped, 3425 deselected**, exit 0; 89.19 s | `integration and not gui and not hardware` |
| Import architecture | **8 kept, 0 broken**, exit 0; 188 files and 524 dependencies, 0.40 s | Existing import-linter contracts, with their existing exclusions. |

The skip is `test_killing_job_owner_also_terminates_its_child` in [desktop process-tree tests](../tests/infrastructure/test_desktop_process_tree.py): Windows Job Objects are unavailable on this macOS host. Integration also emitted two existing WebSocket deprecation warnings. There are no unresolved failures in these controlled local runs.

Run commands from the repository root using the existing environment:

```sh
venv/bin/python -B -m pytest -p no:cacheprovider -o addopts=--strict-markers -q -m 'not gui and not hardware and not integration' --durations=10
venv/bin/python -B -m pytest -p no:cacheprovider -o addopts=--strict-markers -q -m 'integration and not gui and not hardware' --durations=10
venv/bin/lint-imports --no-cache
```

The actual pytest invocations also supply `--junitxml` pointing into the temporary run directory. The runner removes inherited `OMNIBCI_*` settings and sets separate temporary `OMNIBCI_CONFIG_DIR`, `OMNIBCI_PLUGIN_DIR` and `OMNIBCI_MARKET_CACHE_DIR` for each suite. It sets `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUTF8=1`, `MPLBACKEND=Agg` and a temporary `MPLCONFIGDIR`. The existing [test fixture](../tests/conftest.py) also isolates module configuration per test. No dependencies are installed or upgraded.

Two environment effects must not be mistaken for an API defect:

1. The sandbox rejects loopback `bind()` with `PermissionError`. An initial headless attempt was interrupted with exit 130 before completion. Some tests without the integration marker also use sockets, so both final pytest groups run with loopback access. The interrupted attempt is not a passing or failing baseline.
2. The first integration run outside the sandbox produced **33 passed, 3 failed, 1 skipped, 3425 deselected** in 131.11 s. All three failures were local HTTP timeouts. A separate loopback probe found macOS system HTTP/HTTPS proxies, despite no proxy environment variables: the default opener timed out, while a direct opener reached the same server with HTTP 200. Subsequent test processes set `NO_PROXY` and `no_proxy` to `127.0.0.1,localhost,::1`. System proxy settings and repository configuration are unchanged.

The first-run failures were:

| Existing test | First-run observation |
| --- | --- |
| [Market adapter](../tests/infrastructure/test_market_adapter.py), `test_real_signed_http_to_disabled_install_enable_profile_and_runtime_plan` | Fetching the loopback index timed out. |
| [Market repository](../tests/infrastructure/test_market_repository.py), `test_signed_local_http_repository_installs_and_refuses_real_redirects` | Fetching the loopback index timed out. |
| [Node client](../tests/infrastructure/test_node_client.py), `test_authenticated_configuration_never_follows_redirect` | Timed out before receiving the expected HTTP 302 rejection. |

All three tests passed in the complete integration rerun with loopback proxy bypass. Their assertions were unchanged. This confirms the local proxy explanation; it does not justify changing application networking or weakening redirect checks.

Logs, JUnit reports, command/exit/duration records, the before-state hash snapshot and static inventories are local evidence under `/private/tmp/omnibci-s0-20261119-154003`. `first-integration.log`, `first-integration.xml` and `first-integration-result.json` preserve the first completed integration attempt; `headless-sandbox.log` preserves the interrupted attempt. These temporary artifacts are not repository deliverables; this document records the durable conclusions.

The baseline is local and headless. It does not establish Windows/Linux results, installed-wheel behavior, real EEG/trigger hardware operation, GPU display behavior or physical stimulus/output timing. The [existing CI workflow](../.github/workflows/tests.yml) and S6 retain those checks. Passing import rules does not prove that an abstract interface declares every method its runtime calls.

## 3. Packages, Contributions and implementations

Every row below is a manifest declaration, not a separately installed runtime Instance. All listed Package versions are currently `0.1.0`. Package IDs retain their current spelling here; no identity rename is implied.

| Package / manifest | Contribution ID | Kind | Entry point / implementation |
| --- | --- | --- | --- |
| [OmniBCI.file_reader](../omnibci/infrastructure/plugins/acquisition/file/manifest.toml) | `default` | acquisition | [reader:FileReader](../omnibci/infrastructure/plugins/acquisition/file/reader.py) |
| [OmniBCI.neuroscan_curry8_reader](../omnibci/infrastructure/plugins/acquisition/neuroscan/curry8/manifest.toml) | `default` | acquisition | [curry8:Curry8DataReader](../omnibci/infrastructure/plugins/acquisition/neuroscan/curry8/curry8.py) |
| Same Curry8 Package | `trigger` | trigger | [trigger:Curry8Trigger](../omnibci/infrastructure/plugins/acquisition/neuroscan/curry8/trigger.py) |
| [OmniBCI.neuroscan_scan45_reader](../omnibci/infrastructure/plugins/acquisition/neuroscan/scan45/manifest.toml) | `default` | acquisition | [scan45:Scan45DataReader](../omnibci/infrastructure/plugins/acquisition/neuroscan/scan45/scan45.py) |
| Same Scan45 Package | `trigger` | trigger | [scan45:Scan45Trigger](../omnibci/infrastructure/plugins/acquisition/neuroscan/scan45/scan45.py) |
| [OmniBCI.classic40_cca](../omnibci/infrastructure/plugins/algorithms/decoders/classic40_cca/manifest.toml) | `default` | decoder | [classic_40_cca:Classic40CCA](../omnibci/infrastructure/plugins/algorithms/decoders/classic40_cca/classic_40_cca.py) |
| [OmniBCI.classic40_fbcca](../omnibci/infrastructure/plugins/algorithms/decoders/classic40_fbcca/manifest.toml) | `default` | decoder | [classic_40_fbcca:Classic40FBCCA](../omnibci/infrastructure/plugins/algorithms/decoders/classic40_fbcca/classic_40_fbcca.py) |
| [omnibci.default_epoch_detector](../omnibci/infrastructure/plugins/algorithms/epoch/manifest.toml) | `default` | epoch_detector | [detector:DefaultEpochDetector](../omnibci/infrastructure/plugins/algorithms/epoch/detector.py) |
| [omnibci.baseline_correction](../omnibci/infrastructure/plugins/algorithms/filters/manifest.toml) | `default` | filter | [baseline_correction:BaselineCorrection](../omnibci/infrastructure/plugins/algorithms/filters/baseline_correction.py) |
| [omnibci.logger](../omnibci/infrastructure/plugins/effectors/logger/manifest.toml) | `default` | effector | [effector:LogEffector](../omnibci/infrastructure/plugins/effectors/logger/effector.py) |
| [OmniBCI.classic40SSVEP](../omnibci/infrastructure/plugins/paradigms/classic40SSVEP/manifest.toml) | `default` | paradigm | [ssvep:Classic40SSVEP_2](../omnibci/infrastructure/plugins/paradigms/classic40SSVEP/ssvep.py) |
| [example.oz-polarity-demo](../examples/decoder_plugin/manifest.toml) | `oz-polarity` | decoder | [decoder:OzPolarityDemo](../examples/decoder_plugin/decoder.py) |

All Contributions declare configuration schema version 1 and a package-local schema: `default.schema.json`, except the two trigger Contributions use `trigger.schema.json`. CCA, FBCCA and the example declare `signal_input_contract = 2`; the detector and filter declare `1`. S1 removes that dispatch, preserves decoder channel requirements, and adds the agreed trial/result/command schemas. Plugin API v1 does not require changing `manifest_version = 2` or an unrelated schema version.

Two more concrete role implementations exist outside manifests: `_NoTrigger` in [Stimulator](../omnibci/infrastructure/stimulation/stimulator.py) and `WaitingDecoder` in the [epoch benchmark](../scripts/benchmark_epoch_buffer.py). Include their role methods in the coordinated update. Together with the 12 manifest Contributions this makes 14 concrete role class declarations outside tests, not 14 discoverable Contributions.

Also retain and update the relevant non-entry-point helpers: the [file adapter](../omnibci/infrastructure/plugins/acquisition/file/adapter.py), [feedback drawing helper](../omnibci/infrastructure/plugins/paradigms/classic40SSVEP/feedback_display.py), both [PyOpenGL](../omnibci/infrastructure/stimulation/adapters/pyopengl_renderer.py) and [ModernGL](../omnibci/infrastructure/stimulation/adapters/moderngl_renderer.py) adapters, and the existing trigger/device drivers and stimulus resources. Renaming the public drawing capability does not remove these implementations.

## 4. Domain interfaces and direct execution consumers

The required target signatures are defined once in [Plugin API v1](plugin-api-v1.md#5-role-contracts). This table records what must change together, rather than duplicating those definitions.

| Interface / current issue | Current consumers that must move with it | Required adjustment / step |
| --- | --- | --- |
| [SignalInfo / ChannelDescriptor](../omnibci/domain/signal_info.py): processing layout currently carries source/connection identity, native event description and provenance. | [Channel mapping](../omnibci/domain/channel_mapping.py), source implementations/file adapter, [acquisition metadata](../omnibci/infrastructure/acquisition/metadata.py), [Acquisition](../omnibci/infrastructure/acquisition/acquisition.py), [acquisition service](../omnibci/application/services/acquisition_service.py), [signal setup](../omnibci/application/services/signal_setup_service.py), [source expectations](../omnibci/application/source_expectations.py), SDK and their codecs. | S1a–c: expose only the actual signal layout; retain observation facts and host identities in their owning internal records. Preserve fingerprint, stale-connection and mapping checks while updating serialization. |
| [SignalChunk](../omnibci/domain/signal.py), [Classification](../omnibci/domain/classification.py), [Epoch](../omnibci/domain/epoch.py), `Prediction`: timestamp annotation and numeric validation are incomplete; transport/result metadata is wider than plugins need. | Acquisition/preprocessing, channel mapping, [SignalProcessor](../omnibci/infrastructure/signal_processing/signal_processor.py), [processing state](../omnibci/infrastructure/signal_processing/processing_state.py), [EpochProcessor](../omnibci/infrastructure/signal_processing/epoch/epoch_processor.py), [pipeline](../omnibci/infrastructure/signal_processing/epoch/pipeline.py), results/IPC codecs and the benchmark. | S1a/c: keep internal chunk/epoch/classification behavior; validate at actual boundaries, remove the SDK chunk export, and introduce the small public result types. Do not replace every internal record. |
| [SignalSource](../omnibci/domain/ports/acquisition.py): plugin state flags and boolean transitions; two metadata calls; separate checkpoint/resume flags and an untyped checkpoint. | [Worker](../omnibci/infrastructure/acquisition/worker.py) constructs it. Acquisition calls connection/stream/read/metadata/checkpoint operations and reads plugin flags for status. [Runtime apply](../omnibci/application/services/runtime_apply_service.py) consumes resume capability; [checkpoint selection](../omnibci/application/source_checkpoint.py), metadata service and source expectations consume returned state. | S1b: host-owned lifecycle; `trigger_mode`, `resume_mode`, `observe()` and typed checkpoint. Move all three sources and metadata/status projections together. Preserve preview without layout, partial inspection errors, joined reads and file resume without replay. |
| [Filter / FilterContext](../omnibci/domain/ports/algorithm.py): `apply(data, ctx)` exposes unused event/rate fields; advertised shape changes conflict with unchanged pipeline metadata. | Pipeline constructs `FilterContext` and calls each stage; both [initial signal configuration](../omnibci/infrastructure/signal_processing/signal_configuration.py) and [replacement pipeline configuration](../omnibci/infrastructure/signal_processing/pipeline_configuration.py) load ordered filters. | S1c: `apply(data, *, baseline)` and constructor `input_signal`; validate layout-preserving outputs. Update BaselineCorrection and both construction paths. |
| [Decoder](../omnibci/domain/ports/algorithm.py): `for_new_epoch()` implicitly reuses/clones Instances, with no explicit model setup/reset/cleanup lifecycle. | SignalProcessor selects the epoch decoder; EpochProcessor calls `should_decode`; pipeline calls `decode(window, latest_samples)`. Both processing factories, CCA, FBCCA, external example and benchmark depend on these shapes. | S1c: setup/begin_epoch/teardown with one prepared Instance; keyword `latest_raw_segment`; preserve raw gate versus filtered input, forced/short attempts and target numbering. |
| [EpochDetector](../omnibci/domain/ports/algorithm.py): `detect_start` / `detect_end` receive a full SignalChunk and return first-marker tuples. | Processing state repeatedly scans chunks; SignalProcessor owns mapping and epoch transitions; the default detector implements both methods. | S1c: one `detect(raw_data)` returning ordered `EpochBoundary` values. Preserve multiple-marker, repeated-start, baseline and pre-trigger behavior. |
| [Paradigm](../omnibci/domain/ports/paradigm.py): separate `bind_trigger`, broad renderer setup/teardown, old trial/result names; required frame/readiness methods absent from the ABC. | Stimulator binds trigger and calls `trial_stimulus_present` / `trial_result_present`; its readiness property uses duck typing. [RenderLoop](../omnibci/infrastructure/stimulation/render_loop.py) calls `render_frame` and teardown. [Stimulation application](../omnibci/infrastructure/stimulation/application.py), dispatch and schemas carry the payloads. | S1d: required frame/readiness methods; one setup with narrow drawing/trigger capabilities; present_trial/present_result payload validation and teardown without renderer argument. Update Classic40 and feedback helper; preserve free-run, cue and feedback behavior. |
| [Renderer](../omnibci/domain/ports/renderer.py): public drawing API includes window/event/frame lifecycle. | Stimulator/RenderLoop own lifecycle; Classic40 and feedback helper draw/upload resources; both renderer adapters implement the concrete operations. | S1d: export `RenderSurface` for drawing; retain internal Renderer and the adapters for the host. No wrapper is required solely to satisfy the narrower protocol. |
| [TriggerEmitter](../omnibci/domain/ports/acquisition.py): send returns a boolean, device lifecycle is not declared. | Stimulator selects/clears the trigger; Paradigm and Classic40 bind/use it. Curry8, Scan45 and `_NoTrigger` implement it. | S1d: explicit setup/teardown and send failure semantics; expose only `TriggerOutput` to the paradigm. Move device access out of construction; preserve selected-driver pulse/clear timing and explicit no-trigger mode. |
| [Effector](../omnibci/domain/ports/effector.py): `play` naming and nonabstract quiescence leave settlement incomplete as a required contract. | [Actuator](../omnibci/infrastructure/actuation/actuator.py) constructs, sets up, plays, quiesces and tears down LogEffector. [Actuation application](../omnibci/infrastructure/actuation/application.py), engine channel/contracts and [translation](../omnibci/infrastructure/neuroengine/actuation_translator.py) retain correlation/settlement. | S1e: `execute(command)` and required quiescence; validate plugin payload without exposing the host envelope. Preserve serialized ownership, latest-pending replacement and uncertain stop outcomes. |

The live Stimulation application calls `Stimulator.start_async()`. The alternate `Stimulator.start()` / `RenderLoop.start()` thread path is still exercised by `tests/stimulation/test_stimulation.py` and `test_render_loop.py`; it is not safe to treat those tests as irrelevant when simplifying execution. S1d must move their observable lifecycle/cleanup coverage to the retained path. See the separate [Stimulation review](stimulation-interface-design.md).

## 5. SDK and construction change checklist

The [current SDK](../omnibci/sdk/__init__.py) exports:

```text
Color, Decoder, Effector, EpochDetector, Filter, FilterContext,
Paradigm, PluginContext, PluginParameters, Prediction, Rect,
Renderer, SignalChunk, SignalSource, SignalInfo, TextureHandle, TriggerEmitter
```

For S1f, remove the public exports `PluginContext`, `PluginParameters`, `FilterContext`, `Renderer` and `SignalChunk`; add `ChannelDescriptor`, `SourceObservation`, `SourceCheckpoint`, `EpochBoundary`, `RenderSurface` and `TriggerOutput`. The other 12 names remain, with the revised contracts. `SignalChunk` and Renderer remain internal. Deleting an export is not permission to delete its host implementation.

Current [PluginLoader.instantiate](../omnibci/infrastructure/plugins/loader.py) requires API `"2"` and exactly `parameters` plus `context`, validates/copies parameters, and injects `resource_root` through a replaced PluginContext. Concrete constructors are less uniform: some allow omitted inputs, Classic40 still accepts a positional trigger, and Scan45Trigger accepts a separate parallel-port argument. Update the actual constructors and their direct tests, not just loader keyword validation.

The common runtime path is Configuration materialization → accepted module snapshot → [configured_bindings / load_configured_plugin](../omnibci/infrastructure/plugins/runtime_configuration.py) → a fresh catalog selection → PluginLoader → fresh role Instance. The S3 artifact handoff must remove the second catalog lookup, while S1f changes the constructor/API contract.

There are **8 production calls to `load_configured_plugin` across 5 files**:

| File | Slots / call paths |
| --- | --- |
| [acquisition/worker.py](../omnibci/infrastructure/acquisition/worker.py) | `acquisition.source` |
| [signal_processing/signal_configuration.py](../omnibci/infrastructure/signal_processing/signal_configuration.py) | `processing.decoder`, `processing.epoch_detector`; delegates filters to `load_filters`. |
| [signal_processing/pipeline_configuration.py](../omnibci/infrastructure/signal_processing/pipeline_configuration.py) | Ordered filter bindings in `load_filters`, and decoder loading for pipeline replacement. |
| [stimulation/stimulator.py](../omnibci/infrastructure/stimulation/stimulator.py) | `stimulation.trigger`, `stimulation.paradigm` |
| [actuation/actuator.py](../omnibci/infrastructure/actuation/actuator.py) | `actuation.effector` |

[Stimulation process management](../omnibci/infrastructure/stimulation/process_manager.py) also resolves the selected paradigm directly to determine its plugin type. Include it in S3's resolution audit; it is not one of the eight construction calls.

All current production PluginContext references are covered by: SDK, loader, runtime_configuration, the two processing configuration files, and the 10 built-in implementation files in section 3. The example decoder is an additional consumer. Source search found **15 production files, 1 example and 14 test files** containing PluginContext; these are file-reference counts including definitions, annotations and fixture source strings, not counts of runtime calls.

The 14 test files are under `tests/infrastructure/`: `test_classic40_short_epochs.py`, `test_decoder_input_runtime.py`, `test_decoder_plugin_example.py`, `test_explicit_acquisition_parameters.py`, `test_explicit_algorithm_parameters.py`, `test_neuroscan_plugins.py`, `test_plugin_api_versions.py`, `test_plugin_contributions.py`, `test_plugin_discovery.py`, `test_plugin_schemas.py`, `test_plugin_sdk.py`, `test_scan45_channels.py`; and under `tests/neuroengine/`: `test_epoch_processor.py`, `test_signal_processing_worker.py`.

FilterContext also appears in `tests/neuroengine/test_baseline_window.py` and `test_processing_pipeline.py`. Renderer occurs in `tests/infrastructure/test_node_trigger_cleanup.py`. Existing SDK tests explicitly assert the old exports, context pickling and optional paradigm renderer argument; replace those expectations alongside the new behavior. Preserve tests for copied parameters, constructor errors not being retried, import isolation and spawned execution. Old API strings may remain in rejection tests.

## 6. Module boundaries and transitive consumers

These are internal application/host interfaces, not additions to the public SDK. They are grouped by owning implementation step to avoid treating every consumer as a new abstraction.

| Boundary | Existing definitions and consumers | Change / preservation requirement |
| --- | --- | --- |
| Plugin identity versus Configuration | [Configuration models](../omnibci/application/configuration/models.py): PluginSelection, PluginProfile, ProfileBinding, ModuleActivation; [catalog port](../omnibci/application/ports/plugin_configuration_catalog.py), [package contracts](../omnibci/application/plugin_packages.py) and [market contracts](../omnibci/application/plugin_market.py). | S2: move shared identity/kinds once to the proposed leaf `plugin_identity.py`; no alias. Profiles, revisions, slots and input preferences remain Configuration-owned. |
| Package lifecycle versus saved configuration | [PluginPackageService](../omnibci/application/services/plugin_package_service.py) reads the configuration repository and owns `propose_profile` / `create_version_profile`; [ConfigurationService](../omnibci/application/configuration/service.py), runtime configuration/engine reference callbacks and [bootstrap](../omnibci/bootstrap.py) compose it. | S2: move profile operations/result to Configuration, inject narrow reference queries, retain shared admission and pending/active/recovery reference checks. |
| Marketplace versus local publication | PluginMarketRepository includes `install_prepared`; [market service](../omnibci/application/services/plugin_market_service.py), [adapter](../omnibci/infrastructure/plugins/market_adapter.py), [repository](../omnibci/infrastructure/plugins/market_repository.py), [installation service](../omnibci/application/services/plugin_installation_service.py), package service/store/installer implement the flow. | S2: Marketplace prepares verified download evidence; Plugin Management owns local commit for market and local imports. Keep proof expiry/identity revalidation, immutable-version conflict and idempotency checks. |
| Manifest admission and eligibility | [Descriptors](../omnibci/infrastructure/plugins/descriptors.py), [catalog](../omnibci/infrastructure/plugins/plugin_catalog.py), [installer](../omnibci/infrastructure/plugins/package_installer.py), market repository, [configuration catalog](../omnibci/infrastructure/plugins/configuration_catalog.py), [package store](../omnibci/infrastructure/plugins/package_store.py), [lifecycle store](../omnibci/infrastructure/plugins/package_lifecycle_store.py), [installed registry](../omnibci/infrastructure/plugins/installed_registry.py). | S1f/S2: current API/domain rules occur in multiple validators. Share static rules, remove package-wide execution-domain restrictions, and distinguish metadata inspection from node-specific enablement. Keep path/archive/schema validation. |
| Preparation versus execution | ProfileBinding codecs, [accepted configuration](../omnibci/infrastructure/plugins/accepted_configuration.py), runtime_configuration, loader, five factories above, SignalProcessor replacement, [engine builder](../omnibci/infrastructure/neuroengine/builder.py). | S3: private exact artifact/environment pins, pending-to-active handoff and code cache per artifact/process. Preserve fresh Instances, import isolation and failed-import cleanup. Do not expose these records in constructors. |
| Engine and application services | [EnginePort / EngineHandle](../omnibci/application/ports/engine_port.py), engine service, acquisition/stimulation/actuation services, [runtime apply](../omnibci/application/services/runtime_apply_service.py), [command results](../omnibci/application/services/command_result_service.py), [ResultsService](../omnibci/application/services/results_service.py). | S4: replace runtime graph access with used operations, explicit command scope and existing receipts. ResultsService needs the generation value, not the whole engine. Preserve compare-and-swap, recovery, history and uncertain outcomes. |
| Node configuration and readiness | [NodeConfiguration / project / materialize](../omnibci/application/node_configuration.py), [node service](../omnibci/application/services/node_configuration_service.py), [node client](../omnibci/infrastructure/plugins/node_client.py), [peer identity](../omnibci/infrastructure/ipc/peer_identity.py), stimulation/actuation process managers, [run_stimulation](../scripts/run_stimulation.py), [run_actuation](../scripts/run_actuation.py). | S4: both spawned and separately started nodes need artifact/environment pins and readiness comparison. Keep local path resolution, authentication, redirect rejection, stale-peer checks and no automatic remote mutation. |
| Three import/author sources | Existing market prepare/install and ZIP upload; mutable source discovery in the catalog; external decoder example. | S5: retain one installation/runtime path, add static validate/package and isolated immutable develop snapshots. Proposed `plugin_cli.py` does not exist yet. No new SDK runtime, compatibility loader or development guide in S0. |
| HTTP/WS and frontend | [Plugin config](../omnibci/presentation/routers/plugin_config.py), [packages](../omnibci/presentation/routers/plugin_packages.py), [lifecycle](../omnibci/presentation/routers/plugin_package_lifecycle.py), [market](../omnibci/presentation/routers/plugin_market.py), [runtime config](../omnibci/presentation/routers/runtime_config.py), acquisition/stimulation routers; corresponding [presentation schemas](../omnibci/presentation/schemas/__init__.py), [OpenAPI](openapi.json), [REST types](../frontend/src/api/rest-types.ts). | S6: change client contracts only where actual payloads change; internal ownership changes alone do not require wire redesign. Update real consumers together. Do not regenerate these artifacts in S0. |
| Embedded guide, UI terminology and packaging | [DecoderDeveloperGuide](../frontend/src/DecoderDeveloperGuide.tsx), [PluginMarket](../frontend/src/PluginMarket.tsx), [App](../frontend/src/App.tsx), [shell](../frontend/src/locales/shell.ts) / [setup](../frontend/src/locales/setup.ts) labels; [wheel check](../.github/scripts/check_installed_wheel.py). | S6: embedded examples also use the old contract; update after implementation. Unify Plugins naming and retain packaged manifests, schemas and stimulus resources, including the check for 11 built-in Contributions. |

The exact **14 production files containing PluginSelection** are Configuration models/service; application plugin_packages/plugin_market; the plugin_configuration_catalog port; plugin_package_service/signal_setup_service; infrastructure configuration_catalog/descriptors/market_repository/package_installer/package_store/plugin_catalog; and the plugin_config router. The current **five production ProfileBinding consumers/definition files** are Configuration models/service, application node_configuration/plugin_packages, and infrastructure runtime_configuration. This includes serializers and presentation imports that would otherwise be easy to miss in a definition move. Searches also found PluginSelection in 25 test files and ProfileBinding in 28 test files; do not interpret those as extra public APIs.

## 7. Regression map and next action

| Change group | Existing regression coverage to retain and adapt when implementing |
| --- | --- |
| Domain/layout | `tests/domain/test_channel_mapping_domain.py`, `test_decoder_input.py`; metadata, decoder-input, epoch, classification/result and IPC assertions in the application/infrastructure/neuroengine suites. |
| Acquisition | `tests/acquisition/`; infrastructure source metadata, acquisition resume/restart/disconnect/fetch failure, explicit parameters, preview and live-source tests; application source checkpoint/expectations and runtime apply integration. |
| Processing | `tests/neuroengine/test_processing_pipeline.py`, `test_pipeline_configuration.py`, `test_processing_state.py`, `test_epoch_processor.py`, `test_epoch_boundaries.py`, `test_baseline_window.py`, channel mapping and spawned-worker tests; infrastructure classic40 short epochs and decoder example. |
| Stimulation/trigger | `tests/stimulation/`; infrastructure node trigger cleanup, stimulation acknowledgement, Neuroscan/Scan45 and output plugin configuration tests. GUI/hardware markers remain distinct from headless lifecycle checks. |
| Actuation | `tests/actuation/`, `tests/neuroengine/test_actuation.py` and module-peer/translation tests. |
| SDK/loader/manifest | Infrastructure plugin SDK, API versions, built-in contributions, contribution/discovery/schema/catalog/runtime configuration and explicit-parameter tests; `tests/plugin_configuration_helpers.py` and generated temporary plugin sources inside tests. |
| Management and sources | Application package composition/service, profile service, lifecycle admission and runtime package ownership; infrastructure installer/store/lifecycle/market tests; related presentation router tests. |
| Engine/configuration/nodes | Application module/current/runtime configuration, runtime apply/recovery and command results; infrastructure accepted configuration, node client/sync, peer identity and quiescence; independent modules and worker readiness tests. |

This inventory keeps the existing simplifications: one API replacement, two constructor shapes, no PluginContext, one local publication owner and existing Module Hosts. Static records, codecs, renderer adapters and tests are adjusted where their behavior requires it; a new compatibility layer or universal lifecycle framework is not a prerequisite.

**S0 is complete.** Final comparison against the 615-file before-state found only the planned update to `docs/plugin-implementation-plan.md`; this report is the only added nonignored file. All pre-existing source, test, configuration, client and other documentation files retain their original content. No Git staging, commit, branch reset or dependency change was performed. Local documentation links and the manifest/implementation/loader-call inventory were checked against actual files.

S1 has not started. The next implementation unit is S1a: establish focused observable failures for the agreed domain validation/layout changes, then update the affected values and consumers without publishing a partly migrated SDK. The rest of S1 remains one coordinated replacement before its gate can pass.
