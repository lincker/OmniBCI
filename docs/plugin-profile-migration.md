# Plugin configuration initialization and legacy import

## Startup paths

The composition root uses three paths:

1. If an accepted activation exists, reuse it without inspecting old profiles.
2. If no old Acquisition profiles or active ID exist, initialize from the
   resolved settings snapshot with `SettingsPluginConfigInitializer`.
3. Otherwise, import the old Acquisition profiles with
   `LegacyPluginConfigMigration`, then use the same initializer to publish the
   complete configuration.

An empty old profile directory does not prove a fresh installation. A user may
have customized only `settings.toml` or an application profile. Settings-based
initialization preserves those resolved values instead of substituting a new
set of plugin defaults:

```python
result = SettingsPluginConfigInitializer(profile_service).initialize(
    settings_snapshot=legacy_config_service.resolve_snapshot(),
)
```

The initializer does not depend on the old `ConfigProfile` model. The historical
importer validates and converts old profiles in memory before passing the new
profile drafts to it; no old profile file is rewritten.

`InitializationResult` contains `snapshot`, `warnings`, and `already_migrated`.
The historical field name is retained for compatibility and is true whenever an
accepted activation was reused. `MigrationResult` remains an alias for this same
result type. Generated UUIDs, `Migrated ...` names and the stored warnings key
also remain unchanged so interrupted initialization or import can resume.

## Importing old Acquisition profiles

`LegacyPluginConfigMigration` converts a resolved legacy settings snapshot and
existing Acquisition profiles into version 2 plugin profiles and one accepted
activation snapshot. The caller supplies data; the migration does not read or
modify legacy settings/profile files.

```python
result = LegacyPluginConfigMigration(profile_service).migrate(
    settings_snapshot=legacy_config_service.resolve_snapshot(),
    legacy_profiles=existing_acquisition_profiles,
    active_acquisition_id=selected_acquisition_profile_id,
)
```

`MigrationResult` contains `snapshot`, `warnings`, and `already_migrated`.
Warnings are also stored in `CONFIG.migration_warnings` within the accepted
snapshot, so they remain available after restarting the backend.

## Preserved behavior

- Legacy Acquisition profile UUIDs, names, and valid parameters are preserved.
  The selected profile must agree with the resolved settings snapshot.
- Imported profiles pass both legacy validation and the selected plugin's
  schema. Required legacy values are not repaired with new defaults.
- Acquisition and Trigger parameters come from the corresponding
  `ACQUISITION.<parameter>` settings.
- CCA retains `ssvep.duration` and `ssvep.offset_time`. Differences from
  `ALGORITHM.window_duration` or `ALGORITHM.offset_time` produce warnings; the
  migration does not switch CCA to previously unused values.
- FBCCA uses `ALGORITHM.window_duration` and `ALGORITHM.offset_time`.
- Missing scalar settings use declared plugin defaults. Supplied values,
  including zero, are validated and preserved rather than replaced by defaults.
- The filter list preserves order and repeated aliases. Each instance receives
  a deterministic UUID slot and an independent profile.
- Host settings and unrelated legacy keys remain in the accepted settings copy.
- Source, Decoder, Epoch Detector, and Paradigm aliases must be explicit. Empty
  Trigger and Effector aliases remain unselected.

The pure helpers in `omnibci.application.legacy_plugin_config` resolve legacy
aliases and map `legacy_parameters(definition, settings_snapshot)` without
accessing global settings or storage. Standalone runtime adapters use this
module without importing initialization or migration services. The previous
`plugin_config_migration` imports of `legacy_parameters` and
`PluginConfigMigrationError` remain aliases to the same objects.

## Exact identity and invalid data

Each alias is first resolved using the existing legacy resolver. The resulting
package, contribution, and version must then resolve exactly and provide a
configuration schema. Missing or ambiguous packages, missing versions, and
missing schemas block activation; another alias or version is not substituted.

A selected capability unavailable on the current operating system remains in
the portable configuration, with a warning. Runtime startup still performs its
own availability check.

An invalid active Acquisition profile blocks migration. An invalid inactive
profile is skipped with an identifying warning; its original file is retained.
Unknown profile parameters are never silently removed.

## Interrupted migration and retries

Settings initialization and historical import share the following write path:

1. Resolve and validate all intended bindings before writing profiles.
2. Preserve legacy profile IDs and use deterministic UUID5 IDs for generated
   profiles and filter instances.
3. Write every profile durably through the profile repository.
4. Write the accepted activation snapshot last, using compare-and-swap.

If writing fails, previously created valid profiles remain available and no
activation is published. Retrying with the same inputs reuses those profiles
only when their complete planned content matches, excluding the storage revision.
It does not overwrite edits to a partially migrated profile.

If an accepted activation already exists, migration returns it unchanged and
reports `already_migrated=True`. New legacy input does not implicitly replace a
configuration the user has already accepted.
