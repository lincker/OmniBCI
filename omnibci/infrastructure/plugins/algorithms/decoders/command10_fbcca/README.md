# 10-command SSVEP FBCCA

This standalone package provides one training-free `decoder` contribution for
`OmniBCI.command10SSVEP` / `default` / `0.1.0`. It implements Plugin API v1 with
manifest version 1. Its own selection is
`OmniBCI.command10_fbcca` / `default` / `0.1.0`.

The package contains its own protocol table and imports only the public
`omnibci.sdk`, NumPy, SciPy and local modules. Installing the paradigm is not
required to load the decoder or analyze recorded data. The two independent
packages carry identical `targets.py` files, enforced by a repository test.

## Matching target protocol

| ID | Label | Row | Column | Frequency (Hz) | Initial phase (rad) |
| --- | --- | --- | --- | --- | --- |
| 1 | Up arrow | 1 | 3 | 8.0 | 0 |
| 2 | Down arrow | 3 | 3 | 8.8 | pi/2 |
| 3 | Left arrow | 2 | 2 | 9.6 | pi |
| 4 | Right arrow | 2 | 4 | 10.4 | 3pi/2 |
| 5 | A | 1 | 1 | 11.2 | 0 |
| 6 | X | 1 | 5 | 12.0 | pi/2 |
| 7 | B | 2 | 1 | 12.8 | pi |
| 8 | Y | 2 | 5 | 13.6 | 3pi/2 |
| 9 | C | 3 | 1 | 14.4 | 0 |
| 10 | Z | 3 | 5 | 15.2 | pi/2 |

```text
A            Up            X

B      Left  Feedback Right Y

C            Down          Z
```

The paired display uses **60 Hz**, with a four-second simultaneous stimulus.
This is distinct from the EEG sample rate, which comes from the host's prepared
`SignalInfo`. EEG sampling must be **at least 200 Hz** for this fixed filter
bank and its five harmonics. Tested synthetic rates are 200, 250, 500 and 1000 Hz.

The decoder consumes mapped EEG rows only, with no trigger row. Select suitable
posterior channels such as O1, Oz, O2 or parieto-occipital channels through
Processing's input mapping. At least one EEG channel is required; electrode
names and row order are not hard-coded. Blank default channel names retain the
host's all-source-channel default, so review the mapping before use.

## Analysis and phase semantics

The implementation uses filter-bank canonical correlation analysis, following
the [FBCCA approach of Chen et al. (2015)](https://doi.org/10.1088/1741-2560/12/4/046008).
This package's three-bank configuration and rejection thresholds are local
implementation choices, not a reproduction of that paper's reported accuracy.

1. Take a fixed post-trigger analysis window, skipping the configured offset.
2. Remove per-channel DC and normalize channel scale without modifying input arrays.
3. Apply fourth-order Chebyshev I bandpass designs (0.5 dB ripple) at 8–88,
   16–88 and 24–88 Hz. Forward/backward SOS filtering uses
   [SciPy's `sosfiltfilt`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.sosfiltfilt.html).
4. Compare each band against five sine/cosine harmonic pairs for each target.
   SVD removes rank-deficient dimensions, including duplicate or flat channels.
5. Combine squared canonical correlations with weights `k^(-1.25) + 0.25`,
   normalized by their sum, and compare the best and second-best scores.

For sample `n` within the cropped window and harmonic `h = 1..5`, the reference
phase is `h * (2*pi*f*(n + offset_samples)/eeg_sample_rate + initial_phase)`.
Both sine and cosine are included. The initial phases match the paradigm,
but rotating a sine/cosine pair leaves its CCA subspace unchanged. This
classifier tolerates a constant response latency; it is **not a phase-calibrated
classifier** and does not distinguish targets sharing a frequency by phase
alone. All ten current targets have different fundamental frequencies.

The 0.14-second offset is an initial analysis setting, not a measured visual or
hardware latency. It must be validated against the actual recording setup.

## Parameters and output

| Parameter | Default | Meaning |
| --- | --- | --- |
| `window_duration` | 3.0 s | Fixed window, from 1 to 4 seconds. |
| `offset_time` | 0.14 s | Skip post-trigger onset samples; from 0 to 3 seconds. |
| `min_score` | 0.30 | Minimum normalized FBCCA similarity, from 0 to 1. |
| `min_margin` | 0.05 | Minimum absolute best-minus-runner-up score, from 0 to 1. |

Offset plus duration must fit inside the four-second stimulus. The offset uses
`floor(offset_time * sample_rate)` samples and the window uses
`ceil(window_duration * sample_rate)` samples. At 250 Hz the default decision
becomes ready at exactly 785 accumulated post-trigger samples. Extra samples do
not move the analysis window. Forced short windows return `None`.

Accepted output is `Prediction(target_id=1..10, confidence=score)`. The SDK's
`confidence` field carries normalized FBCCA similarity, **not a calibrated
probability of correctness**. The host creates the classification, retains the
trial identity and event label, and sends the target to the selected outputs.
The paradigm receives `{"target_id": ...}`; device-specific commands remain
the chosen Effector's responsibility.

Non-finite analysis data, flat windows, failed numerical decomposition, low
scores and insufficient winner margins return `None`. Exact ties always
abstain, even with a zero margin setting. These initial thresholds are not a
validated idle-state detector or an accuracy guarantee. Tune them with labeled
EEG for the intended subject, channel selection and recording conditions.

There is one classification attempt per complete fixed window per epoch,
including a rejected attempt. The host may keep collecting until a boundary;
`begin_epoch()` resets the decision state for the next trigger, including a
repeated event ID. References and filters are reused until teardown.

Cued paradigm trials use event IDs 1..10. Free choice uses event **11**, which
opens an epoch but is not an eleventh target or an accuracy label. Ground-truth
event IDs never enter this decoder. Zero is the trigger baseline. Use the
host's trigger-edge preprocessing for level-based sources. This package does
not change epoch detection, result correlation or acquisition behavior.

## Package and run

Use the project's host environment with NumPy and SciPy installed. From the
repository root:

```sh
python -m omnibci.plugin_cli validate omnibci/infrastructure/plugins/algorithms/decoders/command10_fbcca
python -m omnibci.plugin_cli package omnibci/infrastructure/plugins/algorithms/decoders/command10_fbcca --output dist/command10-fbcca-0.1.0.zip
```

Select **10-command SSVEP FBCCA** in **Setup → Signal Processing → Decoder**.
For a host without the built-in contribution, install and enable its ZIP first.
Avoid a duplicate installation of the same built-in package/version. Pair it
with **10-command SSVEP** for live stimulation. The existing Classic40 decoders
use different class ordering and are not substitutes for this decoder.

Synthetic tests cover all target IDs, several EEG sample rates, noise, latency,
rank deficiency, invalid inputs, abstention, epoch resets and immutable inputs.
Installation and epoch-host tests verify standalone loading and the distinction
between event labels and predictions. These checks do not establish accuracy
on real EEG, physical marker synchronization or actuator behavior.
