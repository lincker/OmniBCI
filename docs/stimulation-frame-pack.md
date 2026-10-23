# Building the stimulation frame pack

The classic40SSVEP plugin stores its images in
`omnibci/infrastructure/plugins/paradigms/classic40SSVEP/resource/classic40_ssvep.ofp`.
An `.ofp` file is a ZIP archive using DEFLATE compression. It contains
`manifest.json` and raw pixel arrays, rather than PNG or JPEG files.

The updated pack keeps the original storage approach and adds separate images
for outline cues and solid recognition results. The runtime selects the image
for the current phase and target.

## What is inside

The manifest schema is `omnibci.frame-pack/v2`. All images are 1920 pixels wide
and 990 pixels high.

| Group | Count | Format | Array shape | Purpose |
| --- | --- | --- | --- | --- |
| `idle` | 1 | `L8` | `(990, 1920, 1)` | Original target grid |
| `stimulation` | 240 | `L8` | `(990, 1920, 1)` | Original flickering frames |
| `cue` | 40 | `RGB8` | `(990, 1920, 3)` | Four-pixel red inner outline around the prompted target |
| `result` | 40 | `RGB8` | `(990, 1920, 3)` | Solid red background behind the recognized target |

`L8` means one unsigned 8-bit luminance value per pixel: 0 is black and 255 is
white. `RGB8` means three unsigned 8-bit values per pixel, ordered red, green,
and blue. Pure red is `(255, 0, 0)`.

The arrays use **bottom-up** rows: row 0 belongs at the bottom of the displayed
image. The builder preserves this orientation. When exporting a frame to an
ordinary top-down image viewer, flip it vertically once with `np.flipud(frame)`.

```text
classic40_ssvep.ofp
  manifest.json
  frames/idle/000.l8
  frames/stimulation/000.l8 ... 239.l8
  frames/cue/000.rgb8 ... 039.rgb8
  frames/result/000.rgb8 ... 039.rgb8
```

Frame indices start at 0; target IDs start at 1. Target 1 therefore uses
`frames/cue/000.rgb8` during the cue phase and `frames/result/000.rgb8` when
recognized. A prompted target and a recognized target can be different.

## How the builder creates the outline

The implementation is in `scripts/build_stimulation_pack.py`:

- `make_cue_frame(idle, result)` creates one outline image.
- `build_pack(source, output)` builds and verifies the complete archive.

The original v1 pack already contains solid red images in its `cue` group.
The builder moves those images to the v2 `result` group without changing their
pixel bytes. It also preserves the idle and stimulation bytes and their original
source metadata. When rebuilding an existing v2 pack, it uses that pack's
`result` group instead.

Each new cue starts as a copy of the idle grid. Expanding its single channel
into three equal channels preserves the grayscale appearance while allowing
colored pixels:

```python
cue = np.repeat(idle, 3, axis=2)
```

The builder finds the target rectangle from the corresponding solid result
image. It identifies red pixels with high red and low green/blue values, then
takes their minimum and maximum row and column coordinates. This reuses the
actual image geometry instead of maintaining another copy of the grid layout.

For bounds `x0:x1` and `y0:y1`, these four slices draw inside the rectangle:

```python
border = 4
cue[y0:y0 + border, x0:x1] = (255, 0, 0)
cue[y1 - border:y1, x0:x1] = (255, 0, 0)
cue[y0:y1, x0:x0 + border] = (255, 0, 0)
cue[y0:y1, x1 - border:x1] = (255, 0, 0)
```

Everything between the borders remains identical to the idle grid, including
the character. The width is four resource-image pixels and scales with the
displayed image. To change it, edit `_BORDER_PX` in the builder and rebuild the
pack. Existing v2 cue images are regenerated from idle and result images, so
rebuilding does not accumulate outlines.

## Build and inspect it yourself

Run these commands from the repository root. They use the existing virtual
environment. On Windows, use `venv\Scripts\python.exe` in place of
`venv/bin/python` and choose a local temporary output path.

First create a separate archive for inspection:

```bash
venv/bin/python scripts/build_stimulation_pack.py --output /tmp/classic40_ssvep.ofp
```

The source defaults to the bundled resource. It may already be a v2 pack;
this command rebuilds v2 and does not recreate an older v1 pack.

Inspect the schema, dimensions, orientation, and group counts without extracting
all the images:

```bash
venv/bin/python - <<'PY'
import json
from collections import Counter
from zipfile import ZipFile

with ZipFile('/tmp/classic40_ssvep.ofp') as archive:
    manifest = json.loads(archive.read('manifest.json'))
    print('Schema:', manifest['schema'])
    print('Dimensions:', manifest['width'], manifest['height'])
    print('Orientation:', manifest['orientation'])
    print('Groups:', dict(Counter(frame['group'] for frame in manifest['frames'])))
PY
```

To use a different existing v1 or v2 input, supply both paths:

```bash
venv/bin/python scripts/build_stimulation_pack.py --source /path/to/input.ofp --output /tmp/classic40_ssvep.ofp
```

Once ready, rebuild the bundled resource in place and run the stimulation tests:

```bash
venv/bin/python scripts/build_stimulation_pack.py
venv/bin/python -m pytest tests/stimulation -m "not gui and not hardware"
```

Omitting `--output` replaces the selected source archive. The builder first
writes a temporary file in the destination directory, verifies every frame,
closes all archive handles, and finally replaces the destination with
`os.replace`. A failure before replacement leaves the destination untouched.
The destination directory must already exist.

Fixed ZIP timestamps and stable JSON formatting make repeated builds
reproducible with the same input and compression implementation.

## Integrity and provenance

The manifest records each frame's group, index, entry path, pixel format,
shape, byte size, and digests.

| Field | Meaning |
| --- | --- |
| `sha256` | Digest of this frame's uncompressed raw pixel bytes; checked when the frame is read |
| `source` / `source_sha256` | Identity and digest of the input used to produce the frame |
| `geometry_source` / `geometry_source_sha256` | For generated cues, the result frame used to locate the target rectangle |
| `border_px` | For generated cues, the outline width in resource-image pixels |

For a generated cue, `source` is `frames/idle/000.l8`, and `source_sha256`
equals the idle frame's canonical `sha256`. Its geometry fields point to the
corresponding result frame and that frame's canonical digest. The cue's own
`sha256` describes the newly generated RGB pixels. These values have different
roles: integrity answers whether the stored pixels match the manifest;
provenance records which inputs produced them.

The runtime uses `FramePack(path)` and expects v2. The builder explicitly uses
`FramePack(path, allow_legacy=True)` to migrate v1 inputs. In that migration mode,
`iter_result_frames()` reads the old solid `cue` frames. An old archive must be
rebuilt before the updated runtime can use it.

## Compression and rendering are separate

During plugin setup, `FramePack` reads and decompresses each required frame,
checks its size and SHA-256, and supplies an array to `upload_texture`. The
renderer retains the uploaded textures. Each displayed frame then selects an
existing texture; the render loop does not reopen the ZIP or decompress images.

DEFLATE reduces the archive's disk size. It does not reduce the uncompressed
pixel data needed for textures. Adding 40 full RGB cue images adds
`40 × 1920 × 990 × 3 = 228,096,000` bytes, approximately **217.5 MiB**, of raw
pixel payload compared with the original pack. Actual GPU allocation depends
on the renderer and driver. The extra images trade memory for a consistent
resource-based rendering path with no per-frame outline drawing.
