# Write a Decoder plugin

Open **Extensions → Decoder developer guide** in the web console or desktop app
for an offline overview, commented manifest, and downloadable starter files.
The executable starter lives in `examples/decoder_plugin/`; the app displays
those same files rather than maintaining a separate copy of the code.

## Files to provide

```text
my-decoder/
  manifest.toml          # Required, at the package / ZIP root
  decoder.py             # Required implementation, referenced by entrypoint
  default.schema.json    # Parameter schema for editable UI Profiles
  README.md              # Recommended: usage, training and validation notes
  assets/                # Optional: model weights or other package resources
```

Only `manifest.toml` has a prescribed filename. The Python module and schema may
have other names if `entrypoint` and `configuration.schema` reference them.
Discovery permits a missing schema for legacy plugins, but a configurable
Decoder needs a schema to participate in the UI Profile workflow. A decoder
with no editable parameters can provide an empty object schema with
`"properties": {}` and `"additionalProperties": false`.
Single-file entrypoints do not require `__init__.py`, `setup.py`, or a wheel.

## Manifest and channel requirements

Start from the fully commented `examples/decoder_plugin/manifest.toml`.
`manifest_version = 2` describes the document format; `package.api_version = "2"`
selects the constructor API. They are separate from the package release version,
configuration schema version, and `signal_input_contract` capability version.
Package identity is the case-sensitive `publisher.id`; each contribution has an
id unique within that package. Declare `kind = "decoder"` and an entrypoint in
package-relative `module:Class` form. No registry decorator is needed with API 2.

A Decoder that declares `[contributions.channel_requirements]` must also declare
`signal_input_contract = 2` on that contribution. Older hosts reject this
capability instead of silently ignoring its constraints. Contract `1` describes
prepared input metadata without the declarative requirements. Plugins without a
requirements section retain their existing behavior.

| Field | Meaning |
| --- | --- |
| `description` | Explanation of the algorithm's input assumptions; displayed, not executed. |
| `signal_type` | Optional required signal type for every final input channel, for example `eeg`. Omit to impose no type restriction. |
| `min_count` | Positive integer minimum for the final input's signal-channel count. The trigger row does not count. |
| `required_names` | Mandatory channel names, as an unordered subset. Extra channels are allowed. An empty list imposes no named-channel requirement. |
| `default_names` | Ordered suggestions for output names. The user binds them to Acquisition channel IDs; names alone do not establish a mapping. |

Channel names must match the final mapped input names, including spelling and
case. A nonempty default selection must satisfy the minimum count and contain
the mandatory names. It does not establish an exact-order requirement.
An Acquisition containing a channel is insufficient if the Decoder Profile
does not include it in the final input.

The declaration is read-only algorithm metadata. The user's actual selection,
order, output names, and unknown-type declarations remain in `processing_input`
on the Decoder Profile. The live Acquisition owns its normalized descriptor;
raw source metadata remains available separately. Do not add a
second `parameters.input_channels` setting for the same mapping. Existing
Profiles are not overwritten by updated defaults or an Acquisition switch;
preview and runtime preparation validate their resolved input. Offline Profiles
can be saved before an Acquisition is available.

In the Signal Processing binding editor, Acquisition channels are on the left and
ordered algorithm inputs are on the right. Users can select all channels or bind
individual sources, including an explicit different-name binding such as
`{"name": "Oz", "source_channel_id": "channel-0", "signal_type": "eeg"}`.
The optional type declaration fills an unknown Acquisition type and rejects a
conflict with a known type. It does not edit the Acquisition descriptor. Mapping
only selects, orders, and renames signal rows; it preserves sample amplitudes and
the trigger row.
The bound signal enters Epoching and pre-trigger buffering first. Optional
ordered filters transform the assembled window before Decoder execution.

**Epoching** names the segmentation operation; **Epoch** names its data object.
`Epoch.pre_trigger_data` and `Epoch.has_pre_trigger_data` describe captured
history, independently of whether baseline correction is enabled. The data
layout is `[pre-trigger data | window]`, and the full duration includes both.

For a model whose full channel order or units must be exact, validate
those additional assumptions in its constructor using `context.input_signal`.
`required_names` alone does not express them. Publish only requirements that the
algorithm actually needs; the tutorial's `Oz` requirement is an arbitrary
example, not an electrode recommendation for other algorithms.

## Python contract

Import public contracts from `omnibci.sdk`:

```python
from omnibci.sdk import Decoder, PluginContext, PluginParameters, Prediction
```

The entrypoint subclasses `Decoder` and accepts exactly the API 2 keyword inputs
`parameters` and `context` in `__init__`. The host validates parameters against
the JSON Schema, fills declared defaults, and passes an independent copy.
Read packaged resources relative to `context.resource_root`; do not rely on
the process working directory or import backend settings and service objects.

| Method | Required behavior |
| --- | --- |
| `__init__(self, *, parameters, context)` | Initialize the model and verify any additional input assumptions. |
| `should_decode(self, window) -> bool` | Cheap readiness check on the accumulated window before filters run. |
| `decode(self, window, latest_samples) -> Prediction \| None` | Produce an integer target id, optionally a confidence value, or return no prediction. Either outcome may update the decoder's working state. |
| `for_new_epoch(self)` | Optional for stateless decoders; stateful decoders must return an instance with isolated per-epoch working state. |

`context.input_signal` is a `SignalInfo` describing the ordered
mapped input, including channel IDs, names, types, and units. Use its channels to
resolve names to matrix row indices. `context.channel_names` and
`context.acquired_signal` describe the original Acquisition, before mapping.
Never use their indices for mapped input.

Decoder arrays have shape **signal channels × samples**, without a trigger row
or pre-trigger data. `window` contains the post-trigger data accumulated up to
this call, after the ordered epoch Filter chain. It need not extend to an end
marker: the host can attempt decoding while collection is still in progress.
The host calls `should_decode` on the accumulated window **before** those filters.

`latest_samples` is the most recently appended epoch fragment. Upstream channel
mapping has already run, but this separate input does not
pass through the epoch Filter chain. It does not accumulate all samples received
since the previous decode, so it is not a complete incremental-delivery contract.
A whole-window decoder can ignore it. Every `ProcessingPipeline.run` call must
supply `latest_samples` explicitly; omission and `None` are rejected. When there
is no new fragment, pass an empty matrix with shape `(signal_channels, 0)`.
There is no fallback to the full window.

Do not modify either input array in place. A `decode` call that returns `None`
may still have updated internal state; `None` means only that no prediction was
produced. Keep mutable state isolated per epoch with `for_new_epoch()`.
Complete incremental delivery would require a separate contract.

Pre-trigger segments go to filters separately through `FilterContext.baseline`.
Capturing these samples does not enable baseline correction.
`BaselineCorrection` uses the entire supplied segment as its
reference interval and subtracts its per-channel mean from the window.
Filters used with named input must
preserve the declared channel layout; do not silently reinterpret row indices.
At a closing boundary, `force_decode_at_boundary()` bypasses `should_decode`, so
handle short windows inside `decode` as the starter does. Stream drain uses
`decode_if_ready()`: it retains the readiness gate and checks for pending data to
avoid another decode when no samples have been appended since the last attempt.
Under the current collection lifecycle, a returned `Prediction` completes the
trial and ends epoch collection; returning `None` does not guarantee collection
will continue when a boundary or drain is closing it.

A non-None result must be a `Prediction`. Its `target_id` must be a Python integer
or NumPy integer scalar, excluding booleans; floats (including `1.0`), strings,
and arrays are rejected before conversion. `Classification` enforces nonnegative
target ids and confidence in `[0, 1]` when supplied. Invalid outputs fail with the
decoder class, event id, and configuration revision in the error context.

Target ids must agree with the chosen paradigm and effector. A Decoder package
does not supply an epoch boundary detector, filter or paradigm unless it declares those
as additional contributions. Keep decoding independent of device connections,
GUI components and filesystem writes. Load model weights during construction
rather than on every window, and test new-epoch isolation for stateful models.

## Parameter schema

Use the project's supported JSON Schema subset. Each object declares
`additionalProperties: false`; the root is an object. Declare a type, a readable
title/description, a default and any bounds for each configurable parameter.
Defaults must satisfy the schema. JSON has no comments; use `description` fields
to explain parameter semantics. `x-scope` describes portability and `x-apply`
suggests a reconfiguration boundary; the host decides when a change can apply.

Do not use `$schema`, `$ref` or arbitrary JSON Schema features: unsupported
keywords are rejected. See the repository document
`docs/plugin-configuration-schema.md` for the full supported subset.

## Compatibility and packaging

Use `package.platforms` only when the plugin actually restricts supported OSes:
`darwin`, `linux`, and `win32`. Omission allows all supported hosts. Optional
`package.host_version` is an exact OmniBCI version, not a version range.
Optional `[package.requires]` maps installed host distribution names to exact
versions; it is not `[requirements]` and does not accept pip-style ranges.
The installer currently checks the host's exposed library inventory, including
NumPy, scikit-learn and MNE. It never installs Python dependencies. Do not
ship a virtual environment or a `requirements.txt` expecting it to run.

Create a ZIP from **inside** the starter directory using the standard library:

```bash
python -m zipfile -c ../oz-polarity-demo.zip manifest.toml decoder.py default.schema.json README.md
```

Include your `assets` directory in the command if the implementation needs it.
The ZIP must contain `manifest.toml` at its root, without an enclosing folder.
Change `package.publisher`, `package.id`, the contribution id, and display text
for your plugin. Increment `package.version` whenever distributed bytes change;
an installed package/version is immutable. Schema version is separate.

## Install and validate

1. Open **Extensions → Install ZIP…**, select the archive, then enable that exact
   package version. Installation checks metadata and schemas without importing
   Python; successful installation alone does not prove the algorithm executes.
2. In **Setup → Signal Processing → Decoder**, create a Profile for the contribution.
   Review algorithm parameters and the initialized input selection.
3. Select the Acquisition Profile, refresh source information and preview input.
   Check channel IDs, output names, order, units and types. Missing required channels,
   too few channels or the wrong signal type must prevent runtime preparation.
4. Select and apply the Decoder Profile. Start with a known replay file and a
   suitable epoch boundary detector; verify expected targets before using live data.
5. Test reordered source/input channels, a missing required channel, short and
   non-finite windows, repeated epochs and supported OSes. Compare against known
   offline outputs from your real model.

The repository's `tests/infrastructure/test_decoder_plugin_example.py` installs
the real starter archive, discovers and constructs it, and checks predictions
against reordered named input and short windows. From the repository root:

```bash
python -m pytest tests/infrastructure/test_decoder_plugin_example.py
```

The tutorial deliberately uses a mean-sign rule, not a validated classifier.
Its files are a starting point for implementing and validating your own model.

Related repository documents: `docs/plugin-manifest.md`,
`docs/plugin-configuration-schema.md`, `docs/plugin-installation.md`, and
`docs/plugin-profiles.md`.
