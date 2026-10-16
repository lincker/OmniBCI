# Plugin configuration initialization

OmniBCI has not shipped. The retired Acquisition-only Profile format and its
`/config/modules/{module}` API are no longer supported or imported. The backend
and console use version 2 Plugin Profiles and accepted activation snapshots.
Old `active.toml` and `profiles/acquisition/` files are ignored and left untouched.

## Startup policy

1. Reuse an existing activation without inspecting old Profile files or replacing
   it with current settings.
2. When activation is absent, initialize Plugin Profiles from the captured saved
   host settings and packaged first-initialization plugin defaults. Retired named
   Application Profiles are never loaded. The user host file cannot override
   plugin parameters; subsequent changes belong in Plugin Profiles.
3. Validate all profiles before publishing an activation. Invalid plugin selections
   or parameters leave activation unset; the API server still starts.

```python
result = SettingsPluginConfigInitializer(profile_service).initialize(
    settings_snapshot=settings.saved_snapshot(),
)
```

`InitializationResult` contains `snapshot`, `warnings`, and
`already_initialized`. Warnings are persisted in `CONFIG.initialization_warnings`.
Generated profiles use `Initial ...` names and deterministic UUID5 identities.

Missing or corrupt retired files do not represent errors in the current format.
For example, an old `active.toml` pointing to a missing Acquisition Profile no
longer prevents initialization from valid settings. This is normal initialization,
not an attempt to reconstruct that old Profile.

## Values and identities

- Acquisition and Trigger parameters come from `ACQUISITION.<parameter>`.
- The initializer maps CCA's `ssvep.duration` and `ssvep.offset_time`; differences
  from the similarly named `ALGORITHM` defaults produce warnings.
- It maps FBCCA's `ALGORITHM.window_duration` and `ALGORITHM.offset_time`.
  At runtime both decoders use only their pinned parameters and source context.
- Missing scalar settings use declared plugin defaults. Supplied values are
  validated and preserved, including zero.
- Filter order and repeated aliases are retained with distinct instance IDs.
- Host settings and unrelated settings remain in the accepted snapshot.
- Source, Decoder, Epoch Detector and Paradigm aliases must be explicit. Empty
  Trigger and Effector aliases remain unselected.
- Each alias must resolve to an exact package, contribution and version with a
  configuration schema. Missing or ambiguous identities block initialization.
- Profiles can store capabilities unavailable on the current operating system.
  Activation requires locally used capabilities to be available; dormant output
  selections remain portable and initialization reports a warning for them.

## Shared activation validation

Initialization, direct activation, runtime review, application and engine startup
all use `PluginProfileService.validate_activation()`:

- Source, Decoder, Epoch Detector and Paradigm bindings are required.
- Enabled Actuation requires an Effector binding.
- Every binding must match its installed configuration schema and version, with
  all defaults resolved, including dormant output bindings.
- Acquisition and Signal Processing capabilities must support the current platform.
  The Effector must also be available when Actuation is enabled. Runtime changes
  additionally check outputs that are already active.
- Stimulation starts separately, so its dormant Paradigm/Trigger selections do
  not block engine startup or unrelated updates merely due to platform support.

Invalid direct activation requests return 422 without changing the accepted
snapshot. Invalid engine starts return 409 before constructing workers. Review
reports validation errors, and application rechecks under the runtime transition
guard before persisting configuration or pausing modules.

Saving an individual Profile validates its parameters and schema without
requiring a complete activation or local platform support.

The pure helpers in `omnibci.application.legacy_plugin_config` remain only for
explicit first-initialization mapping. Standalone processes and runtime loaders
require accepted bindings and do not fall back to raw aliases. Standalone nodes
currently need the complete pinned package/schema catalog; remote configuration
distribution and role-specific catalog validation are not implemented.

## Failure and recovery

Host service assembly reads host settings directly. Optional acquisition status
metadata uses an applied or accepted snapshot; absent or unreadable activation
returns unavailable metadata instead of falling back to unaccepted settings.
This keeps health, engine status and host configuration APIs available when
plugin initialization fails.

Starting the engine requires a readable, validated activation. Missing or invalid
activation returns HTTP 409 `ENGINE_NOT_READY` before constructing any worker
or starting an engine thread. Existing invalid activation files are not reset or
silently replaced. Configuration read endpoints report their specific storage or
validation error when such a file cannot be read.

When initialization failed and activation is absent, clients can create valid
Plugin Profiles, review a complete set of required bindings through
`/config/apply-plan`, then apply the plan. The engine can start without restarting
the backend. The current Web editor reviews one slot at a time and does not yet
provide a complete initial-configuration wizard; this recovery sequence is
currently available through the API.

These changes affect management and startup boundaries. They do not add work to
acquisition, processing, trial or rendering loops.

## Interrupted initialization

1. Resolve and validate all intended bindings before writing profiles.
2. Write every profile durably using deterministic identities.
3. Write activation last with compare-and-swap.

If writing fails, valid profiles remain available and no activation is published.
A retry reuses profiles only when their complete planned content matches,
excluding storage revision. It does not overwrite user edits to a partially
initialized profile. An existing activation is returned unchanged.
