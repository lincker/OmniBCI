#!/usr/bin/env python
"""
Export the OmniBCI OpenAPI spec to a JSON file.

The spec is the single source of truth for the REST contract; the frontend
generates its TypeScript types from it (see docs/frontend-separation.md).

Usage (run inside the project venv):

    python scripts/export_openapi.py                 # → docs/openapi.json
    python scripts/export_openapi.py path/to/out.json

Generating ``openapi()`` only introspects the routes; it does NOT run the
FastAPI lifespan, so no NeuroEngine / sockets are started.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def export(out_path: str = "docs/openapi.json") -> tuple[Path, int]:
    # Imported lazily so --help / import errors surface clearly.
    from omnibci.presentation.main import create_app

    spec = create_app().openapi()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out, len(spec.get("paths", {}))


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "docs/openapi.json"
    out, n_paths = export(target)
    print(f"Wrote {out} ({n_paths} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
