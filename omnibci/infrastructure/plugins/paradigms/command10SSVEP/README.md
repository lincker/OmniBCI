# 10-command SSVEP

This standalone package provides one `paradigm` contribution. It presents ten
SSVEP targets using the host's native renderer at a fixed 60 Hz refresh rate.
It uses Plugin API v1 and manifest version 1.

Selection: `OmniBCI.command10SSVEP` / `default` / `0.1.0`.
Constructor parameters: `{}`. A compatible decoder and device-specific Effector
are selected separately; this package does not contain either one.

## Target protocol

The canonical definitions are in `targets.py`. IDs remain stable independently
of visual traversal order. Array consumers use `target_id - 1`.

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

The center is a static feedback area, not an additional stimulus target.
The six letter labels do not prescribe device actions. Each target is a
200 x 200 reference-pixel square in the host's 1920 x 1080 window. Target
centers use five columns spaced 360 pixels apart and three rows spaced 300
pixels apart. Four arrows and six letters are supplied as transparent PNGs.

## Trial, timing and feedback

Submit `{"cue_target_id": 1}` to cue target 1; IDs 1 through 10 are accepted.
The selected target has a red cue border for 60 frames (1 second at 60 Hz).
Then all ten targets stimulate together for 240 frames (4 seconds at 60 Hz).
Submit `{"cue_target_id": 0}` for free choice: there is no cue interval and
stimulation starts at the next frame callback.

The target background uses sampled sinusoidal grayscale values:

```text
brightness(i, n) = 0.5 + 0.5 * sin(2 * pi * frequency[i] * n / 60 + phase[i])
```

White labels remain static over the varying background. The center never
flickers. Every trial resets its stimulus phase origin. The index `n` (`0..239`)
advances once per VSync-paced `render_frame` callback.

`present_trial` clears the trigger baseline before starting the trial. At the
first stimulus frame, the plugin requests one start marker: the cued ID (1..10),
or **11** for free choice. Zero is the hardware baseline; it never starts an
epoch. The trigger is cleared when that interval ends. For level-based
acquisition, the host's trigger-edge preprocessing keeps the first sample of
the nonzero run, including across chunks.

Feedback accepts exactly `{"target_id": 1..10}`. An early result is cached
until the fixed stimulus interval ends. Feedback then lasts 90 frames
(1.5 seconds at 60 Hz), with the predicted target's entire background filled red, its
white label preserved, and the same label shown in the center. A result may
also arrive during this feedback interval without
extending it. With no result, the center remains a dash and the trial still
finishes. The last result stays in the idle center until another trial starts.
Only one result is accepted per trial. Idle, pre-stimulus and duplicate results
are rejected. Busy trials cannot be overwritten.

The host's result callback supplies a target ID without a stimulation-trial
identity. The plugin cannot distinguish a delayed previous-trial result from
a current-trial result once another stimulus has started. Trial/result
correlation belongs to the host; these state checks do not add that guarantee.

## Install and run

Use a host with the `stim` extra installed (`pip install "omnibci[stim]"`),
including Pillow for decoding the bundled PNGs. From the repository root,
using the project's Python environment:

```sh
python -m omnibci.plugin_cli validate omnibci/infrastructure/plugins/paradigms/command10SSVEP
python -m omnibci.plugin_cli package omnibci/infrastructure/plugins/paradigms/command10SSVEP --output dist/command10SSVEP-0.1.0.zip
```

In a host that ships this built-in package, select **10-command SSVEP** in
the Stimulation configuration. For a host without it, install and enable the
ZIP first. Avoid installing the same package/version alongside an identical
built-in release: exact duplicate identities are ambiguous in the catalog.
The existing generic trial controls provide trial count, cue ID and the `1-10`
cue sequence. Choose a compatible Trigger contribution for physical markers.
The host's no-trigger mode can preview the display without generating EEG events.

A paired decoder must use the table above and return target IDs 1..10; the
Classic40 decoder's class count and frequency ordering differ. Free-choice
event 11 carries no ground-truth class and must not be used as an accuracy label.
The six auxiliary IDs can be mapped to device commands by the chosen Effector.

## Resources and validation

The renderer owns its window, frame submission and trigger lifecycle. The
paradigm uploads eleven small textures during `setup`, performs no font lookup
or image upload in the frame callback, and releases all acquired handles in
`teardown`, including after partial setup failure. The host clears the trigger
when stopping or cleaning up. Labels use Pillow's bundled font and can be
regenerated with `python scripts/build_command10_assets.py`; runtime rendering
does not depend on system fonts.

Headless tests check the protocol, grayscale values, per-frame countdowns,
phase resets, marker requests, malformed payloads, feedback and cleanup.
Installation tests load the package from its ZIP and verify packaged resources.
These tests do not establish EEG accuracy, physical trigger delivery, display
gamma/luminance or photon timing. Measure those on the intended 60 Hz display
and acquisition hardware. A render return or trigger call is not proof that
the corresponding image or physical marker was delivered.
