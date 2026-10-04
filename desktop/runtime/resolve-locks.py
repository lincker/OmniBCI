"""Refresh committed dependency locks with the pinned uv resolver."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "desktop" / "runtime"


def main():
    versions = json.loads((RUNTIME / "versions.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True, help="Native interpreter at the pinned runtime version")
    args = parser.parse_args()
    python = args.python.resolve()
    actual = subprocess.check_output([python, "-I", "-B", "-c", "import platform; print(platform.python_version())"], text=True).strip()
    if actual != versions["pythonVersion"]:
        parser.error(f"Expected Python {versions['pythonVersion']}; found {actual}")
    uv = os.environ.get("UV", "uv")
    installed = subprocess.check_output([uv, "--version"], text=True).split()[1]
    if installed != versions["uvVersion"]:
        raise SystemExit(f"Expected uv {versions['uvVersion']}; found {installed}")
    for target, configuration in versions["targets"].items():
        output = RUNTIME / "locks" / f"{target}.txt"
        names = set(re.findall(r"^([\w.-]+)==", output.read_text(), re.MULTILINE))
        wheel_only = ",".join(sorted(names - set(versions["sourceBuilds"])))
        subprocess.run([
            uv, "pip", "compile", str(ROOT / "pyproject.toml"), "--extra", "stim",
            "--python", str(python),
            "--python-version", versions["pythonVersion"],
            "--python-platform", configuration["lockPlatform"],
            "--generate-hashes", "--no-header", "--no-annotate",
            "--no-python-downloads", "--build-constraints", str(RUNTIME / "build-constraints.txt"),
            "--only-binary", wheel_only, "--output-file", str(output),
        ], check=True, cwd=ROOT)


if __name__ == "__main__":
    main()
