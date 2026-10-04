"""Stage a native, relocatable CPython runtime without using a development venv."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request


DESKTOP = Path(__file__).resolve().parents[1]
ROOT = DESKTOP.parent
INPUTS = DESKTOP / "runtime"


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def host_target():
    architecture = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}
    return f"{sys.platform}-{architecture.get(platform.machine().lower(), platform.machine())}"


def run(command, **kwargs):
    subprocess.run([str(item) for item in command], check=True, **kwargs)


def download(url, destination, expected_hash):
    if not destination.exists():
        partial = destination.with_suffix(destination.suffix + ".partial")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        partial.replace(destination)
    actual = sha256(destination)
    if actual != expected_hash:
        raise RuntimeError(f"SHA256 mismatch for {destination}: expected {expected_hash}, found {actual}")


def copy_source(root, destination):
    """Copy build inputs without stale outputs or machine-specific configuration."""
    destination.mkdir()
    shutil.copyfile(root / "pyproject.toml", destination / "pyproject.toml")
    def ignore(directory, names):
        excluded = {name for name in names if name.startswith(".") or name == "__pycache__" or name.endswith((".pyc", ".pyo"))}
        if Path(directory) == root / "omnibci" / "conf":
            excluded.add("profiles")
        return excluded
    shutil.copytree(root / "omnibci", destination / "omnibci", ignore=ignore)


def main():
    versions = json.loads((INPUTS / "versions.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=versions["targets"], default=host_target())
    parser.add_argument("--output", type=Path, default=DESKTOP / "resources")
    parser.add_argument("--cache-dir", type=Path, default=INPUTS / "cache")
    args = parser.parse_args()
    if args.target not in versions["targets"]:
        parser.error(f"Unsupported target: {args.target}")
    if args.target != host_target():
        parser.error(f"Build on the target OS/architecture: this interpreter runs {host_target()}")
    config = versions["targets"][args.target]
    uv = os.environ.get("UV", "uv")
    installed_uv = subprocess.check_output([uv, "--version"], text=True).split()[1]
    if installed_uv != versions["uvVersion"]:
        parser.error(f"Expected uv {versions['uvVersion']}; found {installed_uv}")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()) and not (output / "runtime-manifest.json").is_file():
        parser.error(f"Refusing to replace an unrecognized directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = args.cache_dir.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    archive_name = (
        f"cpython-{versions['pythonVersion']}+{versions['pbsRelease']}-"
        f"{config['triple']}-install_only_stripped.tar.gz"
    )
    url = (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{versions['pbsRelease']}/{archive_name.replace('+', '%2B')}"
    )
    archive = cache / archive_name
    download(url, archive, config["sha256"])
    lock = INPUTS / "locks" / f"{args.target}.txt"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output([
        "git", "status", "--porcelain", "--untracked-files=normal", "--",
        "omnibci", "pyproject.toml", "desktop", "frontend",
    ], cwd=ROOT, text=True).strip())
    packages = dict(re.findall(r"^([a-zA-Z0-9_.-]+)==([^\s;\\]+)", lock.read_text(), re.MULTILINE))
    if not packages or "pytest" in packages or "omnibci" in packages:
        raise RuntimeError("Runtime dependency lock is empty or contains development/project requirements")
    # Only these reviewed extensions may build from their hashed source archives.
    wheel_only = ",".join(sorted(set(packages) - set(versions["sourceBuilds"])))
    environment = {**os.environ, "UV_PYTHON_DOWNLOADS": "never", "UV_LINK_MODE": "copy", "PYTHONDONTWRITEBYTECODE": "1"}
    if args.target.startswith("darwin-"):
        environment["MACOSX_DEPLOYMENT_TARGET"] = "13.0"
    with tempfile.TemporaryDirectory(prefix=".runtime-", dir=output.parent) as temporary:
        work = Path(temporary)
        stage = work / "resources"
        stage.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(stage, filter="data")
        python = stage / config["interpreter"]
        actual_version = subprocess.check_output([python, "-I", "-B", "-c", "import platform; print(platform.python_version())"], text=True).strip()
        if actual_version != versions["pythonVersion"]:
            raise RuntimeError(f"Expected CPython {versions['pythonVersion']}; found {actual_version}")
        run([
            uv, "pip", "install", "--python", python, "--no-deps", "--require-hashes",
            "--python-platform", config["lockPlatform"],
            "--only-binary", wheel_only, "--build-constraints", INPUTS / "build-constraints.txt",
            "--requirements", lock,
        ], env=environment)
        # A clean source tree avoids setuptools reusing stale build/lib profiles
        # or files removed from the checkout since a previous developer build.
        source = work / "source"
        copy_source(ROOT, source)
        run([
            uv, "build", source, "--wheel", "--python", python,
            "--build-constraints", INPUTS / "build-constraints.txt", "--out-dir", work / "wheels",
        ], env=environment)
        wheels = list((work / "wheels").glob("omnibci-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one OmniBCI wheel; found {wheels}")
        run([uv, "pip", "install", "--python", python, "--no-deps", wheels[0]], env=environment)
        run([uv, "pip", "check", "--python", python], env=environment)
        # Keep package metadata and native libraries intact. The wheel includes only
        # omnibci packages, never repository tests, development tools, or sample data.
        package = json.loads((DESKTOP / "package.json").read_text())
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        if package["version"] != project["project"]["version"]:
            raise RuntimeError("Desktop and backend application versions must match")
        manifest = {
            "schemaVersion": 1,
            "applicationVersion": package["version"],
            "electronVersion": package["devDependencies"]["electron"],
            "pythonVersion": actual_version,
            "target": args.target,
            "interpreter": config["interpreter"],
            "minimumOS": config["minimumOS"],
            "minimumMacOS": config.get("minimumMacOS"),
            "maximumGlibc": config.get("maximumGlibc"),
            "source": {"commit": commit, "workingTreeModified": dirty},
            "pythonDistribution": {"release": versions["pbsRelease"], "url": url, "sha256": config["sha256"]},
            "dependencyLock": {"file": lock.name, "sha256": sha256(lock)},
            "dependencies": packages,
            "applicationWheel": {"file": wheels[0].name, "sha256": sha256(wheels[0])},
            "buildTools": {"uv": installed_uv, "constraintsSha256": sha256(INPUTS / "build-constraints.txt")},
        }
        (stage / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        notices = stage / "third-party-licenses"
        notices.mkdir()
        shutil.copytree(INPUTS / "licenses", notices / "python-build-standalone")
        run([python, "-I", "-B", DESKTOP / "scripts" / "verify-runtime.py", "--resources", stage, "--collect-licenses"], env=environment)
        if output.exists():
            shutil.rmtree(output)
        stage.replace(output)
    print(f"Staged {args.target} runtime in {output}")


if __name__ == "__main__":
    main()
