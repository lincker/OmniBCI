# Configuration and plugin validation

## Automated checks

The `tests` workflow configures Python 3.14 jobs on GitHub's Linux, macOS, and
Windows runners. Each OS runs the headless unit suite and a separate integration
suite using real subprocesses, local IPC, and checked-in signal fixtures. The
headless suite includes runtime failure injection for drain, projection,
checkpoint, startup, persistence, output settlement, and unknown resume outcomes.

Each OS also generates the frontend REST types, checks that they match the
committed API contract, and builds the console. A fresh wheel replaces the
editable installation before isolated Python verifies bundled plugin manifests,
schemas/defaults, host settings, and all 281 stimulus frames. This catches assets
that work in a checkout but are missing from the distributed package.

Build release wheels from a clean checkout. Setuptools can retain deleted files
in a previous `build/lib` directory; a stale parent plugin manifest can hide newer
contributions. The isolated wheel check rejects an import from the source tree
and detects missing contributions caused by such artifacts.

The separate `contract` workflow compares a newly exported OpenAPI document with
the committed document. Import-linter checks the architecture dependency rules.

## What these checks establish

Local verification on 2026-09-24: 1,927 headless/backend tests passed, including
real-process integration. The final source-capability change was then rechecked
with 49 relevant coordinator, failure, and process tests. All 18 frontend tests,
the production build, generated REST type check, and five import contracts passed.
A clean temporary wheel verified 10 configured contributions and 281 stimulus
frames without importing the source checkout.

Local results only establish behavior on the machine that ran them. Adding a CI
matrix does not establish that Windows or Linux jobs have passed: inspect the
actual workflow results for each commit. The current development environment is
macOS; other OS results require their corresponding runners.

The local console check used isolated configuration and plugin directories. It
verified profile editing, saving without activation, Apply review and completion,
the Run module-progress view, ZIP upload, explicit enablement, and the disabled
repository state. Frontend tests additionally cover uncertain Apply responses,
recovery after a confirmed stop, exact package identities, and version-profile
creation without automatic activation. Signed repository installation is tested
through a controlled HTTP repository and the application's real lifespan.

Final review regressions cover cancellation after a live network packet header,
safe resumption after source-version replacement, and clearing runtime quarantine
without erasing historical failed operations. Timing and sample continuity still
require the hardware checks below.

Headless tests do not validate physical EEG connectivity, parallel-port trigger
timing, display refresh/VSync behavior, or safe stopping of a physical actuator.
Those require the relevant hardware and display setup. Tests tagged `gui` or
`hardware` are excluded from both headless jobs. Fake effectors establish host
protocol ordering only; an installed driver must implement and validate its own
physical quiescence contract before automatic reconfiguration can use it.

## Local commands

```sh
python -m pytest -m "not gui and not hardware and not integration"
python -m pytest -m "integration and not gui and not hardware"
lint-imports
python scripts/export_openapi.py
npm --prefix frontend run codegen
npm --prefix frontend test
npm --prefix frontend run build
```
