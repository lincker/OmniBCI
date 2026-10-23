#!/usr/bin/env python
"""Rebuild the classic40SSVEP compressed frame pack with red outline cues.

Run from the project root with the virtual environment active::

    python scripts/build_stimulation_pack.py
    python scripts/build_stimulation_pack.py --source old.ofp --output new.ofp

An OFP file is a ZIP archive containing a JSON manifest and raw pixel arrays.
This builder preserves the idle, stimulation, and solid result pixels, adds
four-pixel outline cues, and verifies the temporary archive before replacing
the destination. Both original v1 packs and rebuilt v2 packs are valid inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import numpy as np


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.frame_pack import FramePack


_DEFAULT_SOURCE = (
    _ROOT / "omnibci/infrastructure/plugins/paradigms/classic40SSVEP"
    / "resource/classic40_ssvep.ofp"
)
_BORDER_PX = 4


def make_cue_frame(idle: np.ndarray, result: np.ndarray) -> np.ndarray:
    """Copy the idle image and outline the solid result target from inside.

    Raw frames already use bottom-up coordinates, so no image flip is needed.
    The result's red pixels supply the bounds without duplicating the layout.
    """
    if idle.ndim != 3 or idle.shape[2] != 1:
        raise ValueError("Idle frame must have one channel")
    if result.shape != (*idle.shape[:2], 3):
        raise ValueError("Result frame must match the idle frame and have three channels")
    red = (result[:, :, 0] > 200) & (result[:, :, 1] < 50) & (result[:, :, 2] < 50)
    ys, xs = np.nonzero(red)
    if not len(xs):
        raise ValueError("Result frame has no red target")
    x0, x1 = xs.min(), xs.max() + 1
    y0, y1 = ys.min(), ys.max() + 1
    if min(x1 - x0, y1 - y0) <= 2 * _BORDER_PX:
        raise ValueError("Result target is too small for a four-pixel outline")
    if len(xs) <= 0.9 * (x1 - x0) * (y1 - y0):
        raise ValueError("Result frame must contain a solid red target")

    cue = np.repeat(idle, 3, axis=2)
    cue[y0:y0 + _BORDER_PX, x0:x1] = (255, 0, 0)
    cue[y1 - _BORDER_PX:y1, x0:x1] = (255, 0, 0)
    cue[y0:y1, x0:x0 + _BORDER_PX] = (255, 0, 0)
    cue[y0:y1, x1 - _BORDER_PX:x1] = (255, 0, 0)
    return cue


def _write_entry(archive: ZipFile, entry: str, content: bytes) -> None:
    # Fixed timestamps and permissions make repeated builds reproducible.
    info = ZipInfo(entry, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content, compress_type=ZIP_DEFLATED, compresslevel=9)


def _write_frame(archive: ZipFile, frame: np.ndarray, metadata: dict) -> dict:
    raw = frame.tobytes()
    descriptor = dict(
        metadata,
        shape=list(frame.shape),
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    _write_entry(archive, descriptor["entry"], raw)
    return descriptor


def build_pack(source: Path, output: Path) -> Path:
    """Build and verify a v2 pack, then atomically replace the output file."""
    source, output = Path(source).resolve(), Path(output).resolve()
    temporary: Path | None = None
    try:
        with (
            FramePack(source, allow_legacy=True) as pack,
            ZipFile(source) as source_archive,
        ):
            manifest = json.loads(source_archive.read("manifest.json"))
            descriptors = {
                (item["group"], item["index"]): item
                for item in manifest["frames"]
            }
            result_group = "cue" if manifest["schema"].endswith("/v1") else "result"
            idle = pack.idle_frame
            idle_descriptor = descriptors["idle", 0]
            with tempfile.NamedTemporaryFile(
                dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)

            frames = []
            with ZipFile(temporary, "w") as archive:
                frames.append(_write_frame(archive, idle, idle_descriptor))
                for index, frame in enumerate(pack.iter_stimulation_frames()):
                    frames.append(_write_frame(
                        archive, frame, descriptors["stimulation", index]
                    ))
                for index, result in enumerate(pack.iter_result_frames()):
                    result_entry = f"frames/result/{index:03d}.rgb8"
                    result_descriptor = dict(
                        descriptors[result_group, index],
                        group="result", entry=result_entry,
                    )
                    frames.append(_write_frame(archive, result, result_descriptor))
                    cue_descriptor = {
                        "group": "cue", "index": index,
                        "entry": f"frames/cue/{index:03d}.rgb8",
                        "pixel_format": "RGB8",
                        # Provenance records both pixel and geometry inputs.
                        "source": idle_descriptor["entry"],
                        "source_sha256": idle_descriptor["sha256"],
                        "geometry_source": result_entry,
                        "geometry_source_sha256": result_descriptor["sha256"],
                        "border_px": _BORDER_PX,
                    }
                    frames.append(_write_frame(
                        archive, make_cue_frame(idle, result), cue_descriptor
                    ))
                manifest.update(schema="omnibci.frame-pack/v2", frames=frames)
                encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
                _write_entry(archive, "manifest.json", encoded)

        # Reading each frame checks its size and SHA-256, not just ZIP metadata.
        with FramePack(temporary) as verification:
            verification.idle_frame
            for sequence in (
                verification.iter_stimulation_frames(),
                verification.iter_cue_frames(),
                verification.iter_result_frames(),
            ):
                for _ in sequence:
                    pass
        # Close all archives before replacement, including on Windows.
        os.replace(temporary, output)
        return output
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=_DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, help="Defaults to replacing the source pack")
    args = parser.parse_args()
    output = build_pack(args.source, args.output or args.source)
    print(f"Wrote verified frame pack: {output}")


if __name__ == "__main__":
    main()
