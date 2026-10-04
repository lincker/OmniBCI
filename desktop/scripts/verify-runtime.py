"""Verify the staged or packaged runtime with its own isolated interpreter."""
import argparse
from importlib import metadata, resources
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile


def canonical(name):
    return name.lower().replace("_", "-").replace(".", "-")


def validate_wheel_platform(name, wheel, manifest):
    platforms = [tag for line in wheel.splitlines() if line.startswith("Tag: ") for tag in line.rsplit("-", 1)[1].split(".")]
    if "any" in platforms:
        return
    target = manifest["target"]
    for tag in platforms:
        if target.startswith("darwin-"):
            match = re.fullmatch(r"macosx_(\d+)_(\d+)_(.+)", tag)
            architectures = {"x86_64", "intel", "universal2", "universal"} if target.endswith("x64") else {"arm64", "universal2"}
            if match and match[3] in architectures and (int(match[1]), int(match[2])) <= tuple(manifest["minimumMacOS"]):
                return
        elif target == "linux-x64":
            match = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", tag)
            if match and (int(match[1]), int(match[2])) <= tuple(manifest["maximumGlibc"]):
                return
            if tag in {"manylinux1_x86_64", "manylinux2010_x86_64", "manylinux2014_x86_64"}:
                return
            if tag == "linux_x86_64" and canonical(name) in {"moderngl", "glcontext"}:
                return  # Built on the baseline runner; ELF requirements are audited below.
        elif target == "win32-x64" and tag == "win_amd64":
            return
    raise AssertionError(f"{name}: wheel platforms {platforms} exceed the {target} baseline or architecture")


def audit_native_libraries(root, manifest):
    target = manifest["target"]
    if target == "win32-x64":
        return {"status": "wheel-architecture-checked; Windows 10 runtime validation is a manual release gate"}
    candidates = {path.resolve() for path in (root / "python").rglob("*")
                  if path.is_file() and (path.suffix in {".so", ".dylib"} or re.search(r"\.so\.\d", path.name))}
    candidates.add((root / manifest["interpreter"]).resolve())
    maximum = (0, 0)
    checked = 0
    for path in sorted(candidates):
        if target.startswith("darwin-"):
            with path.open("rb") as handle:
                magic = handle.read(4)
            if magic not in {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf"}:
                continue
            architecture = "x86_64" if target.endswith("x64") else "arm64"
            output = subprocess.check_output(["otool", "-arch", architecture, "-l", str(path)], text=True)
            required = [(int(a), int(b)) for a, b in re.findall(r"\bminos (\d+)\.(\d+)", output)]
            required.extend((int(a), int(b)) for a, b in re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+)\.(\d+)", output))
            limit = tuple(manifest["minimumMacOS"])
        else:
            with path.open("rb") as handle:
                if handle.read(4) != b"\x7fELF":
                    continue
            output = subprocess.check_output(["readelf", "--version-info", str(path)], text=True)
            required = [(int(a), int(b)) for a, b in re.findall(r"\bGLIBC_(\d+)\.(\d+)", output)]
            limit = tuple(manifest["maximumGlibc"])
        checked += 1
        if required:
            maximum = max(maximum, max(required))
            assert max(required) <= limit, (str(path), max(required), limit)
    assert checked, "No native runtime libraries were audited"
    return {"librariesChecked": checked, "maximumOSRequirement": list(maximum)}


def plugin_check():
    from omnibci.application.plugin_schema import validate_parameters
    from omnibci.infrastructure.plugins.loader import PluginLoader
    from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginRoot, vendor_plugins_dir
    from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.frame_pack import FramePack

    vendor = vendor_plugins_dir()
    kinds = ("acquisition", "decoder", "epoch_detector", "filter", "paradigm", "trigger", "effector")
    catalog = PluginCatalog({kind: lambda: [PluginRoot(vendor, "builtin", 100)] for kind in kinds})
    contributions = []
    for kind in kinds:
        found = catalog.list_contributions(kind)
        assert found, f"No packaged {kind} contributions"
        for descriptor in found:
            assert descriptor.manifest_path.is_file()
            assert descriptor.configuration_schema is not None
            validate_parameters(descriptor.configuration_schema, {})
            if descriptor.entrypoint and descriptor.supports_platform(sys.platform):
                PluginLoader.load_entrypoint(descriptor, object)
        contributions.extend(found)
    assert len(contributions) == 10, len(contributions)
    resource = resources.files("omnibci.infrastructure.plugins.paradigms.classic40SSVEP").joinpath(
        "resource", "classic40_ssvep.ofp",
    )
    with resources.as_file(resource) as path, FramePack(path) as pack:
        assert pack.idle_frame.shape == (990, 1920, 1)
        assert sum(1 for _ in pack.iter_stimulation_frames()) == 240
        assert sum(1 for _ in pack.iter_cue_frames()) == 40
    return len(contributions)


def spawned_check(connection):
    try:
        count = plugin_check()
        connection.send({"python": sys.executable, "version": platform.python_version(), "plugins": count})
    finally:
        connection.close()


def collect_licenses(root):
    destination = root / "third-party-licenses"
    destination.mkdir(exist_ok=True)
    index = []
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        entry = {"name": name, "version": distribution.version, "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"), "files": []}
        for relative in distribution.files or []:
            text = str(relative).replace("\\", "/")
            if ".." in relative.parts:
                continue
            if ".dist-info/licenses/" not in text and not relative.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "AUTHORS")):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file():
                continue
            target = destination / canonical(name) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            entry["files"].append(target.relative_to(destination).as_posix())
        index.append(entry)
    for source in (root / "python").rglob("LICENSE.txt"):
        if "site-packages" not in source.parts:
            target = destination / "cpython" / source.relative_to(root / "python")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    (destination / "python-packages.json").write_text(json.dumps(index, indent=2) + "\n")


def verify_inside(root, collect):
    root = root.resolve()
    manifest = json.loads((root / "runtime-manifest.json").read_text())
    interpreter = (root / manifest["interpreter"]).resolve()
    assert Path(sys.executable).resolve() == interpreter, sys.executable
    assert platform.python_version() == manifest["pythonVersion"]
    assert Path(sys.prefix).resolve().is_relative_to(root / "python"), sys.prefix
    expected = {canonical(name): version for name, version in manifest["dependencies"].items()}
    expected["omnibci"] = manifest["applicationVersion"]
    installed = {canonical(distribution.metadata["Name"]): distribution.version for distribution in metadata.distributions()}
    assert set(installed) <= set(expected) | {"pip"}, sorted(set(installed) - set(expected))
    for name, version in expected.items():
        assert installed.get(name) == version, (name, installed.get(name), version)
        validate_wheel_platform(name, metadata.distribution(name).read_text("WHEEL") or "", manifest)
    native = audit_native_libraries(root, manifest)
    for name in ("omnibci", "numpy", "mne", "sklearn", "cryptography", "fastapi", "uvicorn", "glfw", "moderngl", "glcontext", "OpenGL.GL"):
        module = importlib.import_module(name)
        paths = [module.__file__] if module.__file__ else list(module.__path__)
        assert paths and all(Path(path).resolve().is_relative_to(root / "python") for path in paths), (name, paths)
    import omnibci
    source = Path(next(iter(omnibci.__path__))).resolve()
    assert not (source / "tests").exists()
    assert not any(source.rglob("*.cnt")), "Sample recordings must not ship in the application"
    assert resources.files("omnibci.conf").joinpath("settings.toml").is_file()
    assert not (source / "conf" / "profiles").exists(), "Machine-specific profiles must not ship"
    count = plugin_check()
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=spawned_check, args=(send,))
    process.start()
    send.close()
    try:
        assert receive.poll(60), "Spawned runtime did not become ready"
        child = receive.recv()
        process.join(15)
        assert process.exitcode == 0, process.exitcode
        assert Path(child["python"]).resolve() == interpreter, child
        assert child["version"] == manifest["pythonVersion"] and child["plugins"] == count, child
    finally:
        receive.close()
        if process.is_alive():
            process.terminate()
            process.join(10)
    if collect:
        collect_licenses(root)
        manifest["nativeCompatibility"] = native
        (root / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    assert (root / "third-party-licenses" / "python-packages.json").is_file()
    print(f"Runtime verified: {manifest['target']}, CPython {manifest['pythonVersion']}, {len(expected)} distributions, {count} plugins, 281 frames, spawn")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--resources", type=Path)
    group.add_argument("--packaged", type=Path, help="Find exactly one unpacked app under the builder output directory")
    group.add_argument("--inside", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--collect-licenses", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        verify_inside(args.inside, args.collect_licenses)
        return
    if args.packaged:
        matches = list(args.packaged.rglob("runtime-manifest.json"))
        if len(matches) != 1:
            parser.error(f"Expected exactly one packaged runtime under {args.packaged}; found {matches}")
        root = matches[0].parent.resolve()
    else:
        root = args.resources.resolve()
    manifest = json.loads((root / "runtime-manifest.json").read_text())
    with tempfile.TemporaryDirectory(prefix="omnibci-runtime-check-") as temporary:
        environment = {
            **os.environ, "OMNIBCI_CONFIG_DIR": str(Path(temporary) / "config"),
            "OMNIBCI_PLUGIN_DIR": str(Path(temporary) / "plugins"),
            "MPLBACKEND": "Agg", "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = [
            root / manifest["interpreter"], "-I", "-B", Path(__file__).resolve(), "--inside", root,
        ]
        if args.collect_licenses:
            command.append("--collect-licenses")
        subprocess.run(command, env=environment, cwd=temporary, check=True)


if __name__ == "__main__":
    main()
