# Plugin Management Refactor Plan & `plugin_type` Removal Roadmap

Status: Phases 1–4 implemented on branch `refactor/plugin-management` (branched from `dev` at `474b7cf`); Section 3 (`plugin_type` removal) remains mandatory immediate follow-up.

## 1. Confirmed Architectural Decisions

1. **Symmetric Clean Architecture layout (Scheme A)**:
   - Built-in plugin implementations remain under `omnibci/infrastructure/plugins/` (`acquisition/`, `algorithms/`, `effectors/`, `paradigms/`). This preserves `pyproject.toml` `package-data` rules, asset build scripts, and `vendor_plugins_dir()`.
   - Plugin Management moves out of `omnibci/infrastructure/plugins/` into two symmetric packages aligned with `omnibci/application/configuration/`:
     - `omnibci/application/plugin_management/` — pure models, identity/schema validation, ports, and services (`PluginPackageService`, `PluginMarketService`).
     - `omnibci/infrastructure/plugin_management/` — manifest parsing, catalog discovery, package store & enablement registry, ZIP installer, signed market repository, artifact preparation, loader, store ownership, execution history, and author packaging tools.
   - Bidirectional `import-linter` contracts enforce strict isolation between `omnibci.infrastructure.plugin_management` and `omnibci.infrastructure.plugins`.
   - Misplaced non-plugin modules move to their owning layers:
     - `accepted_configuration.py` → `omnibci/application/configuration/`
     - `node_client.py` → `omnibci/infrastructure/node_client.py`
   - Shared filesystem primitives (`file_lock`, `atomic_write`, `strict_json_loads`) move to `omnibci/infrastructure/storage.py` for shared use by `repositories/` and `plugin_management/`.
2. **Manifest version unified to `1`**:
   - Pre-launch single contract: change `manifest_version = 2` to `manifest_version = 1` across `manifest.py`, `descriptors.py`, all built-in manifests, `examples/decoder_plugin/manifest.toml`, tests, and active docs without backwards-compatibility shims.
3. **Aggressive dead-code removal (no compatibility shims)**:
   - Delete `[contributions.params]` from manifest schema, `PluginDescriptor`, built-in manifests, and tests (zero production consumers).
   - Delete unused `PluginManifestError.plugin_type` / `plugin_types` fields and their helper parameters.
   - Delete unused `plugin_type = ...` class attributes from built-in plugin classes.
   - Delete unread legacy snapshot alias keys (`ALGORITHM.decoder`, `ALGORITHM.epoch_detector`, `ALGORITHM.filters`, `STIMULATION.paradigm`, `ACQUISITION.trigger`, and broken `ACTUATION.effector`) from `node_configuration.py` / `definitions.py` / `ActuationProcessManager`.
   - Delete unused `PluginPackageInstaller.catalog`, phantom `trigger_plugin_roots` (`vendor_plugins_dir() / "triggers"`), nonexistent `omnibci.infrastructure.plugins.registry` in `pyproject.toml`, and unreferenced `omnibci/infrastructure/plugins/algorithms/ssvep/{fabcca.py,time_space.py}`.
4. **Package API surface unchanged**:
   - Keep `GET /api/v1/plugin-packages` scoped to managed user packages; consolidate the duplicated `/plugin-packages` routers and centralize FastAPI DI providers in `omnibci/presentation/dependencies.py`.
5. **Phased handling of `plugin_type` (Option 1 now, mandatory removal next)**:
   - During `refactor/plugin-management`, remove all dead/unread uses of `plugin_type` while keeping the active cross-module contract intact so this branch stays bounded to plugin management.
   - Complete removal of `plugin_type` is tracked as a **mandatory immediate follow-up** in Section 3 below.

---

## 2. Execution Phases & Verification Gates

| Phase | Scope | Verification |
| --- | --- | --- |
| **Phase 1: Manifest v1, dead code, shared primitives & host requirements** | Switch `manifest_version` 2 → 1; delete all dead code listed in §1.3; extract `omnibci/infrastructure/storage.py` (unify 3 file-lock implementations, `atomic_write`, `strict_json_loads`) and deduplicate ID/version/SHA-256/entrypoint checks; add `scipy` and `pillow` to host library allowlist with range matching and remove server-only packages (`fastapi`, `uvicorn`, `websockets`, `tomlkit`, `platformdirs`); remove ghost `registry` contract in `pyproject.toml`. | `./venv/bin/lint-imports`; `./venv/bin/pytest -m "not gui and not hardware and not integration"`; new tests for Manifest v1 rejection of v2, `scipy`/`pillow` requirements, and range matching. |
| **Phase 2: Performance & unified runtime loading** | Eliminate 7×/49× `enabled.json` and manifest re-scans in `PluginCatalog` & `set_enabled()` (add stat/mtime snapshot cache, single `enabled_package_directories` read per scan, $O(N)$ `list_packages()`); eliminate 6× full-directory SHA-256 hashing across preparation and load by passing verified inventory/digest; unify runtime loading via a single `LoadedPlugin` bundle used by all 5 runtime modules (`Acquisition`, `SignalProcessing`, `Stimulation`, `Actuation`, `NeuroEngine`); remove import-time `sys.meta_path` mutation in `loader.py`; add backend `controls` schema validation matching `frontend/src/plugin-controls.ts`. | Unit tests asserting `enabled.json` read count on `set_enabled` and `artifact_inventory` call count across prepare+load; backend `controls` validation tests; full headless pytest + `lint-imports`. |
| **Phase 3: Structural consolidation (Scheme A)** | Create `omnibci/application/plugin_management/` and `omnibci/infrastructure/plugin_management/`; relocate `accepted_configuration.py` → `application/configuration/` and `node_client.py` → `infrastructure/node_client.py`; merge `PluginInstallationService` + `PluginPackageService`, merge `DirectoryPluginPackageStore` + `ManagedPluginPackageStore` (resolving Protocol name clash & private-method reaching), merge `market_repository` + `market_adapter`, merge `/plugin-packages` routers and centralize DI in `presentation/dependencies.py`; update `import-linter` contracts for bidirectional isolation between `plugin_management` and `plugins`. | OpenAPI contract diff check; `./venv/bin/lint-imports`; full headless + integration pytest; `npm test` in `frontend/`. |
| **Phase 4: Built-in SDK alignment & docs** | Migrate remaining built-in plugins under `omnibci/infrastructure/plugins/` to import public contracts exclusively from `omnibci.sdk` (removing private `omnibci.infrastructure.*` imports where applicable or documenting explicit host-only helpers); update `docs/plugin-manifest.md`, `docs/plugin-development.md`, `docs/decoder-plugin-development.md`, `docs/plugin-api-v1.md` to match Manifest v1 and final paths. | `omnibci-plugin validate` on all built-in plugins and `examples/decoder_plugin`; full headless + integration pytest; `./venv/bin/lint-imports`; `frontend/` tests. |

---

## 3. Mandatory Follow-Up: Complete Removal of `plugin_type`

`plugin_type` is a legacy v1 global string alias that conflicts with exact `PluginSelection(package_id, contribution_id, version)` identity. For example, two versions or packages declaring the same `plugin_type` collide in status comparisons (`stimulation.data.paradigm === selectedParadigm.plugin_type`), and frontend UI behavior is hardcoded to specific built-in aliases.

**This follow-up MUST be executed immediately after `refactor/plugin-management` merges** (as a dedicated cross-module PR touching `acquisition`, `stimulation`, `signal_setup`, OpenAPI schemas, and `frontend/`).

### 3.1 Remaining Active Touchpoints to Replace

| Area | Current Usage | Target Replacement |
| --- | --- | --- |
| **Acquisition `source_id`** | `configuration/service.py:320`, `node_configuration.py:41`, `signal_setup_service.py:82-127`, `processing_source.py:43-50`, `acquisition_service.py:252`, `acquisition/worker.py:80` pass `ACQUISITION.signal_source` (`plugin_type`) as `source_id`. | Identify the active source by exact `PluginSelection` (or canonical `f"{package_id}/{contribution_id}@{version}"` / structured `selection` field) in `AcquisitionStatus`, `SourceMetadata`, and `SignalSetupService`; remove `ACQUISITION.signal_source`. |
| **Stimulation status `paradigm`** | `stimulation/process_manager.py:49-53` (`_configured_paradigm`) exposes `descriptor.plugin_type`; `frontend/src/RunConsole.tsx:120-128` compares `stimulation.data.paradigm === selectedParadigm.plugin_type`. | Expose exact `paradigm_selection: PluginSelection` (and optional display label) in `StimulationStatus`; compare exact `selectionKey(...)` in `RunConsole.tsx`. |
| **Frontend acquisition metadata UI** | `frontend/src/PluginProfiles.tsx:51,81,152,451` hardcodes `["file_reader", "neuroscan_curry8_reader"]` and `definition?.plugin_type === "neuroscan_curry8_reader"` to decide whether to hide dimension fields until device observation and whether to import `channel_labels`. | Replace hardcoded alias checks with explicit source observation capability metadata (e.g., driven by `SourceObservation.provenance` or minimal acquisition manifest capability flags) so third-party acquisition plugins work identically without frontend edits. |
| **Frontend i18n display names** | `frontend/src/i18n.tsx:141-146` (`translatePluginName`) keys `PLUGIN_NAME_KEYS` by `plugin_type`. | Key built-in i18n display overrides by canonical `f"{package_id}/{contribution_id}"` (falling back to manifest `display_name`). |
| **Manifest, Catalog & OpenAPI** | `manifest.py`, `descriptors.py`, `PluginConfiguration`, `PluginConfigurationResponse`, all built-in `manifest.toml` files, and `frontend/src/api/rest-types.ts`. | Remove `plugin_type` from `manifest.toml`, `PluginDescriptor`, `PluginConfiguration`, `PluginConfigurationResponse`, and regenerate `docs/openapi.json` + `frontend/src/api/rest-types.ts`. |

### 3.2 Acceptance Criteria for the Follow-Up PR

- Zero occurrences of `plugin_type` across `omnibci/`, `examples/`, `frontend/src/`, and `docs/openapi.json`.
- Zero hardcoded built-in plugin identifiers in `frontend/src/PluginProfiles.tsx` and `frontend/src/RunConsole.tsx` (also align `RunConsole.tsx:119` playback detection with source `resume_mode` / capability).
- Two versions of the same paradigm or acquisition package are unambiguously distinguished in runtime status, metadata comparison, and `RunConsole` readiness checks.
- Full backend (headless + integration), `lint-imports`, `omnibci-plugin validate`, and frontend unit tests pass.
