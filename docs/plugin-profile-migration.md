# Legacy plugin configuration migration

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

The pure helper `legacy_parameters(definition, settings_snapshot)` exposes the
same parameter-source mapping to legacy runtime adapters without accessing global
settings.

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
