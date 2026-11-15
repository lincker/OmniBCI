# Module configuration

The backend keeps four independent accepted configurations: Acquisition,
Signal Processing, Stimulation, and Actuation. Each owns its settings, pinned
plugin bindings, content revision, file, lock, and application history.
Named saved Profiles remain independent for each plugin.

## Ownership

| Section | Owned host settings | Plugin bindings |
| --- | --- | --- |
| acquisition | None | Source |
| processing | `ALGORITHM.baseline_duration` | Decoder, Epoch Detector, ordered Filters |
| stimulation | `STIMULATION.renderer`, `STIMULATION.auto_spawn` | Paradigm, Trigger |
| actuation | Address/port, watchdog, command age, `auto_spawn` | Effector |
| system | Queue capacity, message-server address/port, startup timeout, `debug` | None |

The `system` revision represents shared infrastructure settings, not an aggregate
of module revisions. A derived `ActivationSet` combines the files for startup;
it does not introduce another saved object or global revision.

## Apply flow

1. The client submits the current bindings for one module and its expected
   accepted revision. A separate system request has no plugin bindings.
2. Planning validates exact selections and owned settings, identifies affected
   runtime resources, and returns the strategy and dependency revision map.
3. Confirmation submits the plan with a unique operation UUID. The coordinator
   rechecks the dependency revisions and runtime generation before applying.
4. Workers acknowledge their new configuration. Only successfully applied
   sections are persisted. A successful change appends module history using
   the operation UUID; repeat submissions are idempotent.

Unrelated module edits do not invalidate a narrow plan. Processing also depends
on Acquisition; every module depends on system. A system rebuild depends on all
sections because it replaces the shared runtime. A first module selection also
initializes system through an explicit plan candidate when its file is absent.
Starting an empty engine accepts initial shared settings without selecting plugins.

| Change while running | Boundary |
| --- | --- |
| No semantic change | No runtime update or history entry |
| System | Restart the engine and restore the previously running modules |
| Acquisition | Restart only Acquisition; revalidate active Processing against source metadata |
| Processing input, Epoch Detector or baseline settings | Restart only Processing |
| Decoder/Filter parameters marked `x-apply: trial` | Trial boundary |
| Other Decoder/Filter changes | Drain, replace Processing in its existing worker, resume |
| Stimulation or Actuation | Replace only that output module |

A stopped module accepts its configuration without starting it. The applied map
reports the engine's prepared configuration; module status reports actual execution.
Narrow Stimulation and
Actuation updates do not broadcast revisions to Acquisition or Processing.
Unknown worker results require recovery; the coordinator does not replay data
or recreate an output driver whose state is unconfirmed.

Live runtime operations require one backend owner for each configuration root.
Independent section locks protect file writes; dependency checks do not form an
atomic transaction against arbitrary other processes editing those files.

External nodes bind their own module and system revisions. Their module cannot
be changed while they are connected. System rebuilds also require those peers to
be disconnected; unrelated narrow module updates remain possible.

## Independent modules on NeuroEngine

NeuroEngine provides the shared event bus, command transport, and peer gateways.
**Start engine** starts this foundation with no source or plugin required. It does
not apply current module edits or start Acquisition, Processing, Stimulation, or
Actuation. **Stop engine** stops all modules and the shared runtime.

- **Connect** applies current Acquisition values, starts the engine when needed,
  starts the Acquisition worker, and connects the source. Disconnect releases that
  worker; Stop reading keeps the connection.
- **Start processing** applies current Processing values and starts its worker.
  It can wait in standby without a connected source. Reading prepares an already
  running Processing module using observed source metadata.
- Acquisition reads and previews data while Processing is stopped. Stopping
  Processing keeps Acquisition connected and resumes its prior reading state.
- Stimulation and Actuation start and stop independently, using only their own
  current values. External nodes require the engine's transport, but neither
  an Acquisition selection nor running A/P workers.
- External preparation applies only the corresponding output configuration.
  Start/connect the external node, then enable that module. `auto_spawn` selects
  managed or external deployment; no separate Actuation enable setting is needed.

Worker restarts retain the engine generation. Source sequence, trial, and epoch
counters remain monotonic during that generation. Worker failure affects that
module's availability, not foundation readiness or unrelated module lifetimes.

## Runtime identity

Runtime settings contain `CONFIG.module_revisions`, `CONFIG.system_revision`,
and `CONFIG.generation`. Node configuration uses a revision map: the engine
receives Acquisition and Processing, and each external output node receives its
own module. Handshakes check the relevant module, system, and generation.

SDK `PluginContext.module_revision` identifies the consuming module. A Trigger
shipped in an acquisition-domain package therefore uses the Stimulation revision
when loaded by Stimulation. Results carry Acquisition and Processing revisions;
source preparation and preview identify the Acquisition revision.

## API and storage

All routes use the normal `/api/v1` prefix and response envelope.

| Route | Purpose |
| --- | --- |
| `POST /engine/start` | Start shared infrastructure only |
| `POST /engine/stop` | Stop every module and shared infrastructure |
| `POST /engine/modules/{module}/start` | Start Acquisition, Processing, or Actuation |
| `POST /engine/modules/{module}/stop` | Stop the specified module |
| `GET /config/modules` | Four accepted modules plus system |
| `GET /config/modules/{module}` | One module; revision is null when absent |
| `POST /config/apply-plan` | Plan current values for the required module |
| `POST /config/apply` | Execute a reviewed plan |
| `GET /config/runtime` | Accepted/applied revision maps, operation and recovery state |
| `GET /config/modules/{module}/history` | Successful application entries, newest first |
| `GET /config/modules/{module}/history/{entry_id}` | Historical full module snapshot |
| `POST /config/modules/{module}/revert-plan` | Plan restoration by history entry ID |
| `GET /config/system` | Accepted shared settings |
| `GET /config/plugin-profiles/{id}/history` | Saved Profile revisions |
| `POST /config/plugin-profiles/{id}/revert` | Copy a historical Profile into a new revision |

See [Plugin profiles](plugin-profiles.md) for current-value workflows, storage,
A → B → A history semantics and independent Profile restoration. There is no
legacy activation endpoint, implicit legacy alias selection, or format migration.
