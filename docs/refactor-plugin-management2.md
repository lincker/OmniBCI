# Plugin Management Refactoring Plan (Part 2): Remove File-Based System State

Date: 2026-11-27. Source baseline: `9f90129` (`dev`, after merging `refactor/ipc`).

Status: **Finalized design (Refined Scheme A)**, implemented in 5 incremental commits on `refactor/plugin-management-2`.

Supersedes: [fix-plugin-management.md](fix-plugin-management.md) and the intermediate multi-lock/WAL review drafts (see Section 3 for the review disposition).

> **Immediate workaround for older builds:** Before upgrading, preserve the preceding Server/node logs, stop relaunchers, confirm that old OmniBCI processes and device operations have stopped, and remove only the `.active` file named in the error message. See [Windows recovery procedure](fix-plugin-management.md#3-recover-the-reported-windows-installation).

---

## 1. Problem Statement & Root-Cause Analysis

### 1.1 Reported Runtime Error

After the plugin management consolidation (`8973637`), running OmniBCI on Windows failed during Server startup with:

```text
omnibci.application.plugin_management.packages.PluginPackageConflictError:
Plugin store has an unconfirmed shutdown. Stop its hosts and device operations
before explicitly removing
C:\Users\lincker\AppData\Local\OmniBCI\OmniBCI\plugins\state\.controller.active
to acknowledge recovery
```

### 1.2 Origin and Execution Chain

The persistent marker mechanism was introduced in `c3f59c6` (`omnibci/infrastructure/plugins/store_ownership.py`) and relocated verbatim into [ownership.py](../omnibci/infrastructure/plugin_management/ownership.py) during `refactor/plugin-management` (`8973637`), changing only the imported exception module path.

- **Startup path:**
  `lifespan` -> `_services_lifespan` ([main.py](../omnibci/presentation/main.py)) -> `build_container()` ([bootstrap.py](../omnibci/bootstrap.py)) -> `PluginStoreOwner(store.plugin_dir, configuration_root=repository.root)` -> acquires `.controller.lock` -> `_require_settled(.controller.active)`.
  If `.controller.active` exists, API server startup aborts immediately. Otherwise, `.controller.active` is created before the Engine, any plugin, or any hardware module has started.
- **Shutdown path:**
  `_services_lifespan` `finally` -> `ServerShutdownService.prepare()` ([shutdown_service.py](../omnibci/application/services/shutdown_service.py)) -> calls `owner.close()` (unlinking `.controller.active`) **only** when zero shutdown errors occur. On any exception, it calls `owner.abandon()`, releasing `.controller.lock` while intentionally leaving `.controller.active` on disk.

### 1.3 Why It Triggers Frequently in Real Usage

Beyond crashes, power loss, IDE Stop, Task Manager termination, Windows console close (`CTRL_CLOSE_EVENT`), and Desktop **Force exit** (`LocalServer.force()` in [local-server.ts](../desktop/src/local-server.ts)), two concrete code defects cause **normal operations** to leave `.active` markers behind:

1. **Defect A — Confirmed child teardown after a startup/runtime error is misclassified as unconfirmed cleanup:**
   When a managed output child (Stimulation or Actuation) raises a startup or runtime exception (e.g., Windows Stimulation 60 Hz display validation error), its `finally: _shutdown()` runs cleanly (closing the window, clearing the trigger, quiescing/tearing down the effector), and then Python propagates the original exception and exits with `exitcode=1`.
   `ManagedOutputProcessManager._stop_locked()` ([process_lifecycle.py](../omnibci/infrastructure/process_lifecycle.py)) treats **any** `exitcode != 0` identically as `cleanup was not confirmed` and permanently latches `self._cleanup_error`. When the user later performs a **normal** Server shutdown, `stop()` re-raises `self._cleanup_error` -> `ServerShutdownService.prepare()` reports failure -> `_services_lifespan` calls `owner.abandon()` -> `.controller.active` stays on disk and blocks every subsequent Server start (surviving reboots) until manually deleted.

2. **Defect B — Normal `Ctrl-C` on standalone nodes permanently bricks package mutations and node restarts:**
   [run_stimulation.py](../scripts/run_stimulation.py) and [run_actuation.py](../scripts/run_actuation.py) document `Press Ctrl-C to exit`. However, `run_configured_node()` ([node_client.py](../omnibci/infrastructure/node_client.py)) catches `BaseException` (which includes `KeyboardInterrupt`) and calls `owner.abandon()`. Because `asyncio.run(self.serve())` re-raises `KeyboardInterrupt` even after `_shutdown()` succeeds, pressing `Ctrl-C` permanently leaves `.stimulation.active` or `.actuation.active` on disk, blocking all `PluginPackageService` mutations (`install`, `set_enabled`, `remove`) via `controller.mutation()` and preventing the node from restarting until manually deleted.

### 1.4 Structural Flaws of the Current Design

- **Misaligned recovery boundary:** A single store-wide file blocks the entire Web/Desktop control plane (API and UI) from starting, even when no Engine or hardware module ever ran, preventing the operator from viewing logs or recovering through the UI.
- **False safety guarantee:** `.controller.active` holds a fixed text string with no PID, module identity, or error context. Meanwhile, managed child hosts (`AcquisitionWorker`, `SignalProcessingWorker`, `StimulationProcess`, `ActuationProcess`) hold no store locks at all. Deleting the file does not stop orphaned hosts; keeping it permanently blocks an already-stopped machine.
- **Overly broad mutation exclusion:** `PluginStoreOwner.mutation()` rejects even non-destructive operations (`install` of a new package, `enable` of an unrelated package) whenever an external node holds a role lock.
- **Conflated responsibilities:** `PluginStoreOwner` bundles four unrelated concerns into one mechanism: (1) cross-process server exclusivity, (2) in-process synchronization between configuration references and package mutations, (3) hardcoded config-to-plugin directory binding (`controller.json`), and (4) physical device/process shutdown confirmation.

---

## 2. Necessity Analysis: File-Based System State Audit

Using persistent files on disk to represent transient system runtime state is **not necessary** and is an anti-pattern for a local/desktop BCI control plane. Every non-user-code file currently written under `plugins/state/`, `config/`, and `.omnibci-dev/` falls into one of five distinct categories:

| Category | Current Files | What It Represents | Necessary as File State? | Final Decision (Refined Scheme A) |
| --- | --- | --- | --- | --- |
| **1. Persistent runtime markers** | `plugins/state/.controller.active`<br>`plugins/state/.stimulation.active`<br>`plugins/state/.actuation.active`<br>`.omnibci-dev/run-*/shutdown.json` | Whether the previous process confirmed clean shutdown | **No (harmful).** Files cannot prove physical hardware state or process liveness after crashes or reboots. | **Remove completely.** Module safety belongs to in-memory `RuntimeAdmission` quarantine + explicit child teardown confirmation + driver setup/quiesce on explicit start. `plugin_development.py` checks `Popen.wait()` exit code directly. |
| **2. Directory path binding** | `plugins/state/controller.json` | Hardcoded absolute `configuration_root` bound to `plugin_dir` | **No.** Breaks directory relocation and `OMNIBCI_CONFIG_DIR` switching; missing packages are already caught explicitly during catalog/artifact preparation. | **Remove completely.** |
| **3. Process-lifetime role locks** | `plugins/state/.controller.lock`<br>`plugins/state/.stimulation.lock`<br>`plugins/state/.actuation.lock` | Cross-process server/node liveness + in-process reference mutex | **No.** In-process consistency only needs `threading.RLock`. Package versions are immutable directories; `disable`/`remove` are guarded by `configuration_references` + `runtime_references`. Single-instance UX is already covered by Electron `app.requestSingleInstanceLock()` and TCP port binding. | **Remove completely** along with `PluginStoreOwner`. Replace in-process coordination with a single `threading.RLock()`. |
| **4. Short-lived write serialization locks** | `plugins/state/.enabled.lock`<br>`packages/__locks/<id>.lock`<br>`config/locks/<section>.lock`<br>`config/execution-history/.lock` | Millisecond-scale file read-modify-write mutexes (`flock` / `msvcrt.locking`) | **Not system state.** Kernel-managed and automatically released on process exit (never stale). | **Keep unchanged** in this pass to preserve atomic storage integrity. |
| **5. Persisted domain & audit data** | `plugins/state/enabled.json`<br>`packages/<id>/<ver>/.omnibci-install.json`<br>`config/settings.toml`, `plugin-profiles/`, `activation/`<br>`config/execution-history/*.json` | User enablement choices, immutable package integrity receipts, user configuration, and execution audit records | **Persisted data, not runtime state.** | **Keep unchanged.** Move `PluginExecutionHistory` out of `ownership.py` into `execution_history.py`. *(Note: `execution-history/*.json` is currently write-only in production code; evaluate separately.)* |

---

## 3. Review Disposition

During design review, several valid lifecycle edge cases were identified alongside heavyweight multi-lock/WAL proposals. Each item is resolved as follows:

| Review Item | Disposition | Engineering Rationale |
| --- | --- | --- |
| **Distinguish "startup/runtime error with confirmed teardown" from "unconfirmed teardown / forced kill" without relying on `READY`** | **Adopted** | Both `Actuation.start()` and `Stimulation.serve()` call driver `setup()` *before* reporting `READY`, so `READY` alone cannot distinguish hardware touch. Instead, track `cleanup_confirmed` right after `finally: _shutdown()` completes inside the child and exit via a dedicated `CLEAN_FAILURE_EXITCODE` (`65`). Confirmed teardown after a business/startup error clears cleanly for retry and shutdown; genuine teardown failure (`exitcode=1`), crash (`-9`/`-15`), or forced termination retains in-memory `_cleanup_error` quarantine without blocking the next Server launch. |
| **Do not let an empty second `Stop` silently clear quarantine after an unconfirmed crash or forced kill** | **Adopted** | Retaining `self._cleanup_error` in memory on `ManagedOutputProcessManager` *only* when cleanup was genuinely unconfirmed (never on `CLEAN_FAILURE_EXITCODE` or `0`) prevents a no-op second `Stop` or blind `Start` from clearing quarantine within the same Server lifetime, while independent modules stay usable and restarting the Server cleanly boots idle without stale disk files. |
| **Keep additive (`install`/`enable`) unblocked while modules run** | **Adopted** | Under the shared in-process `RLock` (held only during short metadata/preparation steps), `install` and `enable` proceed freely while modules run; `disable` and `remove` check exact `(package_id, version)` configuration and runtime references. |
| **Ensure standalone `Ctrl-C` runs teardown cleanly and surfaces real teardown failures** | **Adopted** | Removing `PluginStoreOwner` eliminates stale `.active` files; verifying `cleanup_confirmed` and letting teardown exceptions propagate ensures clean `Ctrl-C` exits cleanly while real teardown failures remain visible. |
| **Retain `controller.json` + bidirectional `config/` binding** | **Rejected** | Persisting absolute paths reproduces stale-file startup lockouts on directory relocation or `OMNIBCI_CONFIG_DIR` changes; missing packages are already caught explicitly on load. |
| **Add cross-process per-version `LockFileEx`/`flock` locks + node-wide durable hardware WAL + remote grants + legacy marker archive state machine** | **Rejected** | Violates the explicit zero-file-runtime-state requirement and `AGENTS.md` simplicity rule. OmniBCI Plugin API v1 has no canonical physical device identity or readback/recovery port; adding a WAL + Win32 `LockFileEx` + legacy upgrade UI would introduce thousands of lines of fragile state machinery for a pre-launch codebase. |

---

## 4. Target Architecture (Refined Scheme A)

### 4.1 Core Design Principles

1. **Zero persistent runtime state files:** Server startup, standalone nodes, and `omnibci-plugin develop` never create, require, or check `.active` files, `controller.json`, `shutdown.json`, or process-lifetime role `.lock` files.
2. **Control plane always boots cleanly:** Starting the OmniBCI Server exposes the REST/WebSocket API and UI with all runtime modules stopped. No prior crash, force exit, or module error can prevent the control plane from launching.
3. **Exact in-process reference & mutation consistency:** A single in-memory `threading.RLock()` wired in `bootstrap.py` serializes configuration profile/activation writes, local plugin artifact preparation, and package admission checks. `install`/`enable` remain non-blocking while modules run; `disable`/`remove` reject only packages referenced by saved profiles, accepted module activations, active sessions, or pending Apply operations.
4. **Honest in-memory child teardown semantics (zero disk files):**
   - `Stimulation.serve()` and `Actuation.serve()` set `self.cleanup_confirmed = True` only after their `finally: _shutdown()` completes without raising (window closed + trigger cleared for Stimulation; pending intents cancelled + `quiesce()` + `teardown()` + IPC closed for Actuation).
   - `run_managed_child()` logs the traceback and exits with `CLEAN_FAILURE_EXITCODE = 65` when an exception escapes `app.run()` **after** `app.cleanup_confirmed is True`; otherwise it re-raises (`exitcode = 1`).
   - `ManagedOutputProcessManager._stop_locked()`:
     - **Still alive after kill escalation (`process.is_alive() is True`):** Retain `self._process` and block replacement.
     - **Exited with `exitcode in (0, CLEAN_FAILURE_EXITCODE)` and NOT forcibly terminated:** Cleanup is confirmed — close handle, clear process state, do **not** latch `_cleanup_error`, allow immediate retry `start()` and clean Server shutdown.
     - **Forcibly terminated (`terminate()`/`kill()`) OR exited with any other code (`1`, `-9`, `-15`, `None`):** Cleanup is genuinely unconfirmed — close and clear the dead handle (`self._process = None`), latch `self._cleanup_error` in memory for this manager instance so neither a no-op second `Stop` nor blind `Start` silently clears `RuntimeAdmission` quarantine, while independent modules remain usable and the next Server launch boots cleanly with no `.active` lockout.
5. **Preserve Clean Architecture boundaries:** All `import-linter` contracts in [pyproject.toml](../pyproject.toml) remain green; no new abstraction layers are introduced.

### 4.2 Component-Level Changes

#### A. Replace `ownership.py` with `execution_history.py`
- **Delete:** `PluginStoreOwner` and [ownership.py](../omnibci/infrastructure/plugin_management/ownership.py).
- **Add:** `omnibci/infrastructure/plugin_management/execution_history.py` containing only `PluginExecutionHistory` (moved unchanged from `ownership.py`).
- **Update imports:** [bootstrap.py](../omnibci/bootstrap.py), [builder.py](../omnibci/infrastructure/neuroengine/builder.py), [worker.py](../omnibci/infrastructure/signal_processing/worker.py).

#### B. Composition Root & Server Lifespan (`bootstrap.py`, `main.py`)
- **[bootstrap.py](../omnibci/bootstrap.py):**
  - Remove `package_owner: PluginStoreOwner | None = None` from `AppContainer`.
  - Remove `PluginStoreOwner` acquisition and `owner.close()` wrapper in `build_container()`.
  - Create one in-memory `package_references_lock = RLock()` and pass `lambda: package_references_lock` to `ConfigurationService.change_guard`, `RuntimeConfigurationService.reference_guard`, `PluginPackageService.admission_guard`, and `prepare_plugins`.
- **[main.py](../omnibci/presentation/main.py):**
  - Remove `container.package_owner.abandon()` / `.close()` from `_services_lifespan`'s `finally` block. Keep `app.state.shutdown_errors`, `app.state.shutdown_complete`, `request_gate`, `shutdown_service`, and `ipc_runtime` teardown intact.

#### C. Standalone Nodes (`node_client.py`)
- **[node_client.py](../omnibci/infrastructure/node_client.py):**
  - Remove `PluginStoreOwner` import, acquisition, `close()`, and `abandon()`.
  - Keep `ManagedPluginPackageStore`, `materialize_node_configuration`, `PluginArtifactPreparer`, and the clean reconnect loop.
  - Pressing `Ctrl-C` in [run_stimulation.py](../scripts/run_stimulation.py) or [run_actuation.py](../scripts/run_actuation.py) executes `_shutdown()` cleanly without leaving state files; if teardown itself fails, the teardown exception propagates visibly.

#### D. Plugin Development Host (`plugin_development.py`)
- **[plugin_development.py](../omnibci/plugin_development.py):**
  - Remove `PluginStoreOwner(workspace, configuration_root=workspace)` and all `owner.close()` / `owner.abandon()` calls.
  - Remove `shutdown.json` writing/reading and `.controller.active` checks in `_settled()`.
  - Rely on child process exit status `result == 0` (the child's `main()` already returns `0 if server.started and app.state.shutdown_complete else 1`):
    - If `result == 0`: delete `run` (`shutil.rmtree(run)`) and return `0`.
    - If `result != 0` or launch/wait fails: retain `run` for diagnostic inspection and raise/return failure without locking `.omnibci-dev` against subsequent `omnibci-plugin develop` runs.

#### E. Output Child Teardown Confirmation (`process_lifecycle.py`, `stimulation/application.py`, `actuation/application.py`)
- **[stimulation/application.py](../omnibci/infrastructure/stimulation/application.py) & [actuation/application.py](../omnibci/infrastructure/actuation/application.py):**
  - Initialize `self.cleanup_confirmed = False` and set `self.cleanup_confirmed = True` immediately after `await await_owned(asyncio.create_task(self._shutdown(...)))` returns without raising.
- **[process_lifecycle.py](../omnibci/infrastructure/process_lifecycle.py):**
  - Define `CLEAN_FAILURE_EXITCODE = 65`.
  - In `run_managed_child()`: when an exception escapes `app.run()` and `getattr(app, "cleanup_confirmed", False) is True`, log the exception and `raise SystemExit(CLEAN_FAILURE_EXITCODE) from exc`; otherwise re-raise normally.
  - In `ManagedOutputProcessManager._stop_locked()`: treat `exitcode in (0, CLEAN_FAILURE_EXITCODE)` (when not forcibly terminated) as confirmed cleanup (`_cleanup_error` remains `None`, state becomes `ChildProcessState.FAILED` if `exitcode == CLEAN_FAILURE_EXITCODE` else `final_state`, handle & metadata cleared). Treat forced termination or any other exit code (`1`, `-9`, `-15`, `None`) as unconfirmed cleanup (`_cleanup_error` retained in memory; dead handle closed and cleared).

---

## 5. Phased Implementation Plan & Verification Steps

Each step is committed individually on branch `refactor/plugin-management-2` after passing its verification checks.

### Step 1: Remove `PluginStoreOwner`, Extract `PluginExecutionHistory`, Wire In-Memory `RLock`
- Create `omnibci/infrastructure/plugin_management/execution_history.py`, delete `ownership.py`.
- Update imports in `bootstrap.py`, `neuroengine/builder.py`, `signal_processing/worker.py`.
- Replace `PluginStoreOwner` with `threading.RLock()` in `bootstrap.py` and remove `package_owner` lifecycle calls in `main.py`.
- **Verification:** `pytest tests/infrastructure/test_execution_history.py tests/neuroengine/test_processing_execution_history.py tests/application/test_shutdown_service.py` + `lint-imports`.

### Step 2: Remove File-State Ownership from Standalone Nodes (`node_client.py`)
- Remove `PluginStoreOwner` from `omnibci/infrastructure/node_client.py`.
- Update `tests/infrastructure/test_node_client.py` to verify zero `.active`/`.lock` files, clean `KeyboardInterrupt` exit, visible teardown failure, and immediate restartability.
- **Verification:** `pytest tests/infrastructure/test_node_client.py`.

### Step 3: Simplify `plugin_development.py` (Exit-Code Driven Cleanup)
- Remove `PluginStoreOwner`, `shutdown.json`, and `_settled()` `.controller.active` checks from `omnibci/plugin_development.py`.
- Update `tests/infrastructure/test_plugin_development.py` and `tests/presentation/test_plugin_development_host.py`.
- **Verification:** `pytest tests/infrastructure/test_plugin_development.py` + `pytest -m integration tests/presentation/test_plugin_development_host.py`.

### Step 4: Distinguish Confirmed Teardown (`CLEAN_FAILURE_EXITCODE`) from Unconfirmed Cleanup
- Add `cleanup_confirmed` in `stimulation/application.py` and `actuation/application.py`.
- Add `CLEAN_FAILURE_EXITCODE` handling in `run_managed_child()` and `ManagedOutputProcessManager._stop_locked()` in `process_lifecycle.py`.
- Add/update tests in `tests/stimulation/test_process_manager.py`, `tests/actuation/test_process_manager.py`, and `tests/infrastructure/test_node_application_lifecycle.py`.
- **Verification:** `pytest tests/stimulation/test_process_manager.py tests/actuation/test_process_manager.py tests/infrastructure/test_node_application_lifecycle.py tests/application/test_stimulation_service.py tests/application/test_actuation_service.py`.

### Step 5: Update Ownership, Bootstrap, Integration Tests, Docs & Full Verification
- Replace `tests/infrastructure/test_plugin_store_ownership.py` with tests verifying zero runtime state files, stale legacy marker tolerance, and in-process `RLock` serialization (additive unblocked, destructive reference-checked).
- Update `tests/application/test_bootstrap.py`, `tests/infrastructure/test_child_runtime_snapshot.py`, `tests/presentation/test_file_plugin_pipeline.py`, `tests/application/test_runtime_apply_integration.py`, and `docs/validation.md`.
- **Verification:** Full headless suite, integration suite, `lint-imports`, and grep confirming zero references to `PluginStoreOwner`, `package_owner`, `.controller.active`, `.stimulation.active`, `.actuation.active`, `controller.json`, or `shutdown.json` in `omnibci/` and `tests/`.

---

## 6. Acceptance Criteria

- [ ] Starting, running, crashing/force-killing, and restarting the OmniBCI Server creates **zero** `.controller.active`, `.stimulation.active`, `.actuation.active`, `controller.json`, or process-lifetime role `.lock` files under `plugins/state/`.
- [ ] Pre-existing stale `.controller.active` or `controller.json` files from older builds are ignored and do not block Server startup.
- [ ] A Server whose final shutdown fails can be restarted immediately without manual file deletion; all modules start stopped by default.
- [ ] A managed Stimulation or Actuation child that raises a startup/runtime error and confirms its own `_shutdown()` exits with `CLEAN_FAILURE_EXITCODE`, allows immediate retry `Start`, and does not fail Server shutdown.
- [ ] A managed child whose `_shutdown()` fails (`exitcode=1`), crashes (`-9`/`-15`), or requires forced termination keeps in-memory `_cleanup_error` so a no-op second `Stop` or blind `Start` cannot silently bypass quarantine in the same Server lifetime; an unstoppable live child (`is_alive() == True`) continues to block replacement.
- [ ] Exiting `scripts/run_stimulation.py` or `scripts/run_actuation.py` with `Ctrl-C` runs teardown cleanly without creating state markers, and surfaces real teardown errors if teardown fails.
- [ ] `omnibci-plugin develop` uses child exit status instead of `shutdown.json` / `.controller.active`; an interrupted or failed run retains its `run-*` directory for inspection without blocking subsequent runs.
- [ ] Additive package operations (`install`, `enable`) succeed while modules run; destructive operations (`disable`, `remove`) remain protected against concurrent configuration and runtime reference changes via the shared in-process `RLock` and exact `(package_id, version)` reference checks.
- [ ] All headless tests, integration tests, and `lint-imports` contracts pass.
