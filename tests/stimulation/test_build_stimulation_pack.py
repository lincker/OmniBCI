"""Exercise frame-pack migration, reproducibility, and failed-build recovery."""
from __future__ import annotations

import hashlib
import json
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
import pytest

from omnibci.infrastructure.plugins.paradigms.classic40SSVEP import frame_pack
from scripts.build_stimulation_pack import build_pack, make_cue_frame


@pytest.fixture()
def legacy_pack(tmp_path, monkeypatch):
    # Small frames exercise the real codec without repeatedly writing 900 MB.
    monkeypatch.setattr(frame_pack, "_WIDTH", 40)
    monkeypatch.setattr(frame_pack, "_HEIGHT", 30)
    monkeypatch.setattr(frame_pack, "_GROUPS", {
        "idle": (1, "L8", 1), "stimulation": (2, "L8", 1),
        "cue": (2, "RGB8", 3), "result": (2, "RGB8", 3),
    })
    idle = np.full((30, 40, 1), 255, dtype=np.uint8)
    idle[13:15, 9:11] = 0
    idle[13:15, 29:31] = 0
    results = []
    for left in (4, 24):
        result = np.repeat(idle, 3, axis=2)
        result[8:20, left:left + 12, 1:] = 0
        results.append(result)
    arrays = {
        "idle": [idle],
        "stimulation": [idle // 2, idle // 3],
        "cue": results,
    }
    manifest = {
        "schema": "omnibci.frame-pack/v1", "width": 40, "height": 30,
        "orientation": "bottom-up", "frames": [],
    }
    path = tmp_path / "legacy.ofp"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for group, frames in arrays.items():
            for index, array in enumerate(frames):
                raw = array.tobytes()
                suffix = "l8" if array.shape[2] == 1 else "rgb8"
                entry = f"frames/{group}/{index:03d}.{suffix}"
                manifest["frames"].append({
                    "group": group, "index": index, "entry": entry,
                    "pixel_format": "L8" if suffix == "l8" else "RGB8",
                    "shape": list(array.shape), "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "source": f"original/{group}/{index}.png",
                    "source_sha256": hashlib.sha256(b"source:" + raw).hexdigest(),
                })
                archive.writestr(entry, raw)
        archive.writestr("manifest.json", json.dumps(manifest))
    return path, arrays, manifest


def test_legacy_format_requires_explicit_migration_access(legacy_pack):
    source, arrays, _ = legacy_pack
    with pytest.raises(frame_pack.FramePackFormatError, match="schema"):
        frame_pack.FramePack(source)
    with frame_pack.FramePack(source, allow_legacy=True) as pack:
        for actual, expected in zip(pack.iter_result_frames(), arrays["cue"], strict=True):
            np.testing.assert_array_equal(actual, expected)


def test_migration_preserves_pixels_and_provenance_and_rebuilds_identically(legacy_pack, tmp_path):
    source, arrays, original = legacy_pack
    output = tmp_path / "updated.ofp"
    build_pack(source, output)
    with frame_pack.FramePack(output) as pack:
        np.testing.assert_array_equal(pack.idle_frame, arrays["idle"][0])
        for actual, expected in zip(pack.iter_stimulation_frames(), arrays["stimulation"], strict=True):
            np.testing.assert_array_equal(actual, expected)
        for actual, expected in zip(pack.iter_result_frames(), arrays["cue"], strict=True):
            np.testing.assert_array_equal(actual, expected)
        for index, cue in enumerate(pack.iter_cue_frames()):
            left = (4, 24)[index]
            assert tuple(cue[8, left]) == (255, 0, 0)
            interior = np.s_[12:16, left + 4:left + 8]
            np.testing.assert_array_equal(
                cue[interior], np.repeat(arrays["idle"][0], 3, axis=2)[interior]
            )
    with ZipFile(output) as archive:
        updated = json.loads(archive.read("manifest.json"))
        assert updated["schema"] == "omnibci.frame-pack/v2"
        assert all(item.compress_type == ZIP_DEFLATED for item in archive.infolist())
    descriptors = {(item["group"], item["index"]): item for item in updated["frames"]}
    for before in original["frames"]:
        group = "result" if before["group"] == "cue" else before["group"]
        after = descriptors[group, before["index"]]
        for key in ("sha256", "source", "source_sha256"):
            assert after[key] == before[key]
    cue = descriptors["cue", 0]
    assert cue["source_sha256"] == descriptors["idle", 0]["sha256"]
    assert cue["geometry_source_sha256"] == descriptors["result", 0]["sha256"]
    first_build = output.read_bytes()
    build_pack(output, output)
    assert output.read_bytes() == first_build


@pytest.mark.parametrize("failure", ["digest", "target"])
def test_failed_build_preserves_destination_and_removes_temporary_file(legacy_pack, tmp_path, failure):
    source, _, manifest = legacy_pack
    damaged = tmp_path / "damaged.ofp"
    entry = "frames/cue/000.rgb8"
    with ZipFile(source) as original, ZipFile(damaged, "w", compression=ZIP_DEFLATED) as archive:
        for name in original.namelist():
            if name == "manifest.json":
                continue
            raw = original.read(name)
            if name == entry:
                raw = bytes(len(raw))
                if failure == "target":
                    descriptor = next(
                        item for item in manifest["frames"] if item["entry"] == entry
                    )
                    descriptor["sha256"] = hashlib.sha256(raw).hexdigest()
            archive.writestr(name, raw)
        archive.writestr("manifest.json", json.dumps(manifest))
    output = tmp_path / "destination.ofp"
    output.write_bytes(b"existing resource")
    before = set(tmp_path.iterdir())
    error = frame_pack.FramePackIntegrityError if failure == "digest" else ValueError
    with pytest.raises(error):
        build_pack(damaged, output)
    assert output.read_bytes() == b"existing resource"
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("kind", ["missing", "thin", "outline"])
def test_cue_generation_rejects_invalid_result_targets(kind):
    idle = np.full((30, 40, 1), 255, dtype=np.uint8)
    result = np.repeat(idle, 3, axis=2)
    if kind == "thin":
        result[10:14, 10:24] = (255, 0, 0)
    elif kind == "outline":
        result[8:22, 10:24] = (255, 0, 0)
        result[9:21, 11:23] = (255, 255, 255)
    with pytest.raises(ValueError):
        make_cue_frame(idle, result)
