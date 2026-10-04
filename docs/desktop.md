# OmniBCI desktop

The desktop application uses Electron with the existing web console and a bundled
CPython backend. Each installer supports both **Local** and **Remote** connections.
Local mode starts the included backend; remote mode connects to an existing server
without starting a local backend. The first release is for internal installation:
there is no automatic updater, public release publishing, or store distribution.

## First launch and data

Choose a connection mode on the connection screen. For local work, select your own
CNT recording in the acquisition settings before starting the engine. An empty
data-source configuration is valid while editing; it must not start acquisition.
The installer contains no demonstration recording or repository test fixtures.
In remote mode, data paths refer to the backend machine; the desktop file picker
cannot select files on that machine.

The desktop remembers the last connection mode and remote address, but does not
persist authentication tokens. Local backend credentials are generated for each
launch and remain in the Electron main process. Switching connections clears the
previous connection's runtime state and result subscriptions.

Export results before stopping a local backend: sessions are currently held in
backend memory. The desktop asks before stopping active work or discarding results.
Disconnecting from a remote backend leaves that service and its experiment running.
No user configuration, plugins, logs, recordings, or exports are stored in the
application's installation directory. Desktop preferences and logs live below
Electron's per-user `userData` directory, in `desktop/connection.json` and
`desktop/logs/`. Existing backend platform-specific user paths remain in use.

## Run a remote backend

On the backend machine, install OmniBCI with its production/stimulation dependencies
and start the normal server entrypoint with a strong private token:

```sh
OMNIBCI_API_TOKEN='replace-with-a-long-random-token' \
  python -m omnibci.presentation.server --host 0.0.0.0 --port 8000
```

In PowerShell set `$env:OMNIBCI_API_TOKEN` first, then run the same Python command.
Enter the server's base address, such as `http://backend-host:8000`, and its token
in the desktop's Remote connection form. Set the same token environment variable
on separately launched stimulation/actuation nodes that need to authenticate to
that API. Do not place credentials in the URL.

HTTP is intended for trusted local networks. For access across untrusted networks,
put the API behind an HTTPS reverse proxy with a normally trusted certificate and
WebSocket forwarding. Enter its `https://` base address; certificate validation is
not disabled by the desktop. The proxy must forward authentication headers.

`--desktop` is a separate, private JSONL control protocol over inherited
stdin/stdout. It is used only for an owned local backend and does not expose a
public HTTP shutdown endpoint. Closing the parent's pipe initiates normal cleanup.
On Windows, the backend owns the process-tree Job Object; its exit closes that job.
Electron's death alone does not guarantee immediate worker termination if the
backend is stuck before it can process EOF. Forced process termination also cannot
confirm that physical acquisition or stimulation hardware has stopped. Verify
device shutdown independently in the hardware validation procedure.

## Supported build targets

| Target | Native CI runner | Internal installer | Product baseline |
| --- | --- | --- | --- |
| `win32-x64` | `windows-2022` | NSIS `.exe`, per-user | Windows 10 22H2 / Windows 11, x64 |
| `darwin-arm64` | `macos-15` | `.dmg` | macOS 13+, Apple Silicon |
| `darwin-x64` | `macos-15-intel` | separate `.dmg` | macOS 13+, Intel |
| `linux-x64` | `ubuntu-22.04` | `.deb` | Ubuntu 22.04 / 24.04, x64 desktop |

The macOS packages are separate architectures, not a universal binary. Windows
ARM64 and other Linux distributions are outside the initial validation matrix.
Python's own operating-system minimum is not the application's minimum: Electron,
NumPy/SciPy and other native wheels also constrain compatibility. Linux locks use
the glibc 2.35 target; the Debian package declares that minimum and the system GUI
and OpenGL libraries. macOS source extension builds target 13.0. Dependency
installation explicitly selects the baseline platform even when the build host is
newer: NumPy/SciPy publish both older-baseline and macOS 14-only wheels. The verifier
rejects incompatible wheel tags and audits actual Mach-O deployment versions or
ELF GLIBC symbol requirements before packaging. Windows wheel tags identify the
architecture but cannot prove Windows 10 compatibility.

CI on a newer OS does not prove the minimum OS works. Before distributing a build,
run the installation checklist below on the product baselines as well as CI hosts.
Hardware drivers, acquisition devices, GPU timing and stimulation latency require
the validation described in [validation.md](validation.md).
The automated stages below are required checks, not a claim that every target has
already passed. Consult the specific CI run and its manifests/logs for that evidence;
clean-machine minimum-OS and physical-device validation remain separate release gates.

## Runtime layout and locked inputs

`desktop/runtime/versions.json` pins CPython **3.14.8**, the Astral
[python-build-standalone 20261003 release](https://github.com/astral-sh/python-build-standalone/releases/tag/20261003),
the four GIL-enabled `install_only_stripped` archives and their SHA256 values.
Electron **44.5.1**, electron-builder **26.17.0**, and the JavaScript tooling are
fixed in `desktop/package.json` and `package-lock.json`.

The installer has this structure (inside `.app/Contents/Resources` on macOS and
`resources` on Windows/Linux):

```text
app.asar                         # Electron main, preload, built renderer, JS dependencies
python/
  bin/python3                    # macOS/Linux interpreter
  python.exe                     # Windows interpreter
  lib/... or Lib/...             # Standard library and installed runtime packages
runtime-manifest.json
third-party-licenses/
```

Python lives outside `app.asar`. It is a real, relocatable interpreter, preserving
`sys.executable` and `multiprocessing` spawn semantics. The runtime retains plugin
`.py` sources, manifests, JSON schemas, `.dist-info` metadata, native libraries,
and the compiled stimulus frame pack. These are required by the plugin loader,
package compatibility checks and resource APIs; do not prune them as unused files.

There are four committed hash-checked dependency locks in `desktop/runtime/locks`.
They resolve the production project plus the `stim` extra, never `dev`. Packages
must have compatible wheels except the explicitly listed `moderngl` and `glcontext`
source builds. Those two archives are hash checked and built on the target machine
using pinned build constraints. This is necessary because ModernGL 5.12.0 has no
CPython 3.14 wheel for every target. macOS x64 also resolves a different compatible
cryptography version from the other targets. Build prerequisites are C/C++ tools
(Xcode Command Line Tools, Visual Studio Build Tools, or GCC/G++) and Linux X11
headers. Users do not need Python, pip, a compiler, or a development environment.

`prepare-runtime.py` verifies the downloaded archive, installs the matching lock
into the extracted interpreter, then builds OmniBCI from a clean temporary source
copy. It excludes machine profiles and bytecode, and cannot reuse a stale
`build/lib` directory from development. The application wheel is installed with
`--no-deps`. A final check rejects development packages, source test recordings,
and machine-specific profiles. Downloads and generated resources are ignored by
Git. A development virtualenv is never copied into the installer.

`runtime-manifest.json` records application, Electron and Python versions, target,
minimum OS, archive URL/hash, lock hash, installed dependency versions, wheel hash,
and build tool versions. The builder rejects a runtime from another target or
application version. It also records the Git commit, whether relevant source files
are modified, and the native compatibility audit. Upstream PBS license notices are vendored at the pinned
release commit with source URLs/hashes; package notices and metadata are collected
from the installed distributions. Electron and its JavaScript dependencies retain
their own notices in the app distribution.

## Local development

Use Node.js 22.12 or newer and Python 3.14. Install the frontend and desktop tooling:

```sh
npm ci --prefix frontend
npm ci --prefix desktop
npm --prefix frontend run build
npm --prefix desktop run build
npm --prefix desktop run typecheck
npm --prefix desktop test
```

For fast iteration, install the backend into a development environment and give
the desktop its **absolute** interpreter path:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
OMNIBCI_DESKTOP_PYTHON="$PWD/.venv/bin/python" npm --prefix desktop run dev
```

In PowerShell, use `.venv\Scripts\python.exe` and set
`$env:OMNIBCI_DESKTOP_PYTHON` to its absolute path before `npm --prefix desktop run dev`.
For renderer hot reload, run `npm --prefix frontend run dev` separately and set
`OMNIBCI_DESKTOP_DEV_URL=http://localhost:5173`. That override is development-only.

## Build and verify an installable app

Build each target on its native OS and architecture. The following commands use
`python3`; on Windows use `python` instead. Install the pinned resolver in your
build environment, not the application runtime:

```sh
python3 -m pip install uv==0.12.23
npm ci --prefix frontend
npm ci --prefix desktop
npm --prefix frontend run build
python3 -B desktop/scripts/prepare-runtime.py
npm --prefix desktop run package:dir
python3 -B desktop/scripts/verify-runtime.py --packaged desktop/release
node desktop/scripts/smoke.mjs --packaged desktop/release
npm --prefix desktop run package
```

On a Linux build host install `libx11-dev`, the GUI/OpenGL libraries listed in the
workflow, and `xvfb`; run the Electron smoke command under `xvfb-run -a` when there
is no desktop session. `--target` can explicitly select a target, but the runtime
script rejects cross-platform or cross-architecture execution. `--cache-dir` can
reuse the checked PBS download. `OMNIBCI_BUILD_PYTHON` selects the bootstrap Python
for the npm `runtime:prepare` / `runtime:verify` wrappers, and `UV` selects the
pinned resolver executable.

The Python verification runs **the bundled Python version**, in isolated mode and
from a temporary working directory, both before and after packaging. It checks
installed versions/import locations, loads plugin sources, validates schemas,
reads all 281 stimulus frames, and starts a real spawned process that repeats
plugin/resource checks with the same interpreter. The Electron smoke test drives
the real window, preload, API/WebSocket connection and local/remote lifecycle.
Its test fixtures are external inputs and never copied into the installer.

Outputs are under `desktop/release`. Every package command passes `--publish never`.
The `package` command wraps the existing verified app with `--prepackaged`; it does
not rebuild or re-sign its payload after the smoke test. Run `package:dir` again
when changing the renderer, main process or runtime, then repeat verification.
Windows builds are unsigned; macOS builds use explicit ad-hoc signing without
notarization or hardened runtime. These internal packages may require the OS's
normal first-open approval. This is not a public distribution signing setup.

## CI and dependency updates

[desktop.yml](../.github/workflows/desktop.yml) runs inexpensive contract, unit and
packaging-input checks on relevant main/dev/feat-ui pushes and pull requests.
Relevant pull requests and manual dispatch also run four independent native jobs.
Each native job builds the UI and runtime,
packages an unpacked app, verifies relocated Python and exercises the actual app.
The app is exercised from a separate installation path containing spaces and
Unicode, with write bits removed while preserving executable bits. Windows file
read-only attributes are checked this way; full Windows directory ACL behavior
still requires clean-machine installation testing.
The workflow uploads a tar archive preserving executable permissions and symlinks,
the runtime manifest, artifact SHA256 checksums, build logs and smoke diagnostics.
Manual dispatch additionally builds
internal installers when the `installers` input is enabled. It has read-only
repository permissions and does not create GitHub releases.
macOS installer jobs also mount the DMG read-only, verify its code signature and
compare every app entry against the smoke-tested payload, then detach the image.

To update Python or PBS, update `runtime/versions.json` only after checking all
four official assets and their hashes. Update the vendored PBS license notices
and `SOURCES.json` to that release commit. With the pinned uv resolver and the matching
native Python 3.14.8 interpreter available, refresh the four dependency locks:

```sh
python3 -B desktop/runtime/resolve-locks.py --python desktop/resources/python/bin/python3
```

This preserves compatible existing pins; use an intentional resolver update when
changing dependency versions. If the dependency graph adds a native package,
require a wheel or explicitly review its source build before changing the build
allowlist. Rebuild and verify every target after changing any runtime pin. Update
desktop and backend application versions together. Check native-wheel minimum OS
requirements when raising versions, not only package names or resolver success.

## Internal installation checklist

Run this on a clean machine with no system Python, including a path containing
spaces and non-ASCII characters:

1. Install and launch. Confirm the empty workspace is usable and no data is
   silently selected. Starting acquisition without a source must give a clear error.
2. Select a real recording on that machine; start, stop and export results. Confirm
   the output opens and survives app restart.
3. Close or switch connections during work. Check cancellation, graceful stop,
   the unexported-results prompt and cleanup of owned backend/worker processes.
4. Connect to a separate remote backend with authentication. Check live results,
   reconnect behavior and that disconnecting does not stop the remote experiment.
5. Restart and reinstall. Confirm per-user configuration is retained and no
   resources were written into the installed application. Check logs and the
   runtime manifest when diagnosing a startup failure.
6. On macOS test both architectures and verify nested native code signatures on a
   second machine. On Linux test Ubuntu 22.04 and 24.04 with their normal desktop
   libraries. Record OS version, architecture, artifact hash and results.
