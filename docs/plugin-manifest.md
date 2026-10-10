# Plugin packages and contributions

The catalog reads manifests without importing plugin Python. A package is an
installation unit; each contribution is an independently selectable capability
within that package. Loading executes only the selected contribution's entrypoint.

## Manifest version 2

```toml
manifest_version = 2

[package]
id = "device-kit"
publisher = "example"
version = "1.0.0"
api_version = "1"

[[contributions]]
id = "reader"
kind = "acquisition"
entrypoint = "driver:Reader"
plugin_type = "example_reader"
display_name = "Example Reader"

[[contributions]]
id = "trigger"
kind = "trigger"
entrypoint = "driver:Trigger"
display_name = "Example Trigger"
```

- `manifest_version` is the manifest format, separate from the package's code
  `version` and plugin `api_version`. Supported API versions are `1` and `2`.
- Package `id`, `publisher`, `version`, and `api_version` are required strings.
  Publisher and package ID form the case-sensitive identity `publisher.id`.
- Package and contribution IDs, and publishers, begin with an ASCII letter or
  digit and contain only ASCII letters, digits, underscores, and hyphens.
- Contributions require `id`, `kind`, and `entrypoint`. Contribution IDs are
  unique throughout the package, including across different kinds.
- Supported kinds are `acquisition`, `decoder`, `epoch_detector`, `filter`,
  `paradigm`, `trigger`, and `effector`.
- `plugin_type` is an optional legacy registry alias, defaulting to the
  contribution ID. With API `1`, the entrypoint class must expose that same
  `plugin_type` and implement the expected domain port.
- `entrypoint` names a Python class relative to the package directory, in
  `module:Class` form. Absolute paths and parent-directory traversal are invalid.
- Optional `display_name`, `description`, `params`, and `controls` belong to
  each contribution. `params` remains read-only display metadata; `controls`
  retains the existing trial-control convention. Neither is a Profile schema.
- `configuration = {schema = "default.schema.json", schema_version = 1}`
  references plugin-owned configuration data. The file must stay inside the
  package, cannot traverse symlinks, and is limited to 1 MiB. Its positive
  schema version is separate from the package and manifest versions.
  Discovery validates the [supported Schema subset](plugin-configuration-schema.md)
  and defaults without importing Python or resolving external references.
  API 2 construction validates parameters and inserts declared defaults before
  executing the selected plugin. A missing schema means no editable profile
  contract; legacy packages can still be discovered.
- Execution fields such as `kind`, `plugin_type`, and `entrypoint` must not
  appear at package level in v2. A malformed contribution invalidates the entire
  package; the catalog does not expose a partially valid package.
- Optional `platforms` on a package or contribution is a nonempty list drawn
  from `darwin`, `linux`, and `win32`. Both constraints must allow the host.
  Omission supports all hosts. Discovery remains static and exposes unavailable
  capabilities; loading rejects them before importing their implementation.

`source`, filesystem location, and search precedence come from configured roots.
Declaring a publisher does not verify the package's author or grant it trust.

## Discovery and exact selection

All configured category roots are searched for packages. A v2 package can expose
any supported kind regardless of its containing category directory. For example,
a package under `acquisition/` can provide both an acquisition reader and a trigger.
Overlapping roots do not duplicate the same physical package.
A category root may itself contain a manifest (for example, bundled filters).
Once a package is found, discovery does not recurse into its internal modules.

The infrastructure catalog provides:

```python
selection = PluginSelection(
    package_id="example.device-kit",
    contribution_id="reader",
    version="1.0.0",
)
descriptor = catalog.resolve_selection("acquisition", selection)
available = catalog.list_contributions("acquisition")
```

Exact selection never resolves through a registry alias or chooses another
version. Different publishers, contributions, and versions can coexist. Multiple
physical packages with the same identity and version are ambiguous, even if one
root has higher precedence. An invalid copy with that identity/version prevents
fallback to another copy. An invalid version field conservatively blocks that
package identity because the catalog cannot establish which version is broken.

These APIs establish the selection contract. Existing runtime settings and HTTP
lists continue to use the legacy alias interface until their migration steps.
This change does not install packages or select an active version automatically.

## Legacy compatibility

An omitted `manifest_version`, or `manifest_version = 1`, uses the existing
single-entry format. Its contribution ID is `default`. Existing publisher/author
fallbacks, IDs, optional versions, and legacy directory-category checks remain.
`version=None` selects an unversioned legacy package only; it never means latest.

`resolve(category, plugin_type)` delegates to `resolve_legacy`. Legacy resolution
and `list(category)` retain root precedence and alias collision rules. Legacy
non-Actuation manifests without entrypoints remain visible in the old list;
exact resolution and `list_contributions` require executable entrypoints.

Bundled packages now use v2 manifests. Scan45 declares acquisition and Windows
parallel trigger contributions independently. Epoch detection and baseline
correction also have explicit entrypoints. Classic40's metadata describes its
existing 240 stimulus frames at 60 Hz (four seconds); no rendering timing changed.
Profile storage and runtime selection wiring are separate steps.

## SDK and construction API

API `2` implementations import their ports from `omnibci.sdk` and accept exactly
the keyword inputs `parameters` and `context`. The host copies parameters and
supplies a frozen, pickle-safe `PluginContext` containing sample rate, channel
names, configuration revision, and the selected package's resource directory.
Context contains metadata, not global settings or live service objects.
The class implements its domain port; a registry decorator and `plugin_type`
attribute are no longer required. The manifest owns its selection identity.

Processing contributions that support prepared channel input declare
`signal_input_contract = 1` beside their contribution `kind`. Their context adds
`acquired_signal` and `input_signal`, both immutable `SourceMetadata` values.
`channel_names` retains the original acquired-channel meaning; the ordered
`input_signal.channels` describes the actual decoder/filter/detector input,
including units, references, and optional positions in meters. The final trigger
row remains outside channel arithmetic. Undeclared plugins retain the legacy
identity path and reject transformed input before streaming. The bundled CCA,
FBCCA, epoch detector, and baseline filter declare this contract.

API `1` keeps its existing constructor arguments and alias checks. Constructor
errors never cause a retry using a different API. All bundled contributions use
API `2`; direct legacy constructor calls remain compatible during migration.
Paradigms receive the selected trigger through `bind_trigger()` before setup.
Acquisition, Processing, Stimulation, and Actuation load only their selected
entrypoints. A missing pinned selection never falls back to a legacy alias.

### Signal-source resumption

`SignalSource` exposes two optional capabilities, both disabled by default:

- `supports_checkpoint`: `checkpoint()` captures the next undelivered sample
  after acquisition has stopped and its fetch task has joined.
  `restore_checkpoint(checkpoint)` stages that position for the next start.
  Checkpoints are opaque dictionaries; an optional `source_identity` containing
  string `resource_id` and `revision` fields distinguishes a different recording
  from changed content at the same path. FileReader implements this contract.
- `resume_without_checkpoint_safe`: a live driver explicitly guarantees that
  starting after stop or reconnect never seeks back or replays samples already
  delivered. Scan45 and Curry8 declare this capability because their start
  commands request live network delivery. It does not promise gap-free samples,
  uninterrupted trigger timing, or continuity across network failures.

Automatic module restart or graph rebuild requires a valid checkpoint or the
explicit live-source guarantee before it can resume an existing stream. An
unknown source remains paused with its old session open, and requires an explicit
engine stop before retrying the change. Decoder/filter trial-boundary changes do
not stop the source and do not need these resumption capabilities.

Headless tests cover capability receipts, command ordering, replay checkpoints,
and conservative rejection of unknown sources. Real amplifier/network pause and
reconnect behavior, including cancellation during partial packet reads, still
requires hardware validation before relying on timing or sample continuity.
