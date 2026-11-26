# Validation

This document describes the current checks. Test counts and results belong to
the commit being reviewed, not to a permanent architecture specification.

## Automated checks

[tests.yml](../.github/workflows/tests.yml) runs Python 3.14 headless and
integration jobs on Linux, macOS, and Windows. Integration tests use real
subprocesses, local IPC, and checked-in signal fixtures. The same workflow checks
frontend tests and builds, generated REST types, and architecture imports.

The headless jobs build and install a wheel, then run
[check_installed_wheel.py](../.github/scripts/check_installed_wheel.py) in isolated
Python. It verifies API v1 manifests, schemas, settings defaults, the 18 canonical
SDK exports, the author CLI entry point, deterministic starter packaging and all
281 Classic40 stimulus frames, 11 Command10 label textures and ten synthetic
Command10 FBCCA predictions without importing the editable checkout. The two
Command10 packages are checked for identical target protocols. Build release wheels from a
clean checkout to avoid retaining deleted files in an old `build/lib` directory.

[contract.yml](../.github/workflows/contract.yml) regenerates OpenAPI and compares
it with [openapi.json](openapi.json). API changes must update that contract and
`frontend/src/api/rest-types.ts` together.

A configured CI matrix does not prove that every platform passed. Inspect the
workflow results for the actual commit. Local results establish only the behavior
on the machine where those checks ran.

## Local commands

Run from the repository root with the project's Python environment active:

```sh
python -m pytest -m "not gui and not hardware and not integration"
python -m pytest -m "integration and not gui and not hardware"
lint-imports
python scripts/export_openapi.py
npm --prefix frontend run codegen
npm --prefix frontend run codegen:check
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

Tests use isolated configuration and log directories. Local IPC integration tests
need permission to bind loopback ports. They must not use real user configuration
files as fixtures.

## Configuration and runtime coverage

The current regression suite checks:

- Unified configuration management with independent module/Profile storage, history and revision conflicts.
- Server/Engine value validation and revisions remain independent under one settings-file lock.
- Spawned runtime components consume explicit snapshots without reading configuration storage.
- One plugin catalog endpoint for all seven plugin kinds; retired discovery routes
  return 404 without redirects, and stimulation trial controls remain available.
- Engine and Server fields sharing one user settings file without lost updates,
  including failed writes and rollback after a concurrent Server save.
- Direct application of current values, without requiring a saved Profile or a
  separate Engine Save action.
- Startup snapshots, server restart requirements, and pending settings remaining
  separate from the running Engine and module state.
- Server routes and version 1 desktop/node protocols, with removed routes,
  obsolete fields and unsupported protocol versions rejected.
- Shared Web/desktop Server connections, Bearer and browser-cookie authentication,
  same-origin validation, and client disconnection without stopping shared runtime.
- Concurrent acquisition preview reads and runtime transitions, preserving capture
  identity checks without reserving module operations for a read-only viewer.
- Strict peer decoding and accepted-message liveness, uniform worker command
  pairs, current SDK signal descriptors and declared processing input contracts.
- Engine foundation startup and independent module start/stop, processing input
  attachment, FIFO drain boundaries, and recovery after failures.
- Bounded signal delivery and output sender queues. A full Processing input queue
  closes that input and marks Processing for recovery while Acquisition continues
  reading and previewing.
- Static source validation/packaging through the shared installer, fixed development
  snapshots, explicit companion packages and fresh normal hosts. Source edits cannot
  change a running snapshot; repeated interrupts and unconfirmed shutdown retain
  ownership. Development command-timeout overrides affect only isolated settings.
- File Reader → packaged starter Decoder → Logger through real HTTP/module paths,
  joining trial/configuration identity to output acknowledgement and private
  execution history, then confirming normal shutdown.
- Host-owned artifact/environment readiness receipts, candidate replacement and
  cancelled preparation; successful preparation does not imply activation or
  physical device delivery.
- Plugins navigation and downloadable API v1 guide/starter consistency.
- Frontend edit preservation during operations, explicit conflict handling,
  uncertain responses, and partial refresh failures.

The authoritative runtime and configuration description is
[module-config.md](module-config.md). Plugin signal contracts are documented in
[the SDK contract](plugin-manifest.md#sdk-and-construction-api); REST and WebSocket payloads are documented
in [frontend-separation.md](frontend-separation.md).

## Performance and physical-device checks

`python scripts/benchmark_epoch_buffer.py` compares the processing buffer with
repeated NumPy append operations. It checks output and dtype equality, measures
wall time and allocated memory, and isolates buffering from decoding and IPC.
It is a local microbenchmark, not an end-to-end latency guarantee.

Headless and simulated-device tests do not establish physical EEG connectivity,
parallel-port marker timing, display refresh/VSync behavior, or physical actuator
stopping. Validate those on the intended device and display. Tests marked `gui`
or `hardware` are excluded from the automated headless and integration jobs.
