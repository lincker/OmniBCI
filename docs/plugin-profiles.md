# Current configuration, saved profiles, and history

Each plugin has editable **Current configuration** values. A saved Profile is a
named reusable copy for one exact package, contribution, and version. Applying
current values never requires saving a Profile first.

## Using the console

- Edit current parameters in Setup. Decoder, Epoch Detector, each ordered Filter,
  Paradigm, Trigger, and Effector retain separate saved Profiles.
- Edit output mode and connection settings in the owning Stimulation or Actuation
  page. Stimulation also owns its renderer; Actuation owns watchdog and command
  age limits. Saving or loading a plugin Profile leaves these module settings intact.
- Save current values as a new Profile when they should be reusable. Load a
  Profile to copy its values into the editor. Neither action applies a module.
- **Connect** applies the current Acquisition values, starts the engine when
  needed, and connects. Metadata inspection is a separate action.
- **Start reading** requires a source connection and starts Acquisition data flow.
  It prepares Processing only if that module is already running. It does not
  apply Processing or Actuation edits. Stop reading keeps the source connected.
- **Start processing** applies current Processing values and starts its worker,
  starting the engine when needed. It can wait for Acquisition data. Stop
  processing leaves the source connected and resumes any prior reading.
- Launching Stimulation and starting Actuation apply only their respective current
  values. Each has its own stop action. Stop/disconnect commands use the running
  configuration without submitting edits.
- **Start engine** starts shared infrastructure without selecting plugins,
  applying module edits, or starting any module. **Stop engine** stops all modules.
- An external node checks its own module revision and the shared system revision.
  With the engine running, the module button prepares its current configuration
  while disconnected. Start/connect the external node, then enable Stimulation
  or start Actuation. No Acquisition selection or A/P worker is required.
  Changing a connected external node's configuration requires disconnecting it.
  A shared system rebuild also requires disconnecting external nodes.

There is no global **Apply current configuration** or **Update saved profile**
button. Shared system settings retain explicit review; module settings, including
Processing's pre-trigger duration, travel with that module's current values.
Editing, loading and saving remain independent of runtime application. Browsing a different plugin
or Profile does not silently change running parameters.

Current edits survive page navigation. Refresh initializes current values from
the backend's accepted modules; unapplied browser edits are not stored on disk.
Connect and Start use one backend request. A failed request preserves current
edits; confirmed application advances only that request's editor baseline.
Concurrent edits from another client produce a revision conflict instead of
being overwritten by an automatic refresh and retry.
When a conflict exists, **Load accepted configuration** explicitly replaces that
module's edits with the backend values and resets its editor baseline.
Decoder current values and Profiles include the channel mapping described in
[channel mapping](channel-mapping.md). An empty output list explicitly selects
all source signal channels; live source metadata remains authoritative.

## History and restore

Each successful module change records a full immutable snapshot with the apply
operation UUID, time, semantic content hash, and optional restored-from entry ID.
A → B → A produces three entries even though the first and last hashes match.
No-change requests, editor changes, failed applies, and internal rollbacks do not
create entries. Retrying an operation does not duplicate its history.

Module **History** offers inspection and restore through the normal plan review.
Restoring validates the historical exact package pins, applies through the normal
runtime boundary, and creates a new history entry. On success, only that module's
current editor values are refreshed; other module edits remain intact. Missing or
disabled packages must be made available before restoring.

Saved Profile history uses integer revisions. Restoring a Profile creates a new
revision containing the older values. It does not load the editor or apply a
module. Profile references copied into bindings retain their saved provenance;
a newer saved revision can be shown as changed since application.

## Storage and ownership

The root is `OMNIBCI_CONFIG_DIR` when set, otherwise
`platformdirs.user_config_path("OmniBCI", appauthor=False)` on the backend machine.

```text
plugin-profiles/<uuid>/current.toml
plugin-profiles/<uuid>/history/<integer-revision>.toml
activation/<module>.toml
activation/system.toml
activation/history/<module>/<operation-uuid>.toml
locks/profiles.lock
locks/<module>.lock
locks/system.lock
```

Four modules have independent files, locks, and compare-and-swap revisions:
`acquisition`, `processing`, `stimulation`, and `actuation`. System settings have
an independent revision but no history or restore. An absent file has a null
accepted revision. The first module application explicitly includes initial
system settings in its reviewed plan if they have not yet been accepted. Starting
an empty engine can also accept the initial shared settings without selecting plugins.

Atomic replacement preserves the prior file on write failure. Profiles are
format version 2 with exact package pins. Legacy flat profile files and the old
`activation.toml` are not read or migrated. Keep backups before changing storage
formats. A Profile cannot be deleted while a current module references it;
deleting an unreferenced Profile also removes its history.

Bindings contain complete parameters, selected versions and schema versions.
Editing a saved Profile cannot change an accepted binding. Module content hashes
include owned host settings, slot identities, mapping, parameters and filter
order; profile IDs and profile revision metadata are excluded. The combined
runtime settings view is derived and has no global revision.

Profiles remain portable data. Platform checks apply when a contribution executes;
a Windows-only trigger can be configured on another host for an external node.
No account, cloud synchronization, or secret export service is introduced.

## Device-specific parameters

For CURRY 8 with parallel TTL, select **Neuroscan CURRY 8 Parallel Trigger**,
edit the current parameters, then launch stimulation. Saving a Profile is optional. Set
`parallel_port_address` to the hexadecimal I/O base address reported by Windows
Device Manager; `0x3100` is a default, not automatic device detection. The
stimulation host needs a physical parallel port, a compatible Neuroscan trigger
cable connected to the amplifier, and an installed InpOut kernel driver and DLL
matching the Python process (`inpoutx64.dll` for 64-bit, `inpout32.dll` for 32-bit).
Make the DLL available to the Windows DLL loader, for example alongside the
Python executable. See the [InpOut installation instructions](https://www.highrez.co.uk/downloads/inpout32/).

Sending markers and clearing both write directly to the configured base address.

InpOut's `Out32` has no success return value. A successful `send()` therefore
means the function returned, not that output pins changed or CURRY received a
marker. Mock-driver and ctypes tests verify software addressing only. Verify
physical output and received marker codes on the Windows stimulation host.

The output uses eight stimulus bits: codes 1–255, with 0 reserved for the idle
baseline. The paradigm calls `clear()` before each event and at its end;
`send()` holds the code until cleared. Clearing retains Scan45's 20 ms low
interval. There is no automatic pulse timer or amplifier acknowledgement.
The amplifier's accepted codes, bit mask, polarity and sampling rate still apply.
In CURRY's **Amplifier Control → Trigger Settings**, use the matching third-party
TTL mode (**Other** in newer CURRY 8 versions, previously **Neuroscan Stim2/STIM2**)
and check incoming stimulus bits before recording. See the
[CURRY 8 manual, Appendix A](https://www.sci.utah.edu/~wolters/LiteraturZurVorlesung/Vorlesungen/Tutorial/Curry8/Curry8UserGuide.pdf#page=912)
and [CURRY 8 release notes](https://www.compumedicsneuroscan.com/Uploads/Curry806ReleaseNotes.pdf#page=36).

This trigger is Windows-only; the CURRY NetStreaming acquisition plugin remains
cross-platform. On macOS, select **None (no trigger output)** for a visual preview
or run stimulation on a Windows host. Selecting a CURRY reader does not
automatically select its trigger.

For Scan45, `channel_count` is the total number of channels in each incoming
sample, including the final event channel. The default is 10 (9 EEG + 1 event),
and any integer of at least 2 is accepted. For 32 EEG channels and one event
channel, enter 33. This value must match the device's stream; the driver does not
automatically discover its channel layout.
