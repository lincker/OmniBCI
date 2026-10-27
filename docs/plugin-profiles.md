# Plugin profiles and accepted activation

`PluginProfileService` separates saved profiles from an accepted startup snapshot.
Profiles use format version 2 and identify an exact package, contribution, and
version. Each saved edit increments the profile revision. A stale revision is a
conflict and cannot overwrite a newer edit.

## Web console

In Setup, choose the module role, then **Plugin and version**, then **Profile**.
The profile selector and sidebar show only profiles for that exact contribution
and version. Installed managed packages must be enabled in Extensions before
they appear; bundled plugins are available directly.

Acquisition shows **Plugin and version** first, with a selector for an existing
Profile. **New configuration** starts from plugin defaults. Enter the address/port
or recording path, then **Review and apply configuration**. Naming and saving are
part of that final review; there is no separate acquisition Save profile step.

For a first configuration, Review proposes an editable profile name. Confirming
saves a new Profile and applies it. For an existing Profile with changed parameters,
the same dialog lists the old and new values and defaults to **Save as new profile**;
**Update original profile** is an explicit alternative. An unchanged Profile is
applied without another save or revision increment. Cancel leaves the editor draft
intact and performs no save or application. Ctrl/Cmd+S opens this review in Acquisition.
Other modules retain their existing explicit Save profile controls.

Saving and runtime application use separate existing backend requests. After saving,
the frontend replans using the saved profile reference and checks that its target and
runtime impact still match the reviewed plan. Changed impact requires another review.
A later Apply failure leaves the saved Profile available and is reported as saved but
not applied; retry does not save it again. An uncertain save response is reconciled
before any retry, rather than blindly creating a duplicate. Neither saving nor Apply
claims that a device connection or data acquisition has been tested.

**Connect** uses the applied configuration and starts the engine if needed. It does
not read metadata, start reading, or apply editor changes. **Get Metadata** is separate;
a metadata error does not mark a connected source as disconnected. **Disconnect**
releases the source after reading stops. Current parameters and the profile binding
are restored after refresh. Existing inline source activations remain readable;
reviewing one now creates its named Profile.

Curry8 and File Reader can be applied and connected without expected sample rate
or output rows. **Use observed values** copies metadata into the draft; final Review
handles saving and application. For Curry8 it also adopts optional `channel_labels`
in complete native row order, including the final `Trigger` row, blank labels and
duplicates. Invalid or unavailable labels remove stale draft labels. This snapshot
does not change channel mapping; live `SignalInfo` remains authoritative for Signal
Processing and contains only signal channels.

Browsing a plugin or Profile does not select it for execution. The module summary
distinguishes the next-start configuration from a running configuration whose
revision has been acknowledged.

For repeated use, **Run → Connect** uses the persisted startup snapshot and starts
the engine if needed. **Start reading** starts reading from an already connected source;
it never starts the engine or connects implicitly. **Stop reading** keeps the source
connected, so reading can be started again without reconnecting. Run also exposes
**Disconnect**, which requires stopped reading. A selected Decoder requires an Epoch
Detector before reading can start; Run points to Signal Processing when that
selection is missing. Saving a newer Profile does not update the startup snapshot
until an explicit review and application. Stimulation and trial commands remain
separate; configured managed Actuation follows the existing stream preparation
policy. Source expectations and channel mapping are validated when processing is
configured; source-only acquisition does not run that processing validation.
Run displays **Start engine** and **Stop engine** above the startup configuration.
These manual engine controls remain visible alongside engine and worker status.

Run distinguishes managed stimulation from external stimulation using the accepted
`STIMULATION.auto_spawn` setting. In external mode, start the external process first,
then use **Enable stimulation**; this action does not open a local stimulation
window. Changing the mode is a backend host configuration change followed by explicit
review and application. Startup failures are shown beside the corresponding controls.

Acquisition setup and Run share one frontend acquisition controller, including
backend-reported connection and reading status, pending commands, and command
errors. Switching pages does not create another connection state. The controller
refreshes acquisition, engine, and configuration status after both successful and
failed commands; failures do not automatically undo a completed engine startup or
connection. Connection and reading checks use configuration, generation, and
connection identity between requests. This frontend sequence is not an atomic
transaction across independently operating clients.

Signal Processing separates Decoder, Epoch boundary detector, and the ordered Filter chain. Each
filter instance selects its own plugin and profile. Browsing or saving another
profile preserves pending filter order; applying the chain is a separate action.

In **Stimulation → Trigger**, choose **None (no trigger output)** and review/apply
to run stimulation without hardware event output. This removes the trigger binding
without deleting its saved Profile; no new Profile is needed. Selecting None alone
does not change the active configuration. Stimulation still presents trials, but
does not send markers to an acquisition device or insert events into recorded data.
Selecting a hardware trigger again retains its platform and device checks.

For CURRY 8 with parallel TTL, select **Neuroscan CURRY 8 Parallel Trigger**,
create/save a Profile, then **Review and apply profile**. Set
`parallel_port_address` to the hexadecimal I/O base address reported by Windows
Device Manager; `0x3100` is a default, not automatic device detection. The
stimulation host needs a physical parallel port, a compatible Neuroscan trigger
cable connected to the amplifier, and an installed InpOut kernel driver and DLL
matching the Python process (`inpoutx64.dll` for 64-bit, `inpout32.dll` for 32-bit).
Make the DLL available to the Windows DLL loader, for example alongside the
Python executable. See the [InpOut installation instructions](https://www.highrez.co.uk/downloads/inpout32/).

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

Acquisition now uses `channel_count` in place of `channel_number`; it still
includes the final trigger/event row. Schema versions are unchanged. Existing
profiles and accepted activation bindings are not migrated automatically, and
the old parameter name is not supported. Rename the saved parameter and remove
the old key, then save and reapply the profile to replace its accepted binding.
If the old key remains in the UI, recreate and apply the profile. Update inline
acquisition bindings to use `channel_count` before applying them.

## Local storage

Storage shares the existing configuration root: `OMNIBCI_CONFIG_DIR` when set,
otherwise `platformdirs.user_config_path("OmniBCI", appauthor=False)`.

- `plugin-profiles/<uuid>.toml`: editable, named plugin parameters.
- `activation.toml`: accepted host settings and an ordered list of pinned slots.
- `.plugin-config.lock`: operating-system file lock for compare-and-swap writes.

Files are replaced atomically after flushing a same-directory temporary file.
The operating-system lock is released if a writer process exits. Legacy
`profiles/` and `active.toml` are not modified by this repository.

## Selection semantics

Bindings copy the selected profile's version, parameters, and schema version.
Editing a selected profile does not mutate the accepted copy. A new selection
must be explicitly accepted, with the previous activation revision. While the
engine runs, the runtime application coordinator commits acceptance after workers
acknowledge the change.

The runtime planning API also accepts an inline acquisition binding with
`slot: "acquisition.source"`, `selection`, and `parameters`. The Acquisition editor
uses it for the initial Review so the plan describes the exact displayed draft.
After final confirmation, it saves or reuses a Profile and replans with `profile_id`
before applying. Legacy inline bindings remain supported and persist in the startup
activation; the UI omits their synthetic Profile revision. Other module updates
preserve them. No backend contract or storage schema changes are required.

Fixed slots are `acquisition.source`, `processing.decoder`,
`processing.epoch_detector`, `stimulation.paradigm`, `stimulation.trigger`, and
`actuation.effector`. Filters use ordered `processing.filter.<uuid>` slots; the
same plugin or profile can appear in several independently identified slots.

The activation revision hashes its semantic content. Renaming/editing a profile
does not change an already accepted revision. Snapshot getters return copies,
so callers and plugins cannot mutate the stored snapshot through shared objects.
Profile metadata is excluded from the semantic hash; parameter values, selected
versions, host settings, slot identities, and filter order are included.

Profiles remain portable data. Platform compatibility is checked at execution;
a Windows-only capability may still be saved on another platform. Schema
`x-scope` metadata records the future portable/machine/secret boundary. No cloud,
account, synchronization, or secret export service is introduced.
