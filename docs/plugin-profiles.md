# Plugin profiles and accepted activation

`PluginProfileService` separates saved profiles from an accepted startup snapshot.
Profiles use format version 2 and identify an exact package, contribution, and
version. Each saved edit increments the profile revision. A stale revision is a
conflict and cannot overwrite a newer edit.

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
must be explicitly accepted, with the previous activation revision. The initial
service permits acceptance only while the engine is idle; the runtime application
coordinator will later commit acceptance after workers acknowledge the change.

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
