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

Acquisition first offers **Use saved profile** or **New profile**. A saved profile
retains its experiment baseline; Get Metadata shows missing or different source
parameters in red. A new profile starts as an unsaved draft: choose its plugin,
enter the address/port or recording path, and Connect. For Curry8 and File Reader,
Get Metadata automatically fills the observed sample rate and total output rows
into that draft. **Save profile** persists it; Connect then applies the saved
baseline for comparison. Changing the draft's connection parameters clears its
previously observed dimensions.

**New profile** starts with the selected plugin's defaults. **Duplicate profile**
saves an independent named copy of the displayed parameters. Browsing another
plugin or profile does not select it for execution, and **Save profile** only
persists its values. Use **Review and apply profile** to change the selected
configuration. The module summary distinguishes the configuration selected for
the next start from a running configuration whose revision has been acknowledged.

Signal Processing separates Decoder, Epoch boundary detector, and the ordered Filter chain. Each
filter instance selects its own plugin and profile. Browsing or saving another
profile preserves pending filter order; applying the chain is a separate action.

For Scan45, `channel_number` is the total number of channels in each incoming
sample, including the final event channel. The default is 10 (9 EEG + 1 event),
and any integer of at least 2 is accepted. For 32 EEG channels and one event
channel, enter 33. This value must match the device's stream; the driver does not
automatically discover its channel layout.

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
`slot: "acquisition.source"`, `selection`, and `parameters`. This pins connection
configuration in the activation without creating or updating a named Profile.
The binding has no `profile_id` and uses `profile_revision: 0`. Other module
updates preserve it. The direct activation endpoint still accepts saved Profile
references only. New Curry8/File drafts omit expected dimensions from this
binding until the user saves and applies the completed Profile.

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
