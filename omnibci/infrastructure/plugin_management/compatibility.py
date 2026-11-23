"""Read-only host compatibility checks shared by discovery and installation."""
from __future__ import annotations

from importlib import metadata
import re


_HOST_LIBRARIES = (
    "numpy", "scipy", "mne", "scikit-learn",
    "pyopengl", "glfw", "moderngl", "pillow",
)
_NUMERIC_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_COMPARATOR_CLAUSE_RE = re.compile(r"\s*(>=|<=|==|>|<)\s*([0-9]+(?:\.[0-9]+)*)\s*")


def normalize_dependency(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_host_libraries() -> dict[str, str]:
    versions = {}
    for name in _HOST_LIBRARIES:
        try:
            versions[normalize_dependency(name)] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return versions


def _numeric_version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not _NUMERIC_VERSION_RE.fullmatch(value):
        raise ValueError("Versions must contain numeric dot-separated components")
    return tuple(int(part) for part in value.split("."))


def _matches_numeric_clauses(actual_version: str, spec: str, *, label: str) -> bool:
    host = _numeric_version(actual_version)
    clauses = spec.split(",")
    if not clauses:
        raise ValueError(f"Unsupported {label} requirement: {spec!r}")
    for clause in clauses:
        match = _COMPARATOR_CLAUSE_RE.fullmatch(clause)
        if match is None:
            raise ValueError(
                f"Unsupported {label} requirement; use exact version or comma-separated >=, >, <=, < or == numeric versions"
            )
        operator, raw = match.groups()
        expected = _numeric_version(raw)
        length = max(len(host), len(expected))
        actual = host + (0,) * (length - len(host))
        expected += (0,) * (length - len(expected))
        matches = {
            ">=": actual >= expected,
            ">": actual > expected,
            "<=": actual <= expected,
            "<": actual < expected,
            "==": actual == expected,
        }
        if not matches[operator]:
            return False
    return True


def _validate_dependency_spec(spec: str) -> bool:
    """Return True for a comparator-range spec, False for a bare exact version."""
    if not isinstance(spec, str) or not spec or spec != spec.strip():
        raise ValueError("Invalid host dependency version requirement")
    if any(spec.startswith(op) for op in (">=", "<=", "==", ">", "<")) or "," in spec:
        for clause in spec.split(","):
            if _COMPARATOR_CLAUSE_RE.fullmatch(clause) is None:
                raise ValueError(
                    "Unsupported host dependency requirement; use an exact version or comma-separated >=, >, <=, < or == numeric versions"
                )
        return True
    if not re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,127}", spec) or spec.endswith("."):
        raise ValueError(f"Invalid host dependency requirement: {spec!r}")
    return False


def check_dependencies(requirements, available: dict[str, str]) -> None:
    if not isinstance(requirements, dict):
        raise ValueError("package.requires must map host library names to version requirements")
    seen = set()
    for name, spec in requirements.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Invalid host dependency name")
        normalized = normalize_dependency(name)
        if normalized in seen:
            raise ValueError(f"Duplicate host dependency requirement: {name}")
        is_range = _validate_dependency_spec(spec)
        seen.add(normalized)
        installed = available.get(normalized)
        if installed is None:
            raise ValueError(f"Unavailable or incompatible host dependency: {name} ({spec})")
        if is_range:
            if not _matches_numeric_clauses(installed, spec, label="host dependency"):
                raise ValueError(
                    f"Unavailable or incompatible host dependency: {name} {spec} (installed {installed})"
                )
        elif installed != spec:
            raise ValueError(f"Unavailable or incompatible host dependency: {name}=={spec}")


def installed_host_version() -> str | None:
    try:
        return metadata.version("omnibci")
    except metadata.PackageNotFoundError:
        return None


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
    if host_version is None:
        raise ValueError("Host versions must contain numeric dot-separated components")
    if not _matches_numeric_clauses(host_version, engines["omnibci"], label="engines"):
        raise ValueError(
            f"Package engines requirement {engines['omnibci']!r} does not match OmniBCI {host_version}"
        )
