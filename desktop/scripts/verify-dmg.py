"""Mount an internal DMG read-only and compare its app to the smoke-tested payload."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def payload(root):
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = ["symlink", os.readlink(path)]
        elif path.is_file():
            result[name] = ["file", digest(path), stat.S_IMODE(path.stat().st_mode) & 0o111]
        elif path.is_dir():
            result[name] = ["directory"]
        else:
            raise RuntimeError(f"Unexpected application entry: {path}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True, help="Smoke-tested app or its parent directory")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("DMG verification requires macOS")
    references = list(args.reference.rglob("runtime-manifest.json"))
    if len(references) != 1:
        parser.error("Expected exactly one reference runtime")
    reference_app = references[0].parents[2]
    expected = payload(reference_app)
    with tempfile.TemporaryDirectory(prefix="OmniBCI DMG verification ") as temporary:
        mount = Path(temporary) / "mounted"
        mount.mkdir()
        subprocess.run([
            "hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(args.image.resolve()),
        ], check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        try:
            assert os.statvfs(mount).f_flag & os.ST_RDONLY, "Disk image was not mounted read-only"
            application = mount / "OmniBCI.app"
            subprocess.run(["codesign", "--verify", "--deep", "--strict", str(application)], check=True)
            actual = payload(application)
            different = [name for name in expected.keys() | actual.keys() if expected.get(name) != actual.get(name)]
            assert not different, f"Installer differs from smoke-tested app: {different[:10]}"
            report = {
                "status": "passed", "image": str(args.image.resolve()), "sha256": digest(args.image),
                "reference": str(reference_app.resolve()), "entriesCompared": len(expected),
                "payloadSha256": hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(),
                "runtimeManifestSha256": digest(application / "Contents/Resources/runtime-manifest.json"),
                "mountedReadOnly": True, "codeSignatureVerified": True,
            }
        finally:
            subprocess.run(["hdiutil", "detach", str(mount)], check=True, capture_output=True, text=True)
    report["detached"] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
