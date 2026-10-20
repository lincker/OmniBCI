# Plugin configuration

OmniBCI uses version 2 Plugin Profiles and accepted activation snapshots. Each
subsystem selects its own plugins and profiles. Saving or applying Acquisition
configuration does not require a Decoder, Epoch boundary detector, Paradigm, or
Trigger selection.

## Startup policy

The backend starts configuration management without creating or selecting plugin
profiles. Operators select a plugin, create or choose a profile, and apply it in
the corresponding subsystem. Plugin parameters come from that profile and the
plugin's declared defaults.

Existing accepted bindings remain unchanged across backend restarts. Editing a
saved profile does not update its accepted binding until the operator applies
that profile again. Absent or invalid activation never causes startup to import
old settings or replace user data.

The retired Acquisition-only Profile format, `active.toml`, named Application
Profiles, and old plugin aliases in host settings are not imported. The automatic
whole-system initializer and its legacy parameter mapper have been removed.
Old files remain untouched.

## Module boundaries

Saving a profile validates that plugin's parameters and configuration version.
Applying a module's selections validates the affected module. Other modules can
remain unconfigured. Runtime startup checks the capabilities needed by the
operation being started; data compatibility checks belong at the boundary where
the consuming module uses source data.

Source channel metadata belongs to Acquisition. Decoder channel mapping and
reference configuration belong to Signal Processing. Retired global
`SIGNAL.setup` settings are not executable configuration and cannot block an
Acquisition connection. Old profile payloads remain readable for explicit repair;
they are not silently migrated into channel mappings.

## Failure and recovery

Host services use host settings directly. Optional acquisition status metadata
uses an applied or accepted source binding. Missing or unreadable activation
returns unavailable metadata, keeping health, engine status, plugin catalog,
and configuration APIs available.

When no configuration has been applied, select the Acquisition plugin and apply
its source profile before connecting. Signal Processing and Stimulation can be
configured separately when needed. Existing invalid configuration files are not
reset or silently replaced; configuration endpoints report their storage or
validation error so the operator can repair them explicitly.
