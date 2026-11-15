"""Read-only host compatibility checks shared by discovery and installation."""
from importlib import metadata
import re


def installed_host_version() -> str | None:
    try:
        return metadata.version("omnibci")
    except metadata.PackageNotFoundError:
        return None


def _numeric_version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
        raise ValueError("Host versions must contain numeric dot-separated components")
    return tuple(int(part) for part in value.split("."))


def check_host_compatibility(package, host_version: str | None) -> None:
    if "host_version" in package:
        expected = package["host_version"]
        if not isinstance(expected, str) or not expected or expected != host_version:
            raise ValueError("Package requires a different exact OmniBCI host version")
    if "engines" not in package:
        return
    engines = package["engines"]
    if not isinstance(engines, dict) or set(engines) != {"omnibci"} or not isinstance(engines["omnibci"], str):
        raise ValueError("package.engines must contain only an omnibci version requirement")
    host = _numeric_version(host_version)
    for clause in engines["omnibci"].split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*([0-9]+(?:\.[0-9]+)*)\s*", clause)
        if match is None:
            raise ValueError("Unsupported engines requirement; use comma-separated >=, >, <=, < or == numeric versions")
        operator, raw = match.groups()
        expected = _numeric_version(raw)
        length = max(len(host), len(expected))
        actual = host + (0,) * (length - len(host))
        expected += (0,) * (length - len(expected))
        matches = {">=": actual >= expected, ">": actual > expected, "<=": actual <= expected,
                   "<": actual < expected, "==": actual == expected}
        if not matches[operator]:
            raise ValueError(f"Package engines requirement {engines['omnibci']!r} does not match OmniBCI {host_version}")
