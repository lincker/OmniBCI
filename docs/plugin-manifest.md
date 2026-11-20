# Plugin packages and contributions

This guide describes the implemented Plugin API v1. Use the [developer workflow](plugin-development.md) to validate, package and run a project, and the [API reference](plugin-api-v1.md) for role signatures.

A **Package** is the installation unit. A **Contribution** declares one selectable capability. An **Instance** is an object created by its owning module host. Plugin Management owns package files and enablement; Configuration owns parameter bindings; module hosts own execution and cleanup. Metadata discovery never imports plugin Python.

## Manifest version 2

```toml
manifest_version = 2

[package]
id = "device-kit"
publisher = "example"
version = "1.0.0"
api_version = "1"
domain = "acquisition"

[[contributions]]
id = "reader"
kind = "acquisition"
entrypoint = "driver:Reader"
display_name = "Example Reader"

[contributions.configuration]
schema = "reader.schema.json"
schema_version = 1

[[contributions]]
id = "trigger"
kind = "trigger"
entrypoint = "driver:Trigger"
display_name = "Example Trigger"

[contributions.configuration]
schema = "trigger.schema.json"
schema_version = 1
```

The manifest belongs at the package/ZIP root. This declaration illustrates a two-contribution package; its referenced Python implementations and schema files must also exist.

- `manifest_version = 2` selects the document format. `api_version = "1"` selects the only supported plugin contract. Package release and configuration-schema versions have separate meanings.
- `id`, `publisher`, `version`, `api_version` and `domain` are required. The case-sensitive package identity is `publisher.id`. Package/contribution IDs and publishers start with an ASCII letter or digit and otherwise use ASCII letters, digits, underscores and hyphens.
- Each Contribution requires a package-unique `id`, a supported `kind` and a package-relative `module:Class` entrypoint. Kinds are `acquisition`, `decoder`, `epoch_detector`, `filter`, `paradigm`, `trigger` and `effector`.
- Package `domain` is classification: `acquisition`, `processing`, `stimulation` or `actuation`. It does not restrict Contribution kinds or select their execution host. Different release versions may have different classifications.
- `plugin_type` is optional metadata, defaulting to the contribution ID. Configuration selects an exact package ID, contribution ID and version. No alias fallback or implicit latest version exists.
- Optional `display_name`, `description`, `params` and `controls` belong to a Contribution. `params` is display metadata; it does not replace a parameter schema or supply executable constructor arguments.
- `configuration` references a package-relative object schema and positive `schema_version`. Discovery can inspect a Contribution without it; normal configurable bindings require it. Use an empty closed object schema when there are no parameters.
- A Decoder requires `channel_requirements`, even if the table is empty. Other kinds cannot declare it. There is no separate `signal_input_contract` selector. See the [Decoder guide](decoder-plugin-development.md#manifest-and-channel-requirements).
- A Paradigm requires exactly `payload_schemas.trial` and `payload_schemas.result`; an Effector requires exactly `payload_schemas.command`. These reference separate runtime object schemas. Other kinds cannot declare `payload_schemas`.
- Optional `platforms` on the package and/or Contribution uses `darwin`, `linux`, `win32`. Both constraints must allow the host. Omission allows all three. Static inspection can show unavailable contributions; execution rejects them before import.

Unknown fields and invalid role declarations invalidate the whole manifest. Schema paths stay inside the package, exclude symlinks, and are bounded to 1 MiB. Publisher text is identity metadata, not proof of authorship. Origin, repository evidence, installed paths and environment fingerprints are host-owned records, never manifest assertions.

## Parameters and runtime payloads

Schemas use the [supported closed JSON Schema subset](../omnibci/application/plugin_schema.py). The root is an object and every object declares `additionalProperties: false`. Describe fields with `title` and `description`; JSON has no comments. External references and unrecognized keywords are rejected.

Constructor parameters receive schema defaults and an independent validated copy. Trial, result and command payloads are validated without inserting defaults. Declare all fields the plugin accepts; a missing or invalid payload must not silently choose another operation. Generic transport correlation stays outside these plugin payloads.

Decoder channel requirements describe final mapped inputs: optional `signal_type`, `min_count`, `required_names`, `default_names` and explanatory `description`. Actual channel selection/order/renaming belongs to Configuration's `processing_input`, not to the plugin parameter schema.

## SDK and construction API

Import supported contracts from `omnibci.sdk`, supplied by the selected OmniBCI host release. Do not bundle or vendor `omnibci`; the installer rejects that reserved namespace. The SDK is a thin facade over canonical contracts and has no separate runtime.

```python
# Acquisition, Paradigm, TriggerEmitter and Effector:
def __init__(self, *, parameters, resource_root): ...

# Decoder, Filter and EpochDetector:
def __init__(self, *, parameters, resource_root, input_signal): ...
```

All inputs are keyword-only. `parameters` belongs only to the selected Contribution. `resource_root` points to the fixed package actually executed. Processing's `input_signal` describes only its mapped signal rows; the event row is separate. Host revisions, package identity, original source descriptors, services and global settings are not constructor inputs. There is no `PluginContext`, old constructor dispatch or API-version fallback.

Construction initializes local state; the owning host performs preparation through each role's lifecycle. A Paradigm receives only `RenderSurface` and `TriggerOutput` in `setup`. The host retains window/event-loop/frame-submission ownership. Decoder setup/epoch reset/teardown and Effector setup/execute/quiesce/teardown are required even when some methods are no-ops. See [Plugin API v1](plugin-api-v1.md) for every role and value invariant.

## Signal-source trigger and resume semantics

A SignalSource declares fixed `trigger_mode` (`events` or `level`) and `resume_mode` (`restart-only`, `live-current` or `checkpoint`) before I/O. The host normalizes a held level once across chunk boundaries: `7,7,8,8` becomes `7,0,8,0`. Discrete File Reader events remain independent. Protocol extraction belongs to the driver; normalization belongs to Acquisition.

`observe()` returns a `SourceObservation`, retaining native text and diagnostics when the complete layout is unavailable. Connect precedes observe/start; observe and checkpoint operations run while stopped with no pending read. A valid `SignalInfo` describes signal channels separately from the final event row.

Checkpoint sources provide `SourceCheckpoint(resource_id=..., resource_revision=..., state=...)` for the next undelivered sample and stage it with `restore_checkpoint()`. File Reader validates the recording identity and cursor. Live-current sources resume present delivery without claiming gap-free capture. Restart-only sources cannot automatically replay an interrupted stream. These are role capabilities, not Profile parameters or optional legacy booleans.

## Discovery, packaging and installation

Ordinary discovery reads enabled managed packages and host-distributed built-ins. It never discovers mutable source projects by walking user category directories. Exact selection must identify one available Contribution/version; invalid/conflicting copies do not cause fallback to another implementation.

Use `omnibci-plugin validate PROJECT` and `omnibci-plugin package PROJECT --output ARCHIVE.zip`. Select the directory containing `manifest.toml`, not an enclosing repository. Outputs must be outside that directory. The tools preserve runtime resources, omit VCS/virtual environments/caches and produce deterministic ZIP bytes. They invoke no plugin Python, build hooks or dependency installer. Shared ZIPs and verified marketplace downloads use the same installer.

Installation checks portable paths, bounded extraction, schemas, entrypoint files, host/library constraints and immutable content. It does not enable a package or create an Instance. Enable the version, select a Contribution in its owning module, then use the normal Connect/Start action. Changed distributed bytes require a new package version; an identical archive retry is idempotent.

`omnibci-plugin develop` uses the same package format in an isolated snapshot and configuration, with fresh normal hosts. Companion packages are explicit archives or built-ins. Source edits affect only the next run. See the [developer workflow](plugin-development.md#develop-a-fixed-snapshot) for launch, stopping and recovery.

## Host and library requirements

Optional `package.engines.omnibci` supports comma-separated numeric version comparisons (`>=`, `>`, `<=`, `<`, `==`), for example `">=1,<2"`. This is not npm's range language. `package.host_version`, when present, requires an exact host version.

`[package.requires]` maps host-exposed Python distribution names to exact installed versions. These are checks, not installation instructions or dependencies on other plugin packages. There is no automatic pip, environment selector or inter-plugin dependency resolver. Use the selected host environment for author tests and deployment.
