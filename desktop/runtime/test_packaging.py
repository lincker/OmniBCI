"""Packaging regressions run with the standard library, without downloads."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


DESKTOP = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_runtime", DESKTOP / "scripts" / "prepare-runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
verify_spec = importlib.util.spec_from_file_location("verify_runtime", DESKTOP / "scripts" / "verify-runtime.py")
verify = importlib.util.module_from_spec(verify_spec)
verify_spec.loader.exec_module(verify)


class PackagingTests(unittest.TestCase):
    def test_cached_archive_is_still_hash_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "python.tar.gz"
            archive.write_bytes(b"changed archive")
            with patch.object(runtime.urllib.request, "urlopen") as network:
                with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                    runtime.download("https://example.invalid/runtime", archive, "0" * 64)
                network.assert_not_called()
                runtime.download("https://example.invalid/runtime", archive, hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_clean_source_excludes_stale_build_and_private_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            (root / "omnibci/conf/profiles").mkdir(parents=True)
            (root / "omnibci/__pycache__").mkdir()
            (root / "build/lib/omnibci/conf/profiles").mkdir(parents=True)
            (root / "tests/fixture").mkdir(parents=True)
            for name in ("pyproject.toml", "omnibci/conf/settings.toml", "omnibci/runtime.py",
                         "omnibci/conf/profiles/private.toml", "omnibci/conf/.secrets.toml",
                         "omnibci/__pycache__/runtime.pyc", "build/lib/omnibci/conf/profiles/removed.toml",
                         "tests/fixture/train.cnt"):
                (root / name).write_text("fixture")
            destination = Path(temporary) / "source"
            runtime.copy_source(root, destination)
            copied = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
            self.assertEqual(copied, {"pyproject.toml", "omnibci/conf/settings.toml", "omnibci/runtime.py"})

    def test_supported_targets_have_separate_hashes_and_interpreters(self):
        versions = json.loads((DESKTOP / "runtime/versions.json").read_text())
        targets = versions["targets"]
        self.assertEqual(set(targets), {"win32-x64", "darwin-arm64", "darwin-x64", "linux-x64"})
        self.assertEqual(len({target["sha256"] for target in targets.values()}), 4)
        for name, configuration in targets.items():
            self.assertRegex(configuration["sha256"], r"^[a-f0-9]{64}$")
            self.assertTrue((DESKTOP / "runtime/locks" / f"{name}.txt").is_file())

    def test_host_target_uses_interpreter_architecture(self):
        for operating_system, machine, expected in (("win32", "AMD64", "win32-x64"),
                                                     ("darwin", "arm64", "darwin-arm64"),
                                                     ("darwin", "x86_64", "darwin-x64"),
                                                     ("linux", "x86_64", "linux-x64")):
            with self.subTest(target=expected), patch.object(runtime.sys, "platform", operating_system), patch.object(runtime.platform, "machine", return_value=machine):
                self.assertEqual(runtime.host_target(), expected)

    def test_newer_host_wheels_cannot_raise_the_product_minimum(self):
        cases = (
            ({"target": "darwin-x64", "minimumMacOS": [13, 0]}, "macosx_10_15_x86_64", "macosx_14_0_x86_64"),
            ({"target": "darwin-arm64", "minimumMacOS": [13, 0]}, "macosx_12_0_arm64", "macosx_10_15_x86_64"),
            ({"target": "linux-x64", "maximumGlibc": [2, 35]}, "manylinux_2_28_x86_64", "manylinux_2_36_x86_64"),
            ({"target": "win32-x64"}, "win_amd64", "win_arm64"),
        )
        for manifest, compatible, incompatible in cases:
            with self.subTest(target=manifest["target"]):
                verify.validate_wheel_platform("numpy", f"Tag: cp314-cp314-{compatible}", manifest)
                with self.assertRaisesRegex(AssertionError, "baseline or architecture"):
                    verify.validate_wheel_platform("numpy", f"Tag: cp314-cp314-{incompatible}", manifest)


if __name__ == "__main__":
    unittest.main()
