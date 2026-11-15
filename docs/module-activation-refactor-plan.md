# Module Activation Refactor — Implementation Plan

This document is a self-contained hand-off. Read all of it before you change any code.
Line numbers and §2 describe the original baseline. The implementation now follows
the amendments below and the executable contracts; use current symbols when reviewing.

## Approved implementation amendments

- Independent runtime operations use module scopes, including operation tracking,
  recovery, worker locks, and output-channel locks. Engine/System changes retain
  global scope. Module acknowledgements and rollbacks merge only owned values.
- Engine startup reads only System and does not bind Stimulation/Actuation ports.
  Each output prepares its listener on its own Prepare/Start action. Module Stop
  retains the listener; Engine Stop closes all listeners.
- Processing attaches/detaches its input at FIFO boundaries without pausing or
  checkpointing Acquisition. Detached samples are not buffered or replayed,
  including file input. EOF/disconnect finish the prior input boundary. P can
  remain waiting for source metadata; P preparation errors do not prevent A reading.
- A full Processing input queue closes that input and marks Processing for
  recovery; Acquisition continues reading and previewing. Faults from an old
  input cannot reset a newer attachment.
- Stop accepts ownership and waits for overlapping operations to settle before
  cleanup. Unrelated modules remain available. History-only write failure is
  reported separately from uncertain runtime state.
- Backend shutdown uses the same Engine Stop path. Module Stop/Disconnect remains
  available for recovery even after its process exits. Cancelled HTTP requests
  do not abandon owned starts, prepares, or stops; Engine Stop also invalidates
  frontend starts that have not yet been sent.
- An unconfirmed external output Stop retains that module's recovery and pinned
  inputs without blocking foundation restart after local resources are released.
  Its existing Prepare action restores the retained listener configuration for
  the new generation; reconnect and confirm Stop before enabling output again.
- Runtime status uses module-keyed `operations`, `recovery`, and section read
  `errors`. Current frontend drafts and pending operations are tracked per module;
  unrelated revisions do not reset Stimulation Trial inputs.
- Keep semantic SHA-256 module revisions, but identify each history entry with
  the existing apply operation UUID. Successful A → B → A changes retain three
  snapshots; the first and third share a hash but have different entry IDs.
- Append module history only after successful application. No-change requests,
  failed application, internal rollback, and operation retries add no records.
  History is retained without a merge window or new sequence-number service.
- Missing module/system files have null accepted revisions. The first module
  plan explicitly includes initial system settings if absent; reads do not save.
  Starting an empty engine can accept initial system settings without selecting plugins.
- Plans expose `expected_revisions` for their actual dependencies: own module plus
  system, also Acquisition for Processing, and all sections for a shared system rebuild.
- Node configuration uses `module_revisions` uniformly, including the engine's
  Acquisition and Processing revisions, plus `system_revision`.
- Existing `requires` means Python libraries and keeps that meaning. As in VS Code,
  host compatibility and libraries are separate concerns; this refactor introduces
  no inter-plugin package dependency resolver.

---

## 0. Goal

Replace the single global `ActivationSnapshot` (one revision for every module) with:

1. **Per-module activation.** Four modules each have their own revision, file, lock, CAS and history: `acquisition`, `processing`, `stimulation`, `actuation`.
2. **A separate `system` settings section.** It holds settings shared by nodes that may run on other machines. Its own revision is checked at the peer handshake, and any change to it forces a full rebuild.
3. **Per-plugin configuration history.** Each named plugin profile keeps all of its revisions. The user can see them and revert, and each plugin can have several named configurations.
4. **Per-domain plugin packages, VS Code style.** A package's manifest declares a fixed `domain`. Versions are exact pins only. Packages install side by side.

The user's original complaint: "the ActivationSnapshot is not work well, the acquisition part, signal process part, stimulation part should have the seperated plugin version control."

### User decisions (fixed — do not re-ask)

| Topic | Decision |
|---|---|
| Architecture | **Option C**: per-module configuration stores plus per-domain package distribution |
| Modules | Four independent modules: acquisition, processing, stimulation, actuation |
| "Plugin version" | Means **configuration versioning per plugin** (profile revisions) |
| Package versions | VS Code style, **exact pins only** (no ranges in selections) |
| Migration | **None.** The system is not online. Delete legacy formats and readers. Old `activation.toml` files are not read. |
| History | **User-visible, with revert.** Several named configurations per plugin. |
| Package domain | Never changes. Scan45 and Curry8 packages are `acquisition` domain and ship their own `trigger` contributions, which the stimulation module uses. |
| Host settings | Each one is assigned to exactly one module. Shared settings go in a separate `system` section, because stimulation and actuation may run on separate machines. |
| Delivery | **Approach B: big-bang replacement.** No compatibility shims, no dual paths. |
| Runtime boundary | NeuroEngine is the shared foundation. After it starts, Acquisition, Processing, Stimulation, and Actuation run independently. |

### Implemented boundary: NeuroEngine and independent modules

NeuroEngine owns the event bus, queues, command transport, and output gateways.
Starting it requires no source or plugin and starts no business module. Stopping
it stops all modules and releases shared infrastructure. Each module uses its
own current configuration and explicit start/stop path; there is no new module
registry, generic lifecycle layer, or global configuration version.

Acquisition Connect starts its worker and connects the source. Processing starts
independently and can wait for Acquisition metadata and data. Reading starts only
the Acquisition stream; it prepares Processing if already running. Stimulation
and Actuation require the foundation, but neither an Acquisition selection nor
running A/P workers. Their external preparation actions apply only their own
current values. Actuation uses explicit Start/Stop, with no `enabled` host flag.

A/P module restart preserves the engine generation and unrelated output processes.
The engine retains sequence, trial, and epoch counters across worker replacement.
`NEUROENGINE.queue_size` belongs to system because the queues are shared foundation
resources. System changes can restart the engine; module changes restart only the
module that owns the affected runtime resources.

Acceptance checks:
- Start the engine with no selected plugins; all four modules remain stopped.
- Start Processing or an output without running Acquisition. Processing may wait
  for source data; output commands validate only the inputs they actually use.
- Acquisition streams and previews data with Processing stopped, without building
  a processing backlog or waiting for a missing processor at drain.
- Starting, stopping, or reconfiguring one module does not start unrelated modules.
- Worker failure does not mark the foundation or unrelated modules unavailable.
- Stopping the engine stops every module and closes the shared runtime.

---

## 1. Project rules (from AGENTS.md — mandatory)

- Write minimal code. Do not add speculative abstractions, helpers or configurability.
- Change only code related to this task, and match the existing style.
- English only. Convert any Chinese comments you touch to English.
- Never delete an author's file-header comment.
- Tests define success. Before each step, state the plan and how you will verify it.
- Ask the user when you are truly uncertain about behaviour. The decisions in §0 are settled.
- The architecture is DDD/Clean, enforced by import-linter (`pyproject.toml`):
  - Layers run presentation → application → domain.
  - application must not import infrastructure.
  - infrastructure must not import presentation.
  - `signal_processing` must not import `neuroengine`.
  - `shared` has zero internal deps.
  - `plugin_catalog` stays read-only.

### Commands

```bash
cd /Users/lincker/workspace/OmniBCI
venv/bin/python -m pytest -q -p no:cacheprovider          # full suite (slow; run in background)
venv/bin/python -m pytest -q -x -p no:cacheprovider tests/application/test_plugin_profiles.py   # single file
venv/bin/lint-imports                                      # architecture contracts
venv/bin/python scripts/export_openapi.py                  # regenerates docs/openapi.json
cd frontend && npm run codegen && npm run typecheck && npm test
```

### Local-network tests (run with socket access)

- `test_market_adapter::test_real_signed_http_to_disabled_install_enable_profile_and_runtime_plan`
- `test_market_repository::test_signed_local_http_repository_installs_and_refuses_real_redirects`
- `test_node_client::test_authenticated_configuration_never_follows_redirect`
- `test_desktop_server::test_desktop_handshake_prepare_resume_and_clean_shutdown`

---

## 2. Original baseline — historical context

- **Branch:** `refactor/ui-configuration`. It has about 54 files of **pre-existing uncommitted user work**. Do not revert, reformat or "clean up" those changes. Among them:
  - `plugin_profiles.py` added `plugin_kind_for_slot`.
  - `plugin_profile_service.py` accepts draft bindings, `{slot, selection, parameters, ...}`, for any slot.
- **Backup** of the tree before this refactor: `/Users/lincker/workspace/OmniBCI-backup-before-module-activation-20261114.tar.gz`
- **The only refactor edit done so far:** `omnibci/application/activation_settings.py` was rewritten. It now contains:

  ```python
  MODULES = ("acquisition", "processing", "stimulation", "actuation")
  SYSTEM_SECTION = "system"
  SECTIONS = (*MODULES, SYSTEM_SECTION)
  SECTION_SETTING_KEYS = {...}            # see §3.3
  RUNTIME_HOST_SETTING_KEYS = union of all sections
  BACKEND_STARTUP_DEFAULTS = {...}        # unchanged
  def backend_startup_settings(settings) -> dict   # unchanged
  def section_settings(settings, section) -> dict  # new: copy keys owned by one section
  ```

  Removed: `PLUGIN_SETTING_KEYS`, `_RETIRED_SETTING_KEYS`, `is_inert_activation_setting`, `activation_host_settings`.

  **The code currently does not import cleanly.** These still reference removed names:
  - `omnibci/application/services/plugin_profile_service.py` (imports at about lines 8–10)
  - `tests/conf/test_host_settings_storage.py` (about line 106)

  Still-valid users of the file: `omnibci/conf/__init__.py:15`, `omnibci/bootstrap.py:8`, `omnibci/application/node_configuration.py:8`, `tests/conf/test_settings.py:9`.

---

## 3. Target design

### 3.1 Domain model (`omnibci/application/plugin_profiles.py`)

Keep:
- `PLUGIN_KINDS`
- `MODULE_KINDS`:
  - acquisition: {acquisition}
  - processing: {decoder, epoch_detector, filter}
  - stimulation: {paradigm, trigger}
  - actuation: {effector}
- `_SLOTS`
- `plugin_kind_for_slot` (`processing.filter.<uuid>` → filter)
- `_copy_data`, `_object_data`, `_fields`, `_processing_input`
- the canonical JSON function: `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)`, then SHA-256 hex

Add a helper `module_for_slot(slot) -> str`, which returns the prefix before the first `.` and validates it is in `MODULES`.

**Delete (legacy, no migration):**
- `is_default_legacy_processing_input`
- `_legacy_fields`
- `_parse_profile_fields` legacy branches
- the `legacy` field on `PluginProfile` and `ProfileBinding`
- `format_version` values other than the current one
- `version=None` in `PluginSelection`; `version` is now a required, non-empty exact version string

**Types:**

```text
PluginSelection(package_id: str, contribution_id: str, version: str)       # version required, exact

PluginProfile(profile_id, name, kind, selection, parameters,
              configuration_version=1, revision=0, processing_input=None)
  - revision: int, incremented on every save (CAS on it, as today)

ProfileRevision(profile_id, revision: int, saved_at: str ISO-8601, profile: PluginProfile)
  - immutable history entry

ProfileBinding(slot, kind, profile_id|None, selection, parameters,
               configuration_version=1, profile_revision=0, processing_input=None)
  - as today, minus `legacy`

ModuleActivation(module: str, settings: dict, bindings: tuple[ProfileBinding, ...])
  - validates: module in MODULES
  - validates: every binding.slot belongs to this module (module_for_slot)
  - validates: binding.kind in MODULE_KINDS[module] and == plugin_kind_for_slot(slot)
  - validates: settings keys ⊆ SECTION_SETTING_KEYS[module]
  - revision property: sha256(canonical({"module", "settings", "bindings"}))
    where bindings exclude profile_id and profile_revision (same rule as today)
  - to_dict() / from_dict(data) ; from_dict verifies stored revision equals computed
  - empty module = ModuleActivation(module, {}, ())  (valid; e.g. no actuation)

ModuleActivationRevision(module, entry_id: str, saved_at: str, activation: ModuleActivation, restored_from=None)
  - immutable history entry

SystemSettings(settings: dict)
  - validates keys ⊆ SECTION_SETTING_KEYS["system"]
  - revision property: sha256(canonical({"system": settings}))
  - to_dict() / from_dict()

ActivationSet(modules: Mapping[str, ModuleActivation | None], system: SystemSettings | None)
  - read-only view, NOT stored
  - revisions() -> {module: revision | None, system: revision | None}
  - bindings() -> all bindings in MODULES order
  - settings() -> merged dict of all module settings plus system settings
```

**Delete `ActivationSnapshot` entirely.** There is no stored global revision.

`PluginProfileRepository` Protocol (replace the old one):

```text
list_profiles(kind=None) -> list[PluginProfile]
get_profile(profile_id) -> PluginProfile | None
save_profile(profile, expected_revision: int | None) -> PluginProfile     # writes history
delete_profile(profile_id, expected_revision: int) -> None                # history is deleted with it
list_profile_history(profile_id) -> list[ProfileRevision]                 # newest first
get_profile_revision(profile_id, revision: int) -> ProfileRevision | None

get_module(module) -> ModuleActivation | None                             # None if file missing
save_module(activation: ModuleActivation, expected_revision: str | None) -> ModuleActivation
list_module_history(module) -> list[ModuleActivationRevision]             # newest first
get_module_history_entry(module, entry_id: str) -> ModuleActivationRevision | None
record_module_history(activation, entry_id, restored_from=None) -> ModuleActivationRevision

get_system() -> SystemSettings | None
save_system(settings: SystemSettings, expected_revision: str | None) -> SystemSettings

get_activation_set() -> ActivationSet                                     # reads 4 modules + system
```

`expected_revision=None` means "the file must not exist yet". This is the same CAS semantics as today.

### 3.2 Storage layout (`TomlPluginProfileRepository`)

Root = the current plugin config root (unchanged).

```text
<root>/
  plugin-profiles/<profile_id>/current.toml
  plugin-profiles/<profile_id>/history/<revision:int>.toml
  activation/acquisition.toml
  activation/processing.toml
  activation/stimulation.toml
  activation/actuation.toml
  activation/system.toml
  activation/history/<module>/<operation-uuid>.toml  (entry_id, saved_at, activation, restored_from)
  locks/profiles.lock
  locks/<module>.lock                                (one per module)
  locks/system.lock
```

Rules:
- Keep the current safety code:
  - `_safe` rejects symlinks and paths that escape the root.
  - Atomic write: temp file, then fsync, then `os.replace`.
  - fcntl/msvcrt locks with 10 ms retry until `lock_timeout_s`.
- **Each module and `system` has its own lock and its own CAS.** Saving `stimulation` must never conflict with saving `processing`.
- `save_module` / `save_system` replace only current accepted files, with no history side effect.
- After successful application, `record_module_history` writes an immutable full
  snapshot identified by the operation UUID. Repeating that entry ID with the
  same snapshot is idempotent; different content conflicts. Reverting creates a
  new entry, even when its content hash already occurs in history.
- Rollback restores/removes only sections written by the failed operation using
  their expected candidate revisions. It does not overwrite another writer.
- `system` has **no** history and **no** revert, by user decision.
- Profile history: `save_profile` writes `history/<new_revision>.toml`, then `current.toml`. `delete_profile` removes the whole `<profile_id>/` dir. It stays blocked while any module binds the profile (see §3.5).
- **Delete** the old `activation.toml`, `.plugin-config.lock` and `plugin-profiles/<id>.toml` handling. Do not read old files.
- The owning capability of a profile still cannot change (keep the existing check).

### 3.3 Setting ownership (already in `activation_settings.py`)

| Section | Keys |
|---|---|
| acquisition | None |
| processing | `ALGORITHM.baseline_duration` |
| stimulation | `STIMULATION.server_ip`, `STIMULATION.server_port`, `STIMULATION.renderer`, `STIMULATION.auto_spawn` |
| actuation | `ACTUATION.server_ip`, `ACTUATION.server_port`, `ACTUATION.watchdog_timeout_s`, `ACTUATION.max_command_age_s`, `ACTUATION.auto_spawn` |
| system | `NEUROENGINE.queue_size`, `NEUROENGINE.startup_timeout_s`, `debug` |

`BACKEND_STARTUP_DEFAULTS` (`LOGGING.*`, `NEUROENGINE.command_timeout_s`, `PLUGINS.user_dir`) are process-startup settings, not runtime settings. They are unchanged.

Shared settings are written by the settings page into `settings.toml` (existing
`api.patchConfig`). Accepting `system` copies `section_settings(host_settings,
"system")`. Module settings come from the current editor's explicit action
request; host values only initialize an absent module. Output addresses belong
to their respective modules and never trigger a shared system rebuild.

### 3.4 Runtime identity

The materialized runtime dict changes:

| Old key | New key |
|---|---|
| `CONFIG.revision` (64-hex) | **removed** |
| — | `CONFIG.module_revisions`: `{"acquisition": hex, "processing": hex, "stimulation": hex, "actuation": hex}` |
| — | `CONFIG.system_revision`: hex |
| `CONFIG.generation` | changes on foundation restart, not module restart |
| `PLUGINS.bindings` | unchanged shape (all bindings) |

Rules:
- Each worker or node keeps and checks **only its own module revision, plus `CONFIG.system_revision`**:
  - acquisition worker → acquisition
  - signal processor → processing
  - stimulation node → stimulation
  - actuation node → actuation
- **The signal contract.** Processing consumes observed Acquisition source facts (`sample_rate`, `channel_count`, `channel_labels`). Output-node startup does not require a source.
  - If an acquisition change alters these facts, the processing module must be re-validated and re-applied.
  - If the facts are unchanged, processing is untouched.
- `PeerIdentity(role, config_revision, generation)` becomes `PeerIdentity(role, module_revision, system_revision, generation)`. The handshake rejects a peer whose `system_revision` or own-module revision differs.
- `StartupReport.config_revision` becomes `module_revisions` (dict) plus `system_revision`. `wait_for_startup_report(expected_module_revisions, expected_system_revision, expected_generation)`.
- `PluginContext.config_revision` (SDK) becomes `module_revision`, meaning the revision of the module that loaded the plugin. Update `omnibci/sdk/__init__.py` accordingly.
- `ResolvedSignalPlan.config_revision` in `domain/channel_mapping.py` becomes `acquisition_revision`, because it describes the source contract.

### 3.5 Apply strategies (`RuntimeConfigurationService._strategy`)

A plan targets **one module or system**. A module candidate is a `ModuleActivation`, and its first plan may also carry initial `SystemSettings`. Strategy, from strongest to weakest:

| Change | Strategy |
|---|---|
| No module or system revision differs | `no_change` |
| Engine not running | `select_on_start` (accept for next start) |
| `system` changed | `rebuild` (foundation and previously running modules) |
| acquisition: source binding or source parameters changed | `rebuild` (Acquisition only) |
| processing: epoch_detector changed, decoder `processing_input` changed, or `ALGORITHM.baseline_duration` changed | `rebuild` (Processing only) |
| processing: only decoder/filter parameters changed, and every changed parameter has `x-apply: trial` | `trial_boundary` |
| processing: other decoder/filter changes | `module` (processing only) |
| stimulation or actuation changed (bindings or their own settings) | `module` (that module only) |

Notes:
- Keep the existing rule: if an external peer (`auto_spawn=false`) for stimulation/actuation is connected and a change needs it, report a plan error instead of applying. Keep the current behaviour and message, and scope it to the affected module or a shared system rebuild.
- Applying an inactive module updates the prepared snapshot without starting its worker. Only a system rebuild recreates the foundation and restores previously running modules. Source changes revalidate running Processing against source metadata without restarting unrelated outputs.
- **Remove the cross-module revision broadcast.** Today a stimulation-only change still sends `UpdateProcessingContext` and `UpdateAcquisitionRevision` so every worker sees the new global revision. Delete:
  - `UpdateAcquisitionRevision = 107` and `UpdateProcessingContext = 213` in `omnibci/shared/events.py`, plus their routing in `neuroengine/engine.py` (about lines 467, 470)
  - `RuntimeApplyCoordinator._update_source_revision` and `_output_context`
  - acquisition `update_revision` command (`infrastructure/acquisition/acquisition.py` about lines 51, 73, 158, 284–290)
  - `signal_processor._update_output_context`
  - `pipeline_configuration.validate_output_context`
- Each strategy persists **only the modules it applied**, via `save_module(..., expected_revision)`. Rollback restores only those modules.

### 3.6 Revert

- **Module revert:** `POST /config/modules/{module}/revert-plan {entry_id}`.
  1. Load `get_module_history_entry(module, entry_id)`.
  2. Re-validate it against the current catalog. Pinned package versions must still be installed and enabled; otherwise return plan errors that list the missing packages.
  3. Return a normal `ConfigurationPlan`, which the user applies with the existing `POST /config/apply`.
  - Revert is never a silent write. Success appends a new history entry and updates only the restored module's current editor values.
- **Profile revert:** `POST /config/plugin-profiles/{id}/revert {revision, expected_revision}`.
  - Writes a **new** revision whose content is copied from the old one, so history is append-only.
  - It does not change any active module. Modules that bind this profile show "profile changed" in the UI. Applying the change is a normal module plan with `refresh_slots`.

### 3.7 REST API (presentation)

Replace:
- `GET/PUT /config/activation` (`routers/plugin_config.py`)
- `AcceptActivationRequest`, `ActivationResponse` (`schemas/plugin_config.py`)

New endpoints, all wrapped in the existing `ApiEnvelope`:

```text
GET  /config/modules                                -> {modules: {<m>: ModuleActivationResponse}, system: SystemSettingsResponse}
GET  /config/modules/{module}                       -> ModuleActivationResponse {module, revision, settings, bindings[]}
GET  /config/modules/{module}/history               -> [{entry_id, revision, saved_at, restored_from, binding_summary[]}]
GET  /config/modules/{module}/history/{entry_id}    -> ModuleActivationResponse
POST /config/modules/{module}/revert-plan           body {entry_id}            -> RuntimePlanResponse
GET  /config/system                                 -> {revision, settings}

GET  /config/plugin-profiles/{id}/history           -> [{profile_id, revision, saved_at, profile}]
POST /config/plugin-profiles/{id}/revert            body {revision, expected_revision} -> PluginProfileResponse
```

Changed endpoints:
- `POST /config/apply-plan` (`RuntimePlanRequest`):
  - `module` becomes required. It takes one of `MODULES` or `"system"`.
  - `expected_revision` means that module's revision (or the system revision).
  - Remove `include_host_settings`. A module plan always copies that section's host settings, and a system plan copies the system settings.
  - Keep `bindings` (draft or profile reference) and `refresh_slots`.
- `GET /config/runtime` (`RuntimeConfigurationResponse`):
  - Replace `accepted_revision` / `applied_revision` with `accepted: {module: rev, system: rev}` and `applied: {module: rev, system: rev} | null`.
  - Keep `running`, `recovery_required`, `generation`, `operation`.
- `RuntimePlanResponse` / `ConfigurationPlan`:
  - `module`
  - `expected_revision`
  - `expected_revisions`: dependency revision map checked at review and execution
  - `target_revisions: {module: rev}`
  - `applied_revisions`
  - keep `strategy`, `changed_slots`, `changed_settings`, `errors`, `signal_summary`, `generation`, `running`
- `GET /config/nodes/{role}` (`NodeConfigurationResponse`): `revision` becomes `module_revisions` (a map) plus `system_revision`.
- Acquisition preview `expected_revision` (`presentation/schemas/acquisition.py` about line 85, `application/services/acquisition_service.py` about lines 255–267): it now means the **acquisition** module revision. Keep the 64-hex validation.

After changing the schemas, run `venv/bin/python scripts/export_openapi.py`, then `cd frontend && npm run codegen`.

### 3.8 Packages (VS Code style)

These already exist; keep them:
- side-by-side install `<root>/publisher.name/<version>/` (`package_paths.py`)
- `state/enabled.json` (`installed_registry.py`)
- removal blocked while referenced (`_require_unused`)
- `profile-version-proposals`
- market `requires` / `host_version` (`package_installer.py`, `market_repository.py`)

To do:
1. **Manifest `domain`.**
   - Add a required `[package] domain = "<module>"` to every manifest. The value must be in `MODULES`.
   - Validate that every contribution kind is allowed for the domain:
     - The domain's own `MODULE_KINDS`.
     - Plus `trigger` for `acquisition` packages: Scan45 and Curry8 ship triggers used by `stimulation.trigger`.
     - Keep the allow-list small and explicit; one constant in the catalog code.
   - The domain must not change across versions of the same package. Reject installing a version whose domain differs from the installed versions.

   Manifests to edit (all `manifest_version = 2`):

   | Manifest | Domain |
   |---|---|
   | `omnibci/infrastructure/plugins/acquisition/file/manifest.toml` | `acquisition` |
   | `omnibci/infrastructure/plugins/acquisition/neuroscan/curry8/manifest.toml` | `acquisition` (default + `trigger`) |
   | `omnibci/infrastructure/plugins/acquisition/neuroscan/scan45/manifest.toml` | `acquisition` (default + `trigger`) |
   | `omnibci/infrastructure/plugins/algorithms/filters/manifest.toml` | `processing` |
   | `omnibci/infrastructure/plugins/algorithms/epoch/manifest.toml` | `processing` |
   | `omnibci/infrastructure/plugins/algorithms/decoders/classic40_cca/manifest.toml` | `processing` |
   | `omnibci/infrastructure/plugins/algorithms/decoders/classic40_fbcca/manifest.toml` | `processing` |
   | `omnibci/infrastructure/plugins/paradigms/classic40SSVEP/manifest.toml` | `stimulation` |
   | `omnibci/infrastructure/plugins/effectors/logger/manifest.toml` | `actuation` |

   Also update the test-fixture manifests and market fixtures that tests build (Grep for `manifest_version` under `tests/`).
2. **`engines` check.**
   - The manifest's `[package] engines = { omnibci = ">=X,<Y" }` must match the host version at install time and at catalog load time. Reject the package if it does not match.
   - Reuse the existing version comparison used by `host_version` in `package_installer.py`; do not add a new dependency.
3. **`requires` check.** Preserve the installer's exact Python-library requirements. Do not reinterpret these as plugin pins. Bound plugin selections themselves must resolve to installed, enabled exact versions when planning.
4. **Delete legacy:**
   - manifest v1 parsing
   - `api_version = "1"` handling
   - `version=None` selections
   - `resolve_legacy` (`plugin_catalog.py` about lines 955–957, `configuration_catalog.py` about line 39)
5. **Expose the domain** in catalog/package API responses (`domain` field), so the frontend can group packages by domain.

### 3.9 Frontend (`frontend/src`)

- `api/plugin-config.ts`:
  - Remove `activation()`.
  - Add `modules()`, `module(m)`, `moduleHistory(m)`, `moduleHistoryEntry(m, entryId)`, `moduleRevertPlan(m, entryId)`, `system()`, `profileHistory(id)`, `profileRevert(profile, revision)`.
  - Replace `Activation` with `ModuleActivation` / `ModulesResponse`.
- `api/runtime-config.ts`: update the types to `accepted` / `applied` maps, and make `module` required in `RuntimePlanRequest`.
- `hooks/useCurrentConfiguration.ts`:
  - Baseline is a `ModulesResponse`.
  - `currentBindings(module, ...)` reads `modules[module].bindings`.
  - `request()` sends `expected_revision: modules[module].revision`; drop `include_host_settings`.
  - `hasChanges` and `dirty` work per module.
- `ConfigCard.tsx`: review shared system settings only. Processing's pre-trigger
  duration belongs to its current module editor and is submitted with its plugins.
- Module actions submit complete current configuration directly to their existing
  Connect/Start endpoints. The backend owns application and the subsequent action.
  `source-lifecycle.ts`, frontend apply-before-start polling, and repeated revision
  checks are removed. Start reading sends its source revision, generation, and
  connection ID for validation inside the backend operation.
- `RunConsole.tsx` (about lines 100–432), `RuntimeConfiguration.tsx` (about lines 56–76), `App.tsx` (about line 62), `hooks/useRuntimeConfiguration.ts` (about line 118), `AcquisitionPreview.tsx` (about line 333): replace the global `accepted_revision` / `applied_revision` / `activation.revision` uses with per-module values. The preview uses the acquisition revision.
- `PluginProfiles.tsx` (about lines 146–515):
  - Add a **History** panel for the selected module: a list of revisions with time and binding summary, a "View", and a "Revert…" that calls `moduleRevertPlan` and opens the existing plan review flow.
  - Add a per-profile history list with "Restore this version", which calls `profileRevert`.
  - Show "profile changed since applied" when a bound profile's revision is newer than the binding's `profile_revision`.
- `PluginMarket.tsx`: group installed and available packages by `domain`, with tabs or sections: Acquisition / Processing / Stimulation / Actuation.
- Add the needed i18n keys in English (and in any existing locales, following the current pattern in `i18n`).
- Regenerate `api/rest-types.ts` with `npm run codegen`. Never hand-edit it.

---

## 4. Implementation steps (in order)

Do the steps in order. After each step, run the listed verification. Tests may stay red until the step that rewrites them; record which failures you expect.

### Step 1 — Domain model

Files:
- `omnibci/application/plugin_profiles.py` (rewrite per §3.1)
- `omnibci/application/activation_settings.py` (already done; do not change it unless needed)

Tasks:
- Implement `ModuleActivation`, `ModuleActivationRevision`, `SystemSettings`, `ActivationSet`, `ProfileRevision` and `module_for_slot`.
- Make `PluginSelection.version` required.
- Delete `ActivationSnapshot` and all the legacy code listed in §3.1.
- Update the `PluginProfileRepository` Protocol.

Verify:
- `venv/bin/python -c "import omnibci.application.plugin_profiles"`
- Rewrite `tests/application/test_plugin_profiles.py` for the new types. Cover: revision stability; revision ignores `profile_id`/`profile_revision`; slot/kind/setting ownership errors; `from_dict` revision mismatch; `SystemSettings` key validation.

### Step 2 — Repository

File: `omnibci/infrastructure/repositories/plugin_profile_repository.py` (rewrite per §3.2).

Verify:
- Rewrite `tests/infrastructure/test_plugin_profile_repository.py`. Cover:
  - per-module CAS conflict
  - **no conflict between different modules**: save stimulation while processing holds its lock
  - history written on every save
  - content-addressed module history
  - profile history and delete
  - symlink/escape rejection
  - lock timeout
  - a missing module file returns an empty `ModuleActivation`

### Step 3 — `PluginProfileService`

File: `omnibci/application/services/plugin_profile_service.py`.

- Fix the broken imports: use `SECTION_SETTING_KEYS`, `section_settings`, `MODULES`.
- `propose(module, bindings, settings, refresh_slots)` returns a `ModuleActivation` candidate from the submitted bindings and explicit module settings. Host values initialize an absent module's editor; applying current configuration does not merge them implicitly. Keep both binding forms (`{slot, profile_id}` and the draft form).
- `propose_system()` returns a `SystemSettings` candidate from host settings.
- `validate_module(candidate, active_kinds)`:
  - Do the existing schema/default/availability checks, scoped to one module.
  - Check that package pins are installed and enabled, and retain the separate Python-library installation check (§3.8.3).
  - For acquisition, keep `_validate_source_parameters`: sample_rate and channel_count must be positive ints.
- The runtime coordinator persists successfully applied candidates through the repository. Remove unused profile-service acceptance wrappers.
- `materialize(activation_set, generation)` produces the runtime dict with `CONFIG.module_revisions`, `CONFIG.system_revision`, `CONFIG.generation`, `PLUGINS.bindings`, `ACQUISITION.signal_source`, `ACQUISITION.<params>`, plus all section settings.
- `delete` is blocked if any module binds the profile.
- Add `profile_history(id)` and `revert_profile(id, revision, expected_revision)`.
- Delete `include_host_settings` and `_validate_host_changes`. Its checks against the startup defaults (`startup_settings_resolver` / `loaded_startup_settings`) stay where they are relevant to the system section: a mismatch in `BACKEND_STARTUP_DEFAULTS` still requires a process restart, as today.

Verify: rewrite `tests/application/test_plugin_profile_service.py` and update `tests/plugin_configuration_helpers.py`. Its `accept_profiles` helper proposes each module with explicit settings and saves fixture values through the repository.

### Step 4 — `RuntimeConfigurationService`

File: `omnibci/application/services/runtime_configuration_service.py`.

- `ConfigurationPlan` fields per §3.7.
- `PlannedConfiguration` holds `candidates: dict[module, ModuleActivation]` and `system: SystemSettings | None`. Keep the 300 s TTL and the cap of 32.
- `plan(module, bindings, expected_revision, refresh_slots)`:
  - Compute the candidate.
  - If the module is acquisition and the source contract changed, also re-validate processing against the new contract. On failure, add plan errors. If Processing is running, prepare its existing worker against the new source metadata before resuming the stream.
- `revert_plan(module, entry_id)` per §3.6.
- `status()` returns `accepted` / `applied` maps.
- `_strategy` per §3.5.

Verify: rewrite `tests/application/test_runtime_configuration_service.py` and `tests/application/test_module_configuration.py`. Add a case that a stimulation-only plan has `target_revisions` with only `stimulation`.

### Step 5 — `RuntimeApplyCoordinator`

File: `omnibci/application/services/runtime_apply_service.py`.

- Delete `_update_source_revision` and `_output_context`, plus their calls in `_apply_pipeline` (about lines 131–159) and `_apply_outputs` (about lines 183–244).
- `_pipeline_receipt` checks `applied_revision == candidates["processing"].revision`.
- `_quiesce` checks the drain receipt's per-module revision, not `CONFIG.revision`.
- Every path persists only the applied modules (`save_module`). Rollback restores only those modules. `_rebuild` (about lines 337–413) may persist several modules and/or the system section, in the §3.5 order.

Verify: rewrite `tests/application/test_runtime_apply_failures.py`, `test_runtime_narrow_apply.py` and `test_runtime_apply_integration.py`. Add a test that a stimulation `module` apply sends **no** command to the acquisition or processing workers.

### Step 6 — Node identity, workers and peers

Replace every `CONFIG.revision` read with the module/system revision of that component (§3.4):

- `omnibci/application/node_configuration.py`:
  - `NodeConfiguration(role, module_revisions, system_revision, generation, settings, bindings)`.
  - `project_node_configuration` reads `CONFIG.module_revisions[<role module>]`. Role → module: engine → acquisition+processing (keep both revisions), stimulation → stimulation, actuation → actuation.
  - `materialize_node_configuration` writes `CONFIG.module_revisions` (only the node's module) and `CONFIG.system_revision`.
  - Delete the legacy `_ALIASES` entries if they only existed for old keys. Keep them if runtime code still reads, e.g., `ACQUISITION.trigger`; verify with Grep first.
- `omnibci/infrastructure/ipc/peer_identity.py`: `PeerIdentity` fields per §3.4.
- `omnibci/infrastructure/neuroengine/builder.py` (about 98–130), `engine.py` (about 72, 95, 253; and remove the event routes at about 467, 470).
- `omnibci/infrastructure/process_lifecycle.py`: `StartupReport`, `wait_for_startup_report`.
- `omnibci/infrastructure/stimulation/application.py` (about 58, 441–442), `stimulation/process_manager.py` (about 68, 245).
- `omnibci/infrastructure/actuation/application.py` (about 82, 384–385), `actuation/process_manager.py` (about 54, 235).
- `omnibci/infrastructure/signal_processing/worker.py` (about 70–71), `signal_configuration.py` (about 19), `signal_processor.py` (about 333–415: `_prepare_signal` checks the acquisition revision in the receipt; `_install_configuration` stores the processing revision; delete `_update_output_context`), `pipeline_configuration.py` (`build_pipeline_update` mirrors: replace `CONFIG.revision` with `CONFIG.module_revisions`; delete `validate_output_context`).
- `omnibci/infrastructure/acquisition/acquisition.py` (preview identity about 246/255, command checks about 282, state about 508: use the acquisition revision; delete `update_revision`), `preview_capture.py` (about 23, 81), `worker.py` (about 82).
- `omnibci/shared/events.py`: delete the two events.
- `omnibci/infrastructure/plugins/runtime_configuration.py` (about 28), `omnibci/sdk/__init__.py`: `PluginContext.module_revision`.
- `omnibci/domain/channel_mapping.py`: `ResolvedSignalPlan.acquisition_revision`.
- `omnibci/infrastructure/plugins/node_client.py`: handshake and node config.

Then run Grep over `omnibci/` for: `CONFIG.revision`, `config_revision`, `UpdateAcquisitionRevision`, `UpdateProcessingContext`, `ActivationSnapshot`, `get_activation`, `save_activation`. Also check these that read `config_revision` and update them: `results_service`, `outbound.py`, `epoch_processor.py`, `processing_state.py`, `configuration_transactions.py`, `effectors/logger/effector.py`. When no matches remain, the step is done.

Verify:
- `tests/application/test_node_configuration.py`, plus the worker/peer/stimulation/actuation/acquisition tests that fail.
- `venv/bin/lint-imports`.

### Step 7 — Remaining application consumers

- `omnibci/application/services/engine_service.py` (about 162, 250, 392, 455, 463): `applied_revision` becomes the `applied` map; `node_configuration_snapshot`.
- `omnibci/application/services/signal_setup_service.py` (about 40, 73–80, 98, 166–179): use acquisition + processing modules.
- `omnibci/application/services/plugin_package_service.py` (about 38, 80): "is package referenced" checks all modules via `get_activation_set()`.
- `omnibci/infrastructure/plugins/accepted_configuration.py` (`accepted_runtime_settings`): build from `get_activation_set()`.
- `omnibci/application/processing_source.py`: drop the legacy `processing_input` default.
- `omnibci/application/services/acquisition_service.py` (about 255–267): preview `expected_revision` is the acquisition revision.
- `omnibci/bootstrap.py` (about 181 `startup_settings_resolver`, about 247 `_RuntimeSettings`): wiring for the new repository and service signatures.
- `tests/conf/test_host_settings_storage.py` (about 106): replace the import of the removed functions with the new section API.

Verify: the full test suite runs without import errors (failures in not-yet-rewritten tests are OK at this point), and `lint-imports` passes.

### Step 8 — Presentation

- `omnibci/presentation/routers/plugin_config.py`, `routers/runtime_config.py`
- `omnibci/presentation/schemas/plugin_config.py` (delete the legacy `_visible_processing_input`, `ActivationResponse`, `AcceptActivationRequest`), `schemas/runtime_config.py`, `schemas/acquisition.py`

Implement exactly §3.7, then run `scripts/export_openapi.py`.

Verify: rewrite `tests/presentation/test_plugin_config_router.py` and the runtime config router tests. Cover: module CAS conflict → 409; history list; revert-plan returns a plan; a revert plan with an uninstalled pinned package returns errors; profile revert creates a new revision.

### Step 9 — Packages

Implement §3.8. The relevant files are under `omnibci/infrastructure/plugins/` (`plugin_catalog.py`, `configuration_catalog.py`, `package_installer.py`, `market_repository.py`, `installed_registry.py`, `package_paths.py`) and the 9 manifests.

Verify:
- Catalog and installer tests.
- New tests:
  - a missing or invalid `domain` is rejected
  - a disallowed contribution kind for a domain is rejected
  - an acquisition package with a `trigger` is accepted
  - a domain change across versions is rejected
  - an `engines` mismatch is rejected
  - an unavailable bound plugin produces a plan error; missing Python `requires` fails installation

### Step 10 — Frontend

Implement §3.9.

Verify: `cd frontend && npm run codegen && npm run typecheck && npm test`. Update the frontend tests that mock `/config/activation` or use the global revisions (Grep `frontend/src` for `activation`, `accepted_revision`, `applied_revision`, `include_host_settings`).

### Step 11 — Full test pass

- Grep `tests/` for the old names and rewrite the remaining tests (78 files referenced them; about 10 need heavy rewrites, the rest are small fixture or name changes).
- Delete tests that only covered removed legacy behaviour: legacy formats, v1 manifests, `resolve_legacy`, `version=None`, `UpdateAcquisitionRevision` / `UpdateProcessingContext`.
- Done when the full suite is green except the 4 known network failures (§1), `lint-imports` passes, and the frontend checks pass.

### Step 12 — Docs and contracts

- Update `docs/configuration-ownership.md`, `docs/module-config.md`, `docs/plugin-profiles.md`, `docs/plugin-manifest.md` (`domain`, `engines`, `requires`) and `docs/plugin-installation.md`.
- Delete `docs/plugin-profile-migration.md`, since there is no migration any more.
- If you need a new import-linter contract, add it to `pyproject.toml`. For example, presentation must not import `plugin_profile_repository` directly; check the existing contracts first.
- Delete this plan file only if the user asks.

---

## 5. Things you must not do

- Do not keep `ActivationSnapshot`, a global `CONFIG.revision`, or any "compat" alias for them.
- Do not read or convert old `activation.toml` / old profile files.
- Do not add version ranges to selections. Pins stay exact.
- Do not add history or revert for the `system` section.
- Do not touch the 54 pre-existing user changes beyond what this refactor requires.
- Do not hand-edit `frontend/src/api/rest-types.ts` or `docs/openapi.json`; regenerate them.
- If a behaviour is unclear and not settled by §0, stop and ask the user.
