"""Copy the built app to a read-only installation path containing spaces and Unicode."""
import argparse
import os
from pathlib import Path
import shutil
import stat
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--parent", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    manifests = list(source.rglob("runtime-manifest.json"))
    if len(manifests) != 1:
        parser.error("Expected exactly one packaged runtime")
    directory = manifests[0].relative_to(source).parts[0]
    destination = Path(tempfile.mkdtemp(prefix="OmniBCI installed 数据 ", dir=args.parent)).resolve()
    installed = destination / directory
    shutil.copytree(source / directory, installed, symlinks=True)
    # Preserve execute bits and symlinks. Windows marks files read-only; true
    # directory access control there remains a clean-machine installation check.
    for path in sorted(installed.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    installed.chmod(stat.S_IMODE(installed.stat().st_mode) & ~0o222)
    if args.github_output:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"path={destination}\n")
    print(destination)


if __name__ == "__main__":
    main()
