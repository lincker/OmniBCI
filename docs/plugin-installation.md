# Local plugin installation and lifecycle

## Storage

Managed packages use the existing user plugin directory. `OMNIBCI_PLUGIN_DIR`
overrides `PLUGINS.user_dir`; otherwise `platformdirs.user_data_dir("OmniBCI",
"OmniBCI") / "plugins"` chooses the operating-system data directory:

| Platform | Typical directory |
| --- | --- |
| macOS | `~/Library/Application Support/OmniBCI/plugins` |
| Linux | `~/.local/share/OmniBCI/plugins` (respects `XDG_DATA_HOME`) |
| Windows | `%LOCALAPPDATA%/OmniBCI/OmniBCI/plugins` |

The Windows path uses platformdirs' default local-data policy. The platformdirs
result is authoritative; applications should use the path provider instead of
constructing these examples.

```text
plugins/
  packages/<publisher.id>/<version>/   # Immutable extracted archive
    manifest.toml
    .omnibci-install.json              # Installer-generated digest receipt
  state/enabled.json                  # Atomically replaced enablement state
  state/.enabled.lock                 # Cross-process lifecycle lock
```

ZIP installation validates archive paths, extraction limits, manifest API and
platform compatibility, exact declared host-library requirements, and declarative
configuration schemas. It does not import plugin code or install Python
dependencies. Reinstalling the identical archive is idempotent; changed bytes for
an existing package/version are rejected. Installed versions remain disabled.
Installation receipts and executable package directories are local machine data;
portable user profiles stay in the configuration repository. Future synchronization
can transfer profile documents and exact package identities without copying
machine paths or executable code.

Legacy user plugin directories remain supported for existing development
workflows. Managed versions are discovered only through `state/enabled.json`.
This enablement rule applies to managed packages; it does not disable legacy
development plugins.

## Explicit lifecycle

1. Install a trusted local ZIP with a v2 root manifest.
2. Enable its exact package/version to make contributions selectable.
3. Create a profile, select its binding, and use the existing runtime Apply flow.
4. To change versions, preview a profile copy against the target version's schema.
   Existing parameters are all preserved by default. Removed or incompatible
   fields cause validation errors; users must explicitly supply replacement values.
   New schema defaults appear in the preview.
5. Save the reviewed copy as a new profile. The source profile and accepted/running
   bindings keep their original versions. Version changes, including rollback,
   require explicit selection and Apply.
6. Disable and then remove an unused version. Saved profiles, accepted bindings,
   running snapshots, and in-progress Apply references prevent disable/removal.

Multiple enabled versions can coexist and are resolved by exact package,
contribution, and version. Ambiguous legacy aliases fail instead of choosing an
arbitrary version. Enabling an exact identity already provided by another catalog
location is rejected.

Plugin enablement grants execution when that contribution is used; it is not a
sandbox. The caller must trust local ZIPs. Publisher text and SHA-256 checks alone
do not establish publisher identity. Signed repository trust is a separate layer
described in `plugin-market-format.md`.

## Application integration

Step 21 uses `DirectoryPluginPackageStore` and `PluginInstallationService`.
Step 22 uses `ManagedPluginPackageStore` and `PluginPackageService` with the
existing `PluginProfileService`. Bootstrap injects a runtime reference provider
and an **exclusive** admission guard shared with configuration Apply and commands.
For `RuntimeAdmission`, pass `admission.transition`. Profile writes must enter the
same admission system so they cannot race a reference check and removal. The
runtime provider should include both applied and pending transition snapshots;
`package_references_from_settings` adapts pinned `PLUGINS.bindings`.

Mount `plugin_packages.router`, and set `app.state.plugin_installation_service`.
For lifecycle operations, also mount `plugin_package_lifecycle.router`, set
`app.state.plugin_package_service`, and use that full service for inventory.

| Method and route | Operation |
| --- | --- |
| `GET /plugin-packages` | Installed versions with enablement and references |
| `POST /plugin-packages/install` | Bounded raw ZIP body; `application/zip` or `application/octet-stream` |
| `PUT /plugin-packages/{package_id}/{version}/enabled` | Explicit boolean enablement |
| `DELETE /plugin-packages/{package_id}/{version}` | Remove an unreferenced, disabled version |
| `POST /plugin-packages/profile-version-proposals` | Validate a version change without saving |
| `POST /plugin-packages/version-profiles` | Create a new profile after source-revision check |

The application's normal API prefix is omitted above. Installation uses bounded
streaming and disk staging, without multipart parsing or loading the complete
archive into memory. Every error leaves accepted and running bindings unchanged.
No account, cloud synchronization, automatic upgrade, or public marketplace is
configured by these components.
