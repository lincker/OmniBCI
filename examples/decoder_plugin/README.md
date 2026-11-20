# Oz Polarity Demo

This executable tutorial reads the mapped `Oz` row and returns target `1` when
its window mean is nonnegative, otherwise target `2`. This arbitrary rule
demonstrates the plugin interface; it is not a validated BCI decoding method.
`Oz` is a deliberate requirement of this example, not a general requirement
of CCA, FBCCA, or every decoder.

The decoder receives the accumulated post-trigger `window` after the epoch
Filter chain, without pre-trigger data or a trigger row. Decoding can happen
before an end marker; the host's `should_decode` check sees the window before
filtering, and a closing boundary can bypass that check. This example returns
`None` for short or non-finite input and otherwise returns a `Prediction`, which
ends collection of the current epoch.

`latest_raw_segment` is a required keyword and contains only the most recently appended epoch
fragment after mapping, without epoch filters. It is not all
samples received since the previous decode. Pipeline callers must supply an
empty `(signal_channels, 0)` matrix when no new fragment exists; omission and
`None` are rejected. This whole-window example ignores the fragment and does not
modify either input in place. Stateful implementations reset working state in
`begin_epoch()` and remember that returning `None` may still update state.
The host calls `setup()` once per binding and `teardown()` when it releases that
binding; this example implements both as no-ops.
Stream drain retains the readiness gate and checks for pending samples before
attempting another decode.

Keep these three files together: `manifest.toml`, `decoder.py`, and
`default.schema.json`. This README is optional. Only `manifest.toml` has a fixed
filename; the other paths are declared in it. Rename the package publisher/id
and contribution id when creating your own plugin.

From this directory, validate and package with the host tooling:

```bash
omnibci-plugin validate .
omnibci-plugin package . --output ../oz-polarity-demo.zip
```

The manifest must be at the ZIP root, without an enclosing directory. In OmniBCI,
open **Plugins → Install ZIP…**, install the archive, and enable its version.
In **Setup → Signal Processing → Decoder**, select **Oz Polarity Demo** and edit its current parameters and mapping. Saving a Profile is optional.
Choose an Acquisition whose named EEG channels include `Oz`, preview the input,
and start Processing to apply the configuration. Use a suitable epoch
detector. Targets `1` and `2` must also agree with your paradigm and effector if
those modules are used. The example adds no paradigm, detector, or filter.

The host must support Plugin API `1`. Decoder channel requirements are declared
directly, without a separate signal-contract selector. NumPy is
already a host dependency; package installation never runs pip or installs
additional dependencies. The decoder uses no external assets or model files.

For an isolated source run, use `omnibci-plugin develop . --contribution oz-polarity --configuration /absolute/path/development.json --command-timeout 60` with an explicit Acquisition companion. The [developer workflow](../../docs/plugin-development.md#develop-a-fixed-snapshot) includes the complete configuration shape. Source edits take effect after confirmed stop and a fresh launch; the same development version can be reused. Ordinary distributed releases remain immutable.

See the [Decoder guide](../../docs/decoder-plugin-development.md) for the implemented API v1 lifecycle, input semantics and verification steps.
