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

`latest_samples` is required and contains only the most recently appended epoch
fragment after mapping, without epoch filters. It is not all
samples received since the previous decode. Pipeline callers must supply an
empty `(signal_channels, 0)` matrix when no new fragment exists; omission and
`None` are rejected. This whole-window example ignores the fragment and does not
modify either input in place. Stateful implementations must isolate working state
with `for_new_epoch()` and remember that returning `None` may still update state.
Stream drain retains the readiness gate and checks for pending samples before
attempting another decode.

Keep these three files together: `manifest.toml`, `decoder.py`, and
`default.schema.json`. This README is optional. Only `manifest.toml` has a fixed
filename; the other paths are declared in it. Rename the package publisher/id
and contribution id when creating your own plugin.

From this directory, create the archive with Python's standard library:

```bash
python -m zipfile -c ../oz-polarity-demo.zip manifest.toml decoder.py default.schema.json README.md
```

The manifest must be at the ZIP root, without an enclosing directory. In OmniBCI,
open **Extensions → Install ZIP…**, install the archive, and enable its version.
In **Setup → Signal Processing → Decoder**, create a profile for **Oz Polarity Demo**.
Choose an Acquisition whose named EEG channels include `Oz`, preview the input,
select the Decoder profile, and apply the configuration. Use a suitable epoch
detector. Targets `1` and `2` must also agree with your paradigm and effector if
those modules are used. The example adds no paradigm, detector, or filter.

The host must support Plugin API `2` and signal input contract `2`. NumPy is
already a host dependency; package installation never runs pip or installs
additional dependencies. The decoder uses no external assets or model files.

The full authoring guide is available offline in **Extensions → Decoder developer
guide**, and in the repository at `docs/decoder-plugin-development.md`.
