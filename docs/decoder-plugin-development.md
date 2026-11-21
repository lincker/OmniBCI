# Write a Decoder plugin

This guide describes the implemented **Plugin API v1**. Open **Plugins → Decoder developer guide** in the console for this offline reference and the actual starter files. The executable [Oz Polarity Demo](../examples/decoder_plugin/) is validated, installed and executed in repository tests. It demonstrates the interface, not a validated BCI classifier.

For all seven plugin kinds and the source-development command, see [Develop an OmniBCI plugin](plugin-development.md). Install a Package, select its decoder Contribution and let Processing construct and own its Instance. Saving a Profile is optional and never starts an Instance.

## Files to provide

```text
my-decoder/
  manifest.toml          # Required at the project / ZIP root
  decoder.py             # Implementation named by the entrypoint
  default.schema.json    # Parameters, defaults and validation
  README.md              # Usage, model assumptions and validation notes
  assets/                # Optional packaged model resources
```

Only `manifest.toml` has a prescribed filename. The manifest references the other paths. A configurable Decoder requires a parameter schema; an empty closed object is sufficient when there are no parameters. No registry decorator, `setup.py` or plugin wheel is needed.

## Manifest and channel requirements

Copy the commented starter manifest, rename its publisher/package/contribution IDs, and retain `manifest_version = 2` with `package.api_version = "1"`. Package identity is case-sensitive `publisher.id`; package release and configuration-schema versions have separate meanings.

Declare `kind = "decoder"`, a package-relative `module:Class` entrypoint and `[contributions.channel_requirements]`. The requirements table is mandatory for a Decoder but may be empty. There is no separate signal-contract selector.

| Field | Meaning |
| --- | --- |
| `description` | Explain the model's assumptions; this text does not run validation. |
| `signal_type` | Optional type required for every final input channel, such as `eeg`. |
| `min_count` | Minimum signal-channel count after mapping, excluding the event row. |
| `required_names` | Required name subset; extra channels are allowed and order is not prescribed. |
| `default_names` | Ordered label suggestions for mapping. Names alone do not identify source rows. |

Names match the final mapped inputs exactly. An Acquisition channel is insufficient if the actual Processing mapping excludes it. A nonempty default selection must satisfy the minimum count and required names. Additional model assumptions about exact order, units or sample rate belong in constructor validation against `input_signal`.

Configuration owns `processing_input`: source selection, order, renamed outputs and declarations for unknown signal types. Do not duplicate those preferences in `parameters.input_channels`. An empty output list explicitly uses all source signal channels. Individual rows use exactly one `source_channel_id` or zero-based `source_channel_index`, excluding the event row. For example:

```json
{"name": "Oz", "source_channel_id": "channel-2", "signal_type": "eeg"}
```

Verify that the chosen source really corresponds to the intended electrode. Mapping selects/orders/renames rows; it does not change samples or turn a different electrode into Oz. A declared type can fill an unknown type but cannot override a known conflict. Preview and runtime preparation reject missing rows, duplicate source selection and incompatible layouts. Offline Profiles remain unverified until a source descriptor is available.

## Python contract

Import the canonical definitions from `omnibci.sdk`:

```python
from omnibci.sdk import Decoder, SignalInfo, Prediction
```

| Method | Required behavior |
| --- | --- |
| `__init__(self, *, parameters, resource_root, input_signal)` | Initialize local state and check mapped input assumptions. |
| `setup(self)` | Load reusable model resources once per binding. |
| `begin_epoch(self)` | Reset working state for every epoch, including repeated event IDs. |
| `should_decode(self, raw_window)` | Return a boolean readiness result on the accumulated window before filters. |
| `decode(self, window, *, latest_raw_segment)` | Return `Prediction` or `None`; both may update working state. |
| `teardown(self)` | Release resources, including after partially failed setup. |

All methods are serialized by the host. Implement explicit no-ops for lifecycle methods a stateless model does not need. There is no per-epoch cloning hook. Keep reusable model resources through epoch resets and acquire them during setup, not on every decode call.

`parameters` is an independent schema-validated copy with defaults. `resource_root` identifies the immutable package being executed. `input_signal` is the prepared mapped `SignalInfo`; use its ordered channels to locate matrix rows. The constructor receives no generic context, original Acquisition descriptor, revisions or server services.

Both arrays have shape **signal channels × samples**, without pre-trigger samples or an event row. `window` is post-trigger data accumulated so far after the optional ordered Filter chain; it can end before a closing marker. The readiness check sees the raw accumulated window before those filters.

`latest_raw_segment` is a required keyword containing only the most recently appended mapped epoch fragment, without epoch filters. It is not all data since the previous decode and does not promise complete incremental delivery. When no new fragment exists, the host supplies shape `(signal_channels, 0)`. `None` and an omitted argument are invalid. Whole-window decoders can ignore this separate fragment.

Do not modify borrowed arrays in place. `Prediction` ends collection of the current epoch; returning `None` continues collection. Targets are nonnegative integer values (including zero), not booleans or fractional values. Confidence is absent or finite in `[0, 1]`. The host retains event labels, forced status and trial/configuration identity; the decoder never receives a ground-truth event label.

A closing boundary can bypass readiness. Handle short and non-finite windows explicitly. Stream drain keeps the readiness gate and checks for pending data to avoid repeating a decode without new samples. The starter returns `None` for short or non-finite windows, otherwise target 1 or 2 from the sign of the mapped Oz mean.

Pre-trigger buffering is independent of baseline correction. A Filter receives the original reference explicitly as `pre_trigger_data`; Decoder input excludes it. Ordered Filters preserve shape, channel order, sample rate and units.

## Parameter schema and dependencies

Use the supported closed JSON Schema subset: object root, `additionalProperties: false`, declared fields and valid defaults. JSON descriptions belong in `description` fields. External references and unsupported keywords fail static validation. `x-scope` describes portability; `x-apply` suggests a boundary while the host decides when changes can apply.

Optional package/contribution `platforms` constrain `darwin`, `linux` and `win32`. `package.engines.omnibci` supports the documented numeric comparison subset, while `package.host_version` and `[package.requires]` check exact host/library versions. The host never installs dependencies from the plugin. Do not vendor `omnibci`, ship a virtual environment or expect build hooks to run.

## Validate, develop and install

From the repository root:

```sh
omnibci-plugin validate examples/decoder_plugin
omnibci-plugin package examples/decoder_plugin --output dist/oz-polarity-demo.zip
```

For a downloaded starter, substitute its directory. Output archives must be outside that directory. The shared tools preserve assets and verify deterministic package bytes without importing the implementation. Changed distributed bytes require a new package release version.

Use the complete current-configuration example in [the development workflow](plugin-development.md#develop-a-fixed-snapshot), with a real recording path:

```sh
omnibci-plugin develop examples/decoder_plugin --contribution oz-polarity --configuration /absolute/path/development.json --command-timeout 60
```

This connects the explicitly configured source and starts Processing through normal hosts in separate inventory/configuration. It does not start reading. The constructor sees the frozen snapshot's resources and prepared input. Press Ctrl+C, wait for confirmed cleanup, edit and restart; the same development version can produce a fresh snapshot. Unknown cleanup retains files and blocks restart.

For ordinary use, open **Plugins → Install ZIP…**, install and enable the exact version. In **Setup → Signal Processing → Decoder**, select the Contribution and edit current parameters and mapping. Saving a Profile is optional. Connect Acquisition, inspect metadata, preview the mapped input, start Processing and then start reading. Choose an epoch detector matching the recording's event semantics. Targets must agree with the selected paradigm/effector when those modules are used.

## Verify behavior

The starter tests cover static installation, mapped/reordered named inputs, missing required channels, short/forced/non-finite windows, and unchanged borrowed arrays. The release checks also run CNT File Reader → starter Decoder → Logger and correlate the acknowledgement to the source trial and host execution history.

For a stateful model, additionally test repeated epochs, returns of `None`, partial setup failure, teardown and candidate replacement. Use actual model validation data to assess accuracy; interface tests do not establish decoding quality. Measure runtime latency and validate equipment/display behavior separately as described in [validation](validation.md).
