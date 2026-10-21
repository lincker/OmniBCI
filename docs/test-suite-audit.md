# Test Suite Audit

Date: 2026-09-25. Baseline: `a806aa1` on `feat-module-config`.

This is a historical audit: its counts and validation results describe that
checkpoint. The 2026-10-21 minimal-runtime update removed Session, persistence,
raw recording and archive tests with their production features. Their obsolete
keep decisions are removed below. Current direct-result coverage lives in
processor, drain, results-service, WebSocket and input-mapping tests.

## Scope and criteria

Reviewed every tracked file under `tests`: **147 test modules, five Python support files, and one recording fixture**. Each test module was checked against its current production consumer and neighboring coverage. File names, age, use of mocks, and absence of a direct `assert` were not deletion criteria by themselves.

Remove tests only when they exercise generated language/framework behavior without a project rule, repeat stronger retained coverage, or preserve a compatibility path that production no longer reads. Keep current failure, lifecycle, concurrency, real-time safety, protocol and API contracts. Repair a stale target when its assertion still protects current behavior.

## Changes

- Delete one entire file: `tests/application/test_queries.py`.
- Trim 19 files and correct the live SSVEP test target in one file.
- Remove **39 test functions / 53 collected cases**, including parameterized retired-configuration scenarios.
- Preserve 2257 collected cases across 146 test modules; two existing cases are renamed to describe their current contract.
- Remove two obsolete `active.toml` nonexistence assertions from retained installer and market tests.
- Leave production code and all fixture data unchanged. No new tests or runtime abstractions were added.

## Changed-file decisions and retained coverage

### tests/actuation/test_application.py

**Trim.** Keep command validation, latest-command mailbox, correlation, age gates, heartbeat responsiveness and lifecycle tests; remove one fully duplicated queue-close case.

TestQueueOwnership.test_owned_event_queue_is_closed_once repeats two _close_event_queue calls and close/cancel_join call-count assertions already covered by the top-level test_owned_event_queue_close_is_idempotent_and_non_blocking, which additionally asserts join_thread is not called. Other cases exercise distinct live Actuation paths.

Removed test functions:

- `TestQueueOwnership::test_owned_event_queue_is_closed_once`

### tests/application/test_bootstrap_plugin_configuration.py

**Trim.** Keep active configuration composition and restart tests; delete three families whose only special premise is a removed and unread legacy file format.

build_container reads current plugin profiles/activation through accepted_runtime_settings. Automatic whole-system initialization has been removed: fresh startup leaves modules unconfigured, covered by test_fresh_installation_does_not_select_plugins and test_bootstrap_ignores_retired_plugin_settings. No production reader remains for active.toml or the named application profile overlay. Retained accepted inputs across restart are covered by test_existing_activation_keeps_its_revision_and_pinned_inputs; status source selection by test_status_reads_applied_values_while_engine_running. The three legacy-file families create unrelated valid/missing/corrupt files and duplicate those observable assertions.

Removed test functions:

- `test_existing_activation_ignores_damaged_legacy_files`
- `test_retired_application_overlay_cannot_change_accepted_or_initial_configuration`
- `test_retired_profiles_do_not_affect_initialization`

### tests/application/test_commands.py

**Trim.** Retain the two behaviorally significant default-value contracts; remove generated dataclass behavior checks.

Production command modules contain only dataclass declarations. Eight cases only instantiate, assign constructor fields, compare generated equality, or test frozen setattr. StimulationAppService consumes paradigm=None and n_trials=1/trial_params=None; its default-trial service test asserts only EventType, so those two defaults should remain protected.

Removed test functions:

- `TestAcquisitionCommands::test_commands_are_value_equal`
- `TestAcquisitionCommands::test_connect_is_frozen`
- `TestAcquisitionCommands::test_distinct_command_types_not_equal`
- `TestEngineCommands::test_all_instantiate`
- `TestEngineCommands::test_frozen`
- `TestStimulationCommands::test_start_with_paradigm`
- `TestStimulationCommands::test_stop_instantiates`
- `TestStimulationCommands::test_trial_with_params`

### tests/application/test_engine_port.py

**Trim.** Keep observable port-boundary behavior; remove two historical private-field-name assertions.

The fake EnginePort intentionally lacks event_bus, so successful service dispatch establishes the boundary. Merely asserting _engine_svc is absent prevents an implementation name from returning without testing any forbidden access or observable behavior.

Removed test functions:

- `TestAcquisitionUsesPort::test_no_private_engine_svc_attribute_remains`
- `TestStimulationUsesPort::test_no_private_engine_svc_attribute_remains`

### tests/application/test_engine_service.py

**Trim.** Keep readiness, startup/stop exclusion, ownership, and failure tests; remove obsolete symbol-absence marker.

is_engine_ready gates dispatch and process-handle access; current start/stop code still has explicit startup barriers and retains unconfirmed resources. TestProcessorHandlersRemoved only asserts two removed method names do not exist, adding no behavioral guarantee about worker lifecycle.

Removed test functions:

- `TestProcessorHandlersRemoved::test_handlers_are_gone`

### tests/application/test_queries.py

**Delete.** Delete tests that exercise only generated dataclass construction, equality, defaults and assigned fields.

GetEngineStatusQuery, GetAcquisitionStatusQuery, EngineStatusResult and AcquisitionStatusResult are plain dataclasses without custom logic. test_query_is_frozen only compares equality and does not test immutability. Actual status construction/projection is exercised by engine/acquisition service tests and presentation contracts.

Removed test functions:

- `TestAcquisitionQueries::test_query_equals`
- `TestAcquisitionQueries::test_result_connected_streaming`
- `TestAcquisitionQueries::test_result_with_source_info`
- `TestEngineQueries::test_query_is_frozen`
- `TestEngineQueries::test_result_defaults`
- `TestEngineQueries::test_result_with_extra`

### tests/conf/test_settings.py

**Trim.** Keep explicit-file mode and worker snapshots; simplify the current retired-section filtering contract and delete a redundant old-overlay absence case.

OmniBCISettings._flatten explicitly excludes top-level META/PROFILES, which remains custom production behavior worth checking. It does not read profiles/old-profile.toml, so creating that obsolete file and asserting its bytes unchanged has no unique value. The first test already verifies all retained setting values and that PROFILES.active is absent; the second missing-overlay case covers no separate production branch.

Removed test functions:

- `test_missing_retired_profile_is_not_loaded`

Renamed `test_retired_application_profile_cannot_override_settings` to `test_retired_profile_section_is_excluded_from_settings`; current behavior assertions remain.

### tests/infrastructure/test_ipc_network.py

**Trim.** Retain real socket safety/lifecycle tests; remove two codec assertions duplicated by the contract suite.

TestFrameCodec.test_header_size_is_eight only compares a constant; test_ipc_contracts.TestFraming pins the actual eight wire bytes and rejects non-eight-byte headers. test_frame_public_fields_are_read_only is a strict subset of test_frame_and_header_cannot_be_mutated_out_of_sync there.

Removed test functions:

- `TestFrameCodec::test_frame_public_fields_are_read_only`
- `TestFrameCodec::test_header_size_is_eight`

### tests/infrastructure/test_market_repository.py

**Trim.** Keep signature/cache/transport security coverage; remove the obsolete active.toml assertion.

StaticMarketRepository is live and verifies Ed25519 signatures, repository identity, expiry and bounded downloads. test_install_downloads_verified_archive_without_executing_or_enabling checks installer.root/active.toml, a retired filename in the wrong storage domain; actual enablement is tested in package lifecycle and market adapter suites.

### tests/infrastructure/test_package_installer.py

**Trim.** Keep immutable archive validation/security coverage; remove the retired active.toml assertion.

PluginPackageInstaller performs static bounded extraction, identity/dependency checks and atomic publication. test_valid_archive_is_static_catalog_readable_and_not_activated checks root/active.toml, which production no longer uses; activation/enablement isolation is already verified against actual stores by package_store/lifecycle tests.

### tests/infrastructure/test_plugin_profile_repository.py

**Trim.** Keep persistence/CAS/cross-process locking; remove the retired old-active-file preservation test.

TomlPluginProfileRepository only reads plugin-profiles/*.toml and activation.toml; no production active.toml references remain. test_storage_preserves_legacy_files merely creates an unrelated legacy file then proves writes to other paths do not change it.

Removed test functions:

- `test_storage_preserves_legacy_files`

### tests/presentation/test_acquisition_router.py

**Trim.** Keep acquisition status and command HTTP contracts; remove the weaker status-fields duplicate.

test_initial_status already accesses and checks both fields. test_openapi_contract validates the published schema. The separate field-presence test adds no behavior.

Removed test functions:

- `TestAcquisitionStatus::test_status_schema_fields`

### tests/presentation/test_config_router.py

**Trim.** Keep current host settings API, strict field validation and apply boundaries; remove six retired application-profile route cases.

The obsolete routes have no production or frontend consumer. test_openapi_contract asserts the complete current route set. Keep rejected retired PATCH fields because they exercise current input validation and prevent partial writes.

Removed test functions:

- `test_application_profile_routes_are_removed`

### tests/presentation/test_configuration_startup.py

**Trim.** Keep real application lifespan initialization, repair and logging ownership; simplify the fresh-start test.

Rename the retired-selection test to test_fresh_lifespan_initializes_configuration_without_starting_engine. Remove only the unread active.toml setup and its nonmutation assertion. Keep initial plugin parameters, management availability, accepted revision, no worker construction and unchanged host settings assertions.

Renamed `test_dangling_retired_acquisition_selection_does_not_block_lifespan` to `test_fresh_lifespan_initializes_configuration_without_starting_engine`; current behavior assertions remain.

### tests/presentation/test_engine_router.py

**Trim.** Keep engine start, stop, status and HTTP errors; remove a mock-bypassed duplicate and two retired processor-route cases.

test_start_conflict_when_already_running replaces the entire service handler, so setting _running does not test idempotence. Service-level test_start_twice_is_idempotent remains. Exact OpenAPI route coverage replaces assertions for the deleted processor endpoints.

Removed test functions:

- `TestEngineProcessorEndpointsRemoved::test_processor_start_is_gone`
- `TestEngineProcessorEndpointsRemoved::test_processor_stop_is_gone`
- `TestEngineStart::test_start_conflict_when_already_running`

### tests/presentation/test_error_envelope.py

**Trim.** Keep semantic HTTP error codes, error serialization and validation mapping; remove an enum-literal check and duplicate success envelope.

test_openapi_contract checks the complete published error enum; real endpoint tests check the success envelope.

Removed test functions:

- `TestErrorResponseModel::test_error_code_is_string_value`
- `TestUnifiedErrorThroughApp::test_success_envelope_still_intact`

### tests/presentation/test_event_hook_wiring.py

**Trim.** Keep actual event subscriptions and WebSocket dispatch; remove a check for an obsolete private attribute.

The real lifespan registration test observes current listener wiring. Checking that _register_event_hooks is absent on app.state only preserves a past implementation detail.

Removed test functions:

- `test_no_dangling_unused_hook_on_app_state`

### tests/presentation/test_websocket_results.py

**Trim.** Keep wire validation, heartbeat, broadcast and real WebSocket delivery; remove two weaker duplicates.

test_serialises_all_classification_fields covers the two fields in the deleted JSON-roundtrip test. test_receives_trial_completed establishes the same connection and additionally verifies the delivered message.

Removed test functions:

- `TestTrialCompletedMessage::test_json_roundtrip`
- `TestWebSocketEndpoint::test_connects_successfully`

### tests/stimulation/test_render_loop.py

**Trim.** Keep frame execution, timing argument, liveness and cleanup tests; remove one assertion-free duplicate smoke case.

TestRenderLoopLifecycle.test_start_spawns_thread creates _make_loop(0), starts, then stops with timeout=1.0 and has no assertion. TestRenderLoopLifecycle.test_is_running_false_after_stop executes the identical path and additionally asserts not loop.is_running. The remaining tests protect current thread-mode RenderLoop behavior.

Removed test functions:

- `TestRenderLoopLifecycle::test_start_spawns_thread`

### tests/stimulation/test_ssvep_config.py

**Fix.** Keep the SSVEP creator invariants and brightness contracts, but point brightness tests at the currently used class.

SSVEPConfig is consumed by creator/make_pics.py. TestSSVEPBrightnessContract currently calls StimTargetRect, whose only repository consumer is this test. UIFactory._set_stim_targets instead constructs SSVEPRectView; each class has an independent cal_brightness implementation. Replace the test import/helper with SSVEPRectView(width=140,height=140,position_x=0,position_y=0,refresh_rate=fs,frequency=frequency,start_phase=start_phase), retaining all three existing mathematical assertions. Applied this test-only target update; venv/bin/python -m pytest tests/stimulation/test_ssvep_config.py -q passed all 13 tests. Production code was unchanged.

## Complete file inventory

Test counts below are baseline function counts, before parameter expansion. `Keep` means the file remains necessary; `Trim` means its useful coverage remains after the listed removals; `Fix` changes a stale test target.

### tests/acquisition

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/acquisition/test_acquisition_filereader.py](../tests/acquisition/test_acquisition_filereader.py) | Keep | 1 | Keep the real CNT-fixture integration test; it validates supported file replay behavior beyond mocked adapters. |
| [tests/acquisition/test_acquisition_worker.py](../tests/acquisition/test_acquisition_worker.py) | Keep | 3 | Keep worker orchestration and pinned plugin-loading boundary coverage. |

### tests/actuation

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/actuation/__init__.py](../tests/actuation/__init__.py) | Keep | 0 | Keep the empty test-package marker; it has no obsolete behavior and supports a stable import namespace for spawn-based tests. |
| [tests/actuation/test_actuator.py](../tests/actuation/test_actuator.py) | Keep | 10 | Keep actual user-plugin loading and device resource ownership contracts. |
| [tests/actuation/test_application.py](../tests/actuation/test_application.py) | Trim | 29 | Keep command validation, latest-command mailbox, correlation, age gates, heartbeat responsiveness and lifecycle tests; remove one fully duplicated queue-close case. |
| [tests/actuation/test_contracts.py](../tests/actuation/test_contracts.py) | Keep | 6 | Keep validation at the IPC-to-device trust boundary. |
| [tests/actuation/test_engine_channel.py](../tests/actuation/test_engine_channel.py) | Keep | 3 | Keep the bidirectional route-mirror contract. |
| [tests/actuation/test_process_manager.py](../tests/actuation/test_process_manager.py) | Keep | 19 | Keep managed subprocess lifecycle and engine-service integration coverage. |
| [tests/actuation/test_settle_actuation.py](../tests/actuation/test_settle_actuation.py) | Keep | 3 | Keep physical-output settlement and nonblocking event dispatch regressions. |

### tests/application

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/application/__init__.py](../tests/application/__init__.py) | Keep | 0 | Keep the test package marker required by cross-file test helper and fixture imports. |
| [tests/application/test_acquisition_service.py](../tests/application/test_acquisition_service.py) | Keep | 12 | Command dispatch and authoritative worker-state projection are current application behavior. |
| [tests/application/test_bootstrap.py](../tests/application/test_bootstrap.py) | Keep | 8 | Composition, subscriptions, and repository selection remain necessary. |
| [tests/application/test_bootstrap_plugin_configuration.py](../tests/application/test_bootstrap_plugin_configuration.py) | Trim | 13 | Keep active configuration composition and restart tests; delete three families whose only special premise is a removed and unread legacy file format. |
| [tests/application/test_command_result_service.py](../tests/application/test_command_result_service.py) | Keep | 7 | Command correlation, terminal outcomes, and concurrency directly protect runtime command correctness. |
| [tests/application/test_commands.py](../tests/application/test_commands.py) | Trim | 10 | Retain the two behaviorally significant default-value contracts; remove generated dataclass behavior checks. |
| [tests/application/test_config_lifecycle.py](../tests/application/test_config_lifecycle.py) | Keep | 4 | Keep exclusion-gate concurrency and bootstrap-selected-source integration coverage. |
| [tests/application/test_config_service.py](../tests/application/test_config_service.py) | Keep | 11 | Host-setting whitelist, numeric validation, saved/applied separation, and revision conflicts remain active. |
| [tests/application/test_engine_port.py](../tests/application/test_engine_port.py) | Trim | 9 | Keep observable port-boundary behavior; remove two historical private-field-name assertions. |
| [tests/application/test_engine_restart.py](../tests/application/test_engine_restart.py) | Keep | 5 | Fresh process graphs and subscription replay remain required after stop. |
| [tests/application/test_engine_service.py](../tests/application/test_engine_service.py) | Trim | 22 | Keep readiness, startup/stop exclusion, ownership, and failure tests; remove obsolete symbol-absence marker. |
| `tests/application/test_legacy_plugin_config.py` | Removed | 10 | Removed with the obsolete host-settings-to-plugin parameter mapper. |
| [tests/application/test_node_configuration.py](../tests/application/test_node_configuration.py) | Keep | 13 | Role-scoped projection and validation are the newly accepted standalone-node contract. |
| [tests/application/test_node_configuration_service.py](../tests/application/test_node_configuration_service.py) | Keep | 4 | Node publication must follow actual runtime ownership and reject unsettled transitions. |
| `tests/application/test_plugin_config_initialization.py` | Removed | 13 | Removed with automatic whole-system profile initialization; module selections are explicit. |
| [tests/application/test_plugin_lifecycle_admission.py](../tests/application/test_plugin_lifecycle_admission.py) | Keep | 4 | Package operations must not race profile validation and publication. |
| [tests/application/test_plugin_package_composition.py](../tests/application/test_plugin_package_composition.py) | Keep | 6 | Keep actual bootstrap/lifespan package and market integration tests. |
| [tests/application/test_plugin_package_service.py](../tests/application/test_plugin_package_service.py) | Keep | 10 | Reference-aware removal and explicit version migration are still active package lifecycle behavior. |
| [tests/application/test_plugin_profile_service.py](../tests/application/test_plugin_profile_service.py) | Keep | 30 | Core Profile/Activation semantics, validation, and host/runtime ownership remain necessary. |
| [tests/application/test_plugin_profiles.py](../tests/application/test_plugin_profiles.py) | Keep | 7 | Custom immutable snapshots, semantic hashing, and input validation need direct value-object tests. |
| [tests/application/test_plugin_schema.py](../tests/application/test_plugin_schema.py) | Keep | 16 | The project implements its own constrained schema validator; its edge-case tests are necessary. |
| `tests/application/test_queries.py` (deleted) | Delete | 6 | Delete tests that exercise only generated dataclass construction, equality, defaults and assigned fields. |
| [tests/application/test_runtime_admission.py](../tests/application/test_runtime_admission.py) | Keep | 3 | Nested command exclusion and context propagation are concurrency correctness requirements. |
| [tests/application/test_runtime_apply_failures.py](../tests/application/test_runtime_apply_failures.py) | Keep | 12 | Retain fault-injection coverage for transaction, checkpoint, generation and output-safety boundaries. |
| [tests/application/test_runtime_apply_integration.py](../tests/application/test_runtime_apply_integration.py) | Keep | 1 | Keep real-process evidence for live rebuild, narrow updates and checkpoint rollback. |
| [tests/application/test_runtime_configuration_service.py](../tests/application/test_runtime_configuration_service.py) | Keep | 28 | Planning and operation lifecycle invariants remain active and nonredundant. |
| [tests/application/test_runtime_narrow_apply.py](../tests/application/test_runtime_narrow_apply.py) | Keep | 19 | Preserving ongoing acquisition and processing while reconciling partial updates requires this coverage. |
| [tests/application/test_runtime_package_ownership.py](../tests/application/test_runtime_package_ownership.py) | Keep | 4 | Historical applied configuration must not be confused with retained runtime resources. |
| [tests/application/test_runtime_recovery_status.py](../tests/application/test_runtime_recovery_status.py) | Keep | 3 | Current quarantine and historical failed operation records must remain distinct. |
| [tests/application/test_runtime_snapshot.py](../tests/application/test_runtime_snapshot.py) | Keep | 6 | Late-started managed children and restarts must use one immutable applied snapshot. |
| [tests/application/test_source_checkpoint.py](../tests/application/test_source_checkpoint.py) | Keep | 6 | Resource identity determines safe restore versus new timeline behavior. |
| [tests/application/test_stimulation_service.py](../tests/application/test_stimulation_service.py) | Keep | 13 | Stimulation dispatch, subprocess cleanup and external-node uncertainty remain current. |

### tests/conf

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/conf/test_host_settings_storage.py](../tests/conf/test_host_settings_storage.py) | Keep | 21 | Keep portable override storage, file-CAS and worker realtime-isolation tests. |
| [tests/conf/test_paths.py](../tests/conf/test_paths.py) | Keep | 2 | Portable relative and absolute path resolution is a small but actual custom policy. |
| [tests/conf/test_settings.py](../tests/conf/test_settings.py) | Trim | 7 | Keep explicit-file mode and worker snapshots; simplify the current retired-section filtering contract and delete a redundant old-overlay absence case. |

### tests

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/conftest.py](../tests/conftest.py) | Keep | 0 | Shared service doubles, API fixtures and isolated configuration/log directories are required by the suite. |

### tests/domain

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |

### tests/fixture

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/fixture/train.cnt](../tests/fixture/train.cnt) | Keep | Fixture | Real EEG recording fixture still used by file-reader and direct-processing end-to-end tests. |

### tests/infrastructure

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/infrastructure/__init__.py](../tests/infrastructure/__init__.py) | Keep | 0 | Package marker required by explicit cross-test imports. |
| [tests/infrastructure/test_accepted_configuration.py](../tests/infrastructure/test_accepted_configuration.py) | Keep | 8 | Accepted activation resolution remains the backend startup boundary; missing/corrupt selections must fail without writes. |
| [tests/infrastructure/test_acquisition.py](../tests/infrastructure/test_acquisition.py) | Keep | 21 | Covers live acquisition command dispatch, correlated outcomes, producer shutdown and state projection. |
| [tests/infrastructure/test_acquisition_fetch_failure.py](../tests/infrastructure/test_acquisition_fetch_failure.py) | Keep | 2 | Protects continuity guarantees after dropped data or reader/preprocessor failures. |
| [tests/infrastructure/test_acquisition_resume_capability.py](../tests/infrastructure/test_acquisition_resume_capability.py) | Keep | 4 | Separates safe live-source resumption from replay checkpoints. |
| [tests/infrastructure/test_application_cleanup.py](../tests/infrastructure/test_application_cleanup.py) | Keep | 5 | Tests shared IPC ownership release and cancellation/error aggregation. |
| [tests/infrastructure/test_backend_logging.py](../tests/infrastructure/test_backend_logging.py) | Keep | 15 | Protects the nonblocking logging contract and bounded resource ownership. |
| [tests/infrastructure/test_builtin_contributions.py](../tests/infrastructure/test_builtin_contributions.py) | Keep | 6 | Verifies shipped manifests correspond to loadable domain ports and packaged resources. |
| [tests/infrastructure/test_child_runtime_snapshot.py](../tests/infrastructure/test_child_runtime_snapshot.py) | Keep | 5 | Protects copied configuration handoff to managed child processes. |
| [tests/infrastructure/test_command_confirmation_integration.py](../tests/infrastructure/test_command_confirmation_integration.py) | Keep | 1 | Provides real queue/EventBus/broker round-trip coverage. |
| [tests/infrastructure/test_config_paths.py](../tests/infrastructure/test_config_paths.py) | Keep | 5 | Tests cross-platform configuration-root selection without creating user data. |
| [tests/infrastructure/test_event_bus_pickle.py](../tests/infrastructure/test_event_bus_pickle.py) | Keep | 12 | Covers spawn-safe serialization and EventBus consumer lifecycle. |
| [tests/infrastructure/test_explicit_acquisition_parameters.py](../tests/infrastructure/test_explicit_acquisition_parameters.py) | Keep | 7 | Tests constructor configuration isolation and explicit parameter requirements. |
| [tests/infrastructure/test_explicit_algorithm_parameters.py](../tests/infrastructure/test_explicit_algorithm_parameters.py) | Keep | 8 | Tests algorithm instances consume supplied parameters and context independently. |
| [tests/infrastructure/test_file_reader.py](../tests/infrastructure/test_file_reader.py) | Keep | 10 | Baseline replay source lifecycle remains necessary alongside checkpoint tests. |
| [tests/infrastructure/test_file_reader_checkpoint.py](../tests/infrastructure/test_file_reader_checkpoint.py) | Keep | 8 | Protects exactly-once replay resumption and checkpoint validation. |
| [tests/infrastructure/test_ipc_contracts.py](../tests/infrastructure/test_ipc_contracts.py) | Keep | 17 | Authoritative low-level wire shape and malformed input coverage. |
| [tests/infrastructure/test_ipc_network.py](../tests/infrastructure/test_ipc_network.py) | Trim | 35 | Retain real socket safety/lifecycle tests; remove two codec assertions duplicated by the contract suite. |
| [tests/infrastructure/test_live_source_resume.py](../tests/infrastructure/test_live_source_resume.py) | Keep | 4 | Protects live packet framing across cancellation and reconnection. |
| [tests/infrastructure/test_market_adapter.py](../tests/infrastructure/test_market_adapter.py) | Keep | 8 | Covers trust configuration and integration with runtime admission and package publication. |
| [tests/infrastructure/test_market_repository.py](../tests/infrastructure/test_market_repository.py) | Trim | 26 | Keep signature/cache/transport security coverage; remove the obsolete active.toml assertion. |
| [tests/infrastructure/test_neuroscan_curry8.py](../tests/infrastructure/test_neuroscan_curry8.py) | Keep | 4 | Vendor protocol conformance and real stream integration. |
| [tests/infrastructure/test_neuroscan_plugins.py](../tests/infrastructure/test_neuroscan_plugins.py) | Keep | 4 | Covers built-in catalog-to-driver construction and trigger address handling. |
| [tests/infrastructure/test_node_application_lifecycle.py](../tests/infrastructure/test_node_application_lifecycle.py) | Keep | 10 | Current external-node readiness and safe reconnect boundary. |
| [tests/infrastructure/test_node_client.py](../tests/infrastructure/test_node_client.py) | Keep | 11 | Current standalone HTTP startup/reconnection supervisor contract. |
| [tests/infrastructure/test_node_configuration_sync.py](../tests/infrastructure/test_node_configuration_sync.py) | Keep | 1 | Cross-layer integration for applied snapshot publication and generation admission. |
| [tests/infrastructure/test_node_trigger_cleanup.py](../tests/infrastructure/test_node_trigger_cleanup.py) | Keep | 5 | Protects real trigger release ordering before external stimulation reconnect. |
| [tests/infrastructure/test_outbound.py](../tests/infrastructure/test_outbound.py) | Keep | 19 | Tests actual event projection and JSON wire data used by sockets and WebSocket clients. |
| [tests/infrastructure/test_output_plugin_configuration.py](../tests/infrastructure/test_output_plugin_configuration.py) | Keep | 2 | Ensures output applications use pinned plugin inputs without global settings fallback. |
| [tests/infrastructure/test_package_installer.py](../tests/infrastructure/test_package_installer.py) | Trim | 30 | Keep immutable archive validation/security coverage; remove the retired active.toml assertion. |
| [tests/infrastructure/test_package_lifecycle_store.py](../tests/infrastructure/test_package_lifecycle_store.py) | Keep | 11 | Tests actual install-enable-disable-remove state and exact catalog publication. |
| [tests/infrastructure/test_package_store.py](../tests/infrastructure/test_package_store.py) | Keep | 6 | Tests disabled inventory distinct from selectable contributions. |
| [tests/infrastructure/test_peer_identity_handshake.py](../tests/infrastructure/test_peer_identity_handshake.py) | Keep | 14 | Tests admission before any external socket participation. |
| [tests/infrastructure/test_peer_quiescence_receipts.py](../tests/infrastructure/test_peer_quiescence_receipts.py) | Keep | 1 | Preserves rich pause/settle evidence over the JSON boundary. |
| [tests/infrastructure/test_peer_startup_identity.py](../tests/infrastructure/test_peer_startup_identity.py) | Keep | 2 | Tests managed process entrypoints and managers propagate and enforce identity. |
| [tests/infrastructure/test_peer_transport.py](../tests/infrastructure/test_peer_transport.py) | Keep | 10 | Tests adapter-level lifecycle/ownership independently of socket internals. |
| [tests/infrastructure/test_plugin_api_versions.py](../tests/infrastructure/test_plugin_api_versions.py) | Keep | 3 | Guards explicit API-2 construction and no retry after plugin code fails. |
| [tests/infrastructure/test_plugin_catalog.py](../tests/infrastructure/test_plugin_catalog.py) | Keep | 1 | Small but unique public PluginInfo projection smoke test for effectors. |
| [tests/infrastructure/test_plugin_contributions.py](../tests/infrastructure/test_plugin_contributions.py) | Keep | 16 | Covers package/contribution identity, exact versioning and catalog ambiguity. |
| [tests/infrastructure/test_plugin_discovery.py](../tests/infrastructure/test_plugin_discovery.py) | Keep | 25 | Covers static discovery traversal and safe selected-entrypoint loading. |
| [tests/infrastructure/test_plugin_profile_repository.py](../tests/infrastructure/test_plugin_profile_repository.py) | Trim | 14 | Keep persistence/CAS/cross-process locking; remove the retired old-active-file preservation test. |
| [tests/infrastructure/test_plugin_schemas.py](../tests/infrastructure/test_plugin_schemas.py) | Keep | 5 | Covers static package schema declarations and pre-import validation. |
| [tests/infrastructure/test_plugin_sdk.py](../tests/infrastructure/test_plugin_sdk.py) | Keep | 7 | Public plugin type identity/import isolation and spawn compatibility. |
| [tests/infrastructure/test_process_lifecycle.py](../tests/infrastructure/test_process_lifecycle.py) | Keep | 10 | Generic child readiness protocol validation. |
| [tests/infrastructure/test_runtime_configuration.py](../tests/infrastructure/test_runtime_configuration.py) | Keep | 6 | Current runtime selection must reject alias fallback, duplicates and schema mismatches. |
| [tests/infrastructure/test_scan45_channels.py](../tests/infrastructure/test_scan45_channels.py) | Keep | 7 | Protects configurable channel count and event decoding across packet widths. |
| [tests/infrastructure/test_stimulation_ack.py](../tests/infrastructure/test_stimulation_ack.py) | Keep | 7 | Tests renderer command acknowledgements and correlated failures. |
| [tests/infrastructure/test_stream_drain.py](../tests/infrastructure/test_stream_drain.py) | Keep | 16 | Protects ordered producer/processor draining and processing completion evidence. |
| [tests/infrastructure/test_trial_revision_wire.py](../tests/infrastructure/test_trial_revision_wire.py) | Keep | 1 | Unique trial CONFIG.revision propagation across WebSocket schema and peer payload. |

### tests/neuroengine

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/neuroengine/test_actuation.py](../tests/neuroengine/test_actuation.py) | Keep | 12 | Keep trial-to-actuation translation, sequence metadata, feedback accounting and opt-in wiring. |
| [tests/neuroengine/test_baseline_window.py](../tests/neuroengine/test_baseline_window.py) | Keep | 20 | Keep baseline/window splitting, immutable epoch ownership and preprocessing semantics. |
| [tests/neuroengine/test_config_snapshot.py](../tests/neuroengine/test_config_snapshot.py) | Keep | 3 | Keep snapshot isolation and real spawn-boundary installation coverage. |
| [tests/neuroengine/test_epoch_processor.py](../tests/neuroengine/test_epoch_processor.py) | Keep | 13 | Keep epoch accumulation, decoding readiness and domain result wrapping. |
| [tests/neuroengine/test_neuro_engine.py](../tests/neuroengine/test_neuro_engine.py) | Keep | 14 | Keep orchestration, command routing, thread startup and complete resource cleanup. |
| [tests/neuroengine/test_neuro_engine_builder.py](../tests/neuroengine/test_neuro_engine_builder.py) | Keep | 13 | Keep construction and dependency-wiring coverage. |
| [tests/neuroengine/test_neuro_engine_config.py](../tests/neuroengine/test_neuro_engine_config.py) | Keep | 11 | Keep explicit configuration mapping, defaults and debug/sample-rate injection. |
| [tests/neuroengine/test_neuroengine_processor.py](../tests/neuroengine/test_neuroengine_processor.py) | Keep | 4 | Keep bounded event-driven queue waiting and prompt delivery checks. |
| [tests/neuroengine/test_node_listener_identity.py](../tests/neuroengine/test_node_listener_identity.py) | Keep | 5 | Keep external-node identity wiring from the latest implementation. |
| [tests/neuroengine/test_peer_gateway.py](../tests/neuroengine/test_peer_gateway.py) | Keep | 25 | Keep routing, readiness and transactional gateway/channel lifecycle coverage. |
| [tests/neuroengine/test_peer_safety.py](../tests/neuroengine/test_peer_safety.py) | Keep | 9 | Keep deterministic connection/heartbeat watchdog and failsafe tests. |
| [tests/neuroengine/test_pipeline_configuration.py](../tests/neuroengine/test_pipeline_configuration.py) | Keep | 6 | Keep actual candidate validation and complete replacement pipeline construction. |
| [tests/neuroengine/test_processing_configuration_boundary.py](../tests/neuroengine/test_processing_configuration_boundary.py) | Keep | 11 | Keep runtime configuration state-machine regressions at epoch boundaries. |
| [tests/neuroengine/test_processing_configuration_spawn.py](../tests/neuroengine/test_processing_configuration_spawn.py) | Keep | 1 | Keep the real multiprocess integration test complementing in-process boundary tests. |
| [tests/neuroengine/test_processing_output_context.py](../tests/neuroengine/test_processing_output_context.py) | Keep | 6 | Keep output-only metadata update contracts distinct from pipeline replacement. |
| [tests/neuroengine/test_processing_pipeline.py](../tests/neuroengine/test_processing_pipeline.py) | Keep | 4 | Keep the filter/decoder composition contract and FilterContext propagation. |
| [tests/neuroengine/test_processing_state.py](../tests/neuroengine/test_processing_state.py) | Keep | 14 | Keep granular signal-state transitions and domain-event publication behavior. |
| [tests/neuroengine/test_signal_processing_worker.py](../tests/neuroengine/test_signal_processing_worker.py) | Keep | 6 | Keep pinned plugin selection, actual construction and processor wiring. |
| [tests/neuroengine/test_stimulation_feedback.py](../tests/neuroengine/test_stimulation_feedback.py) | Keep | 6 | Keep inbound stimulation outcome accounting and engine ownership coverage. |
| [tests/neuroengine/test_worker_readiness.py](../tests/neuroengine/test_worker_readiness.py) | Keep | 9 | Keep configured worker startup barriers and real-process rollback coverage. |

### tests/presentation

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/presentation/__init__.py](../tests/presentation/__init__.py) | Keep | 0 | Package marker supports unambiguous imports and cross-file test helpers. |
| [tests/presentation/test_acquisition_router.py](../tests/presentation/test_acquisition_router.py) | Trim | 14 | Keep acquisition status and command HTTP contracts; remove the weaker status-fields duplicate. |
| [tests/presentation/test_config_router.py](../tests/presentation/test_config_router.py) | Trim | 10 | Keep current host settings API, strict field validation and apply boundaries; remove six retired application-profile route cases. |
| [tests/presentation/test_configuration_startup.py](../tests/presentation/test_configuration_startup.py) | Trim | 11 | Keep real application lifespan initialization, repair and logging ownership; simplify the fresh-start test. |
| [tests/presentation/test_engine_router.py](../tests/presentation/test_engine_router.py) | Trim | 9 | Keep engine start, stop, status and HTTP errors; remove a mock-bypassed duplicate and two retired processor-route cases. |
| [tests/presentation/test_error_envelope.py](../tests/presentation/test_error_envelope.py) | Trim | 9 | Keep semantic HTTP error codes, error serialization and validation mapping; remove an enum-literal check and duplicate success envelope. |
| [tests/presentation/test_event_hook_wiring.py](../tests/presentation/test_event_hook_wiring.py) | Trim | 2 | Keep actual event subscriptions and WebSocket dispatch; remove a check for an obsolete private attribute. |
| [tests/presentation/test_health.py](../tests/presentation/test_health.py) | Keep | 2 | Checks real HTTP health and OpenAPI serving, including middleware and mounted endpoint behavior. |
| [tests/presentation/test_node_configuration_router.py](../tests/presentation/test_node_configuration_router.py) | Keep | 2 | Checks settled role snapshots, version identity, private role data and rejection during unsafe transitions. |
| [tests/presentation/test_openapi_contract.py](../tests/presentation/test_openapi_contract.py) | Keep | 12 | Canonical contract for the full public route set, schemas and semantic error enums. |
| [tests/presentation/test_plugin_config_router.py](../tests/presentation/test_plugin_config_router.py) | Keep | 10 | Covers profile CRUD and revision conflicts, schemas, plan consistency, explicit application and startup-setting restrictions. |
| [tests/presentation/test_plugin_market_router.py](../tests/presentation/test_plugin_market_router.py) | Keep | 5 | Covers signed-catalog and install HTTP contracts, rejected trust overrides and nonblocking repository calls. |
| [tests/presentation/test_plugin_package_lifecycle_router.py](../tests/presentation/test_plugin_package_lifecycle_router.py) | Keep | 4 | Covers package enablement, reference protection, removal and version-copy conflict handling. |
| [tests/presentation/test_plugin_packages_router.py](../tests/presentation/test_plugin_packages_router.py) | Keep | 6 | Covers bounded ZIP upload, chunk limits, content validation and admission before installation. |
| [tests/presentation/test_plugins_router.py](../tests/presentation/test_plugins_router.py) | Keep | 3 | Covers all contribution categories, manifest metadata and unknown-category validation. |
| [tests/presentation/test_runtime_config_router.py](../tests/presentation/test_runtime_config_router.py) | Keep | 1 | Covers runtime configuration request and response schemas, conflicts, unavailable state and unknown fields. |
| [tests/presentation/test_stimulation_router.py](../tests/presentation/test_stimulation_router.py) | Keep | 15 | Checks paradigm metadata, stimulation and trial HTTP commands, payload dispatch and renderer failures. |
| [tests/presentation/test_websocket_results.py](../tests/presentation/test_websocket_results.py) | Trim | 21 | Keep wire validation, heartbeat, broadcast and real WebSocket delivery; remove two weaker duplicates. |

### tests/stimulation

| File | Decision | Baseline tests | Reason |
| --- | --- | ---: | --- |
| [tests/stimulation/test_application_lifecycle.py](../tests/stimulation/test_application_lifecycle.py) | Keep | 10 | Keep renderer/IPC lifecycle and startup failure regressions. |
| [tests/stimulation/test_feedback_display.py](../tests/stimulation/test_feedback_display.py) | Keep | 15 | Keep feedback widget GPU-resource and mutation contracts. |
| [tests/stimulation/test_frame_pack.py](../tests/stimulation/test_frame_pack.py) | Keep | 1 | Keep the actual packaged asset integration test. |
| [tests/stimulation/test_pause_stimulation.py](../tests/stimulation/test_pause_stimulation.py) | Keep | 3 | Keep truthful renderer-pause and trigger-clearing acknowledgements. |
| [tests/stimulation/test_process_manager.py](../tests/stimulation/test_process_manager.py) | Keep | 12 | Keep managed stimulation process lifecycle tests. |
| [tests/stimulation/test_render_loop.py](../tests/stimulation/test_render_loop.py) | Trim | 10 | Keep frame execution, timing argument, liveness and cleanup tests; remove one assertion-free duplicate smoke case. |
| [tests/stimulation/test_ssvep_config.py](../tests/stimulation/test_ssvep_config.py) | Fix | 13 | Keep the SSVEP creator invariants and brightness contracts, but point brightness tests at the currently used class. |
| [tests/stimulation/test_stimulation.py](../tests/stimulation/test_stimulation.py) | Keep | 14 | Keep Stimulator orchestration and renderer-selection coverage. |
| [tests/stimulation/test_stimulator_classic40SSVEP.py](../tests/stimulation/test_stimulator_classic40SSVEP.py) | Keep | 36 | Keep the current Classic40SSVEP_2 plugin state machine and asset lifecycle. |

## Deliberately retained

- Legacy plugin aliases and manifest v1 tests: these names still correspond to production-supported initialization and catalog paths.
- Real-time and distributed-node tests: role validation, identity, reconnection, quiescence, stream draining, readiness and failure cleanup protect current runtime behavior.
- Package markers and shared fixtures: retain package imports, shared helpers and isolation of user configuration/logging state.
- `tests/fixture/train.cnt`: still consumed by real file-reader and end-to-end tests with explicit playback configuration.

## Validation

- Baseline collection: **2310** cases. After cleanup: **2257** cases. Every removed node is accounted for above, excluding the two documented renames.
- SSVEP target correction: `venv/bin/python -m pytest tests/stimulation/test_ssvep_config.py -q` — **13 passed**.
- Full suite: **2257 passed, zero failed or skipped in 490.45 seconds**. Command: `venv/bin/python -m pytest -o addopts=--strict-markers -q`. Local TCP and multiprocessing tests ran with host loopback access.
- `git diff --check`: passed.
- Independent review of the cleanup diff found no deleted helper consumers or unique current behavior lost.

## Follow-up observations outside this cleanup

- Production `StimTargetRect` has no remaining repository consumer after targeting the live `SSVEPRectView`. It was left untouched because this task concerns tests.
- The opening description in `tests/application/test_bootstrap.py` still says container construction starts the engine; current composition creates workers lazily. Its assertions remain useful, and its prose was left outside the cleanup scope.
