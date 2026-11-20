# Plugin packages and contributions

This guide is a historical API 2 snapshot, not current developer guidance.
The implementation now selects API 1 and uses explicit constructor inputs;
remaining manifest and payload changes are tracked in the [implementation plan](plugin-implementation-plan.md).
The complete guide will be rewritten after the coordinated refactor.
See [Plugin API v1](plugin-api-v1.md) for the target contract.

Use the [canonical plugin terminology](plugin-marketplace-design.md#31-canonical-plugin-terminology):
a Plugin Package is the installation unit, a Contribution is a static declaration
of a selectable capability, and an Instance is its runtime object. The catalog
reads declarations without importing plugin Python. The owning Module Host loads
the selected entry point and constructs an Instance.

For required Decoder files, an annotated starter, packaging and testing, see
[Write a Decoder plugin](decoder-plugin-development.md). The same guide and
starter files are available offline on the plugin management page, currently
labeled **Extensions**, under **Decoder developer guide**. The agreed target
page name is **Plugins**.

## Manifest version 2

```toml
manifest_version = 2

[package]
id = "device-kit"
publisher = "example"
version = "1.0.0"
api_version = "2"
domain = "acquisition"

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
  `version` and plugin `api_version`. Only API version `2` is supported.
- Package `id`, `publisher`, `version`, `api_version`, and `domain` are required strings.
  Publisher and package ID form the case-sensitive identity `publisher.id`.
- Package and contribution IDs, and publishers, begin with an ASCII letter or
  digit and contain only ASCII letters, digits, underscores, and hyphens.
- Contributions require `id`, `kind`, and `entrypoint`. Contribution IDs are
  unique throughout the package, including across different kinds.
- Supported kinds are `acquisition`, `decoder`, `epoch_detector`, `filter`,
  `paradigm`, `trigger`, and `effector`.
- `plugin_type` is optional display/runtime source metadata, defaulting to the
  contribution ID. Selection always uses the exact package/contribution/version;
  a registry alias is never a fallback.
- `entrypoint` names a Python class relative to the package directory, in
  `module:Class` form. Absolute paths and parent-directory traversal are invalid.
- Optional `display_name`, `description`, `params`, and `controls` belong to
  each contribution. `params` remains read-only display metadata; `controls`
  retains the existing trial-control convention. Neither is a Profile schema.
- `configuration = {schema = "default.schema.json", schema_version = 1}`
  references plugin-owned configuration data. The file must stay inside the
  package, cannot traverse symlinks, and is limited to 1 MiB. Its positive
  schema version is separate from the package and manifest versions.
  Discovery validates the [supported Schema subset](../omnibci/application/plugin_schema.py)
  and defaults without importing Python or resolving external references.
  API 2 construction validates parameters and inserts declared defaults before
  executing the selected plugin. A missing schema means no editable profile
  contract.
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

All configured category roots are searched for packages. A package declares one
immutable domain: `acquisition`, `processing`, `stimulation`, or `actuation`.
Contributions must belong to that domain, with one explicit exception: acquisition
packages may also ship Trigger contributions consumed by Stimulation. Different
versions of one package cannot change its domain.
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

Only manifest version 2, API version 2, and explicit exact package versions are
accepted. There is no legacy manifest reader, alias resolver, or implicit latest
version selection. Installation and enablement remain explicit operations.

## Compatibility and dependencies

`[package] engines = { omnibci = ">=0.1,<1" }` optionally constrains the host at
installation and catalog discovery. Supported clauses are comma-separated
`>=`, `>`, `<=`, `<`, and `==` comparisons over numeric dot-separated versions.
This deliberately small syntax does not implement npm ranges. The existing
`host_version` field still requires an exact host version when present.

`[package.requires]` maps Python library distribution names to exact installed
versions. The installer checks these requirements; it does not install libraries.
This field is not a list of OmniBCI plugin packages. No inter-plugin dependency
resolver is introduced. Multiple contributions inside one package need no
inter-package dependency declaration.

## SDK and construction API

API `2` implementations import their ports from `omnibci.sdk` and accept exactly
the keyword inputs `parameters` and `context`. The host copies parameters and
supplies a frozen, pickle-safe `PluginContext` containing sample rate, prepared
signal descriptors, `module_revision`, and the selected package's resource directory.
The revision belongs to the consuming module, including a Trigger shipped in
an acquisition-domain package and loaded by Stimulation.
Context contains metadata, not global settings or live service objects.
The class implements its domain port. The manifest owns its selection identity
and the entrypoint used to load it.

Signal Processing contributions must declare
`signal_input_contract = 1` beside their contribution `kind`. Decoder
contributions can declare `signal_input_contract = 2` together with
`[contributions.channel_requirements]` to add minimum count, signal type and
required names, plus ordered name suggestions for channel mapping. Contract 2 requires that
section and is currently Decoder-only. Missing or unsupported declarations are
rejected during manifest discovery. The
[Decoder authoring guide](decoder-plugin-development.md#manifest-and-channel-requirements)
describes every field, with comments in the executable starter manifest.

Both prepared input contracts add `acquired_signal` and `input_signal` to the
context, as immutable `SignalInfo` values.
Use `acquired_signal.channels` for the original Acquisition channels and the
ordered `input_signal.channels` for the actual decoder/filter/detector input,
including source channel IDs, signal types and optional units. The final trigger
row remains separate. Channel mapping selects and orders rows without changing
sample values. The bundled CCA
and FBCCA declare contract 2 with flexible EEG input and no mandatory electrode
names. The bundled epoch boundary detector and baseline-correction filter retain contract 1.

Constructor errors never cause a retry using a different API. All bundled
contributions use API `2`; API `1` construction is removed.
Paradigms receive the selected trigger through `bind_trigger()` before setup.
Acquisition, Signal Processing, Stimulation, and Actuation load only their selected
entrypoints. A missing pinned selection never falls back to a legacy alias.

### Signal-source trigger semantics

`SignalSource.trigger_is_level` is an optional Python capability, defaulting to
`False`. Return `True` only when the final event row contains codes held over
consecutive samples. Scan45 and Curry8 declare it. The host then keeps the first
sample of each nonzero code and clears immediately repeated values, preserving
code changes: `7,7,8,8` becomes `7,0,8,0`. This happens after protocol decoding
and before processing-queue and acquisition-preview publication.

Discrete event sources, including File Reader's CNT annotations, leave this
capability false so adjacent independent events remain intact. Undeclared
sources retain their existing values. The capability is not a manifest field,
Profile parameter, or inference from `SignalInfo.native_event`; byte extraction
and device protocol interpretation remain the source plugin's responsibility.

Host preprocessing state spans chunks within one read. It resets when a stopped
stream starts again or a new connection is established, preserving the first
nonzero sample after that boundary. An already-running Start is idempotent and
does not reset it. These resets also apply when a source restores a checkpoint;
the host does not extend the source checkpoint format with preprocessing state.
Discrete File Reader events are unaffected.

### Signal-source resumption

`SignalSource` exposes two optional resumption capabilities, both disabled by default:

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
unknown source remains paused and requires an explicit engine stop before
retrying the change. Decoder/filter trial-boundary changes do
not stop the source and do not need these resumption capabilities.

Headless tests cover capability receipts, command ordering, replay checkpoints,
and conservative rejection of unknown sources. Real amplifier/network pause and
reconnect behavior, including cancellation during partial packet reads, still
requires hardware validation before relying on timing or sample continuity.
