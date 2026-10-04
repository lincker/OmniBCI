"""Write SHA256 checksums for the current target's internal distribution artifacts."""
import hashlib
import json
from pathlib import Path


def main():
    desktop = Path(__file__).resolve().parents[1]
    manifest = json.loads((desktop / "resources/runtime-manifest.json").read_text())
    artifacts = list(desktop.glob("OmniBCI-*-unpacked.tar.gz"))
    for suffix in ("exe", "dmg", "deb"):
        artifacts.extend((desktop / "release").glob(f"*.{suffix}"))
    lines = []
    for artifact in sorted(artifacts):
        with artifact.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        lines.append(f"{digest}  {artifact.relative_to(desktop).as_posix()}")
    if not lines:
        raise SystemExit("No built artifacts found")
    output = desktop / f"SHA256SUMS-{manifest['target']}.txt"
    output.write_text("\n".join(lines) + "\n")
    print(output)


if __name__ == "__main__":
    main()
