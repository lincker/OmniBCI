# Configuration and plugin validation

## Automated checks

The `tests` workflow configures Python 3.14 jobs on GitHub's Linux, macOS, and
Windows runners. Each OS runs the headless unit suite and a separate integration
suite using real subprocesses, local IPC, and checked-in signal fixtures. The
headless suite includes runtime failure injection for drain, projection,
checkpoint, startup, persistence, output settlement, and unknown resume outcomes.

Managed stimulation and actuation children explicitly use the `spawn` context so
each launch inherits the caller's current deployment environment. A regression
starts a forkserver before changing the plugin directory, then checks that both
managed children receive the updated directory without changing pytest's global
process start method.

An independent Linux job generates the frontend REST types, checks that they
match the committed API contract, runs the frontend tests, and builds the console.
It runs even when backend checks fail. On each OS, a fresh wheel replaces the
editable installation before isolated Python verifies bundled plugin manifests,
schemas/defaults, host settings, and all 281 stimulus frames. This catches assets
that work in a checkout but are missing from the distributed package. The wheel
checks still run after a headless test failure if dependency installation
succeeded; cancellation stops them, and test failures still fail the job.

Build release wheels from a clean checkout. Setuptools can retain deleted files
in a previous `build/lib` directory; a stale parent plugin manifest can hide newer
contributions. The isolated wheel check rejects an import from the source tree
and detects missing contributions caused by such artifacts.

The separate `contract` workflow compares a newly exported OpenAPI document with
the committed document on pull requests and pushes to `dev` or `main`.
Import-linter checks the architecture dependency rules, including the prohibition
on domain imports of infrastructure. A push to either branch or a pull request
runs nine checks: three headless, three integration, frontend, architecture, and
OpenAPI. Configure these check names as required in branch protection.

## What these checks establish

The acquisition-profile montage follow-up on 2026-10-10 passed focused backend
batches of 195 configuration/API/domain checks, 125 profile/storage checks, 26
signal/API-contract checks, 61 acquisition/bootstrap checks, and 34 processing/API
checks including real CNT acquisition across processes. These batches overlap.
Coverage includes persistent source observations, missing-field manual fallback,
read-only device facts, stale-profile and concurrent-write rejection, pinned
manual inputs, connection invalidation, and device/configuration conflicts.
All 70 frontend tests, type checking, production build, OpenAPI/REST consistency,
and six architecture contracts passed. Frontend regressions also cover changed
channel counts, generated names, and avoiding reuse of a file's observed
coordinates as manual declarations for another file. The full backend suite and
physical-device/other-OS release checks were not rerun for this follow-up.

Montage validation on 2026-10-10 used the existing Python 3.14 environment on
macOS. The final focused suites passed 153 runtime/API tests and 224
domain/configuration/plugin tests (these suites overlap). The complete integration
suite passed 24 tests with one skip; the real CNT montage and command-confirmation
process tests also passed after the final acquisition fix. The full headless run
had 2,361 passes and five failures: a lightweight source test double lacked a
connection property, and the event-routing expectation omitted the two new
commands. Those failures were corrected and covered by the final focused suites;
the complete headless suite was not rerun after those fixes.

All 57 frontend tests, type checking, production build, OpenAPI/REST type
consistency, and six architecture contracts passed. A clean temporary wheel was
installed outside the checkout and checked without the editable-package import
hook: all ten contributions, 281 stimulus frames, new montage modules, and the
installed MNE electrode template resolved successfully. Template coordinates
were explicitly verified in the head frame and meters. Browser screenshot QA
was unavailable because the computer-use runtime exposed no browser surfaces.
These checks do not establish physical Scan45/CURRY behavior or Windows/Linux
results; those remain release validation work.

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
