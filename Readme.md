<!--
 * @Author: lincker
 * @Date: 2025-04-28 14:21:21
 * @LastEditors: lincker haolin.d.wu@gmail.com
 * @LastEditTime: 2025-12-23 09:42:20
 * @FilePath: /OmniBCI/Readme.md
 * @Description: 
 * 
 * Copyright (c) 2025 by lincker, All Rights Reserved. 
-->
# OmniBCI

OmniBCI is a cross-platform brain-computer interface (BCI) framework for EEG
acquisition, real-time signal processing, visual stimulation, external-device
control, and browser-based system management. Data sources, decoding algorithms,
stimulation paradigms, and effectors can be extended through plugins. The system
runs on Windows, macOS, and Linux.

## Features

- File playback and multiple EEG acquisition devices.
- Pluggable epoch detection, preprocessing filters, and decoding algorithms.
- Visual stimulation in an independent process.
- External-device control through effector plugins.
- A REST API, real-time WebSocket results, and a React web console.
- Automatic subprocess management on one machine or distributed stimulation
  and actuation across multiple machines.

## System Architecture

### Subsystem View

```text
+-------------------+       REST / WebSocket       +-------------------+
| React Web Console | <--------------------------> | FastAPI           |
+-------------------+                              +---------+---------+
                                                            |
                                                  control / status
                                                            |
                                                            v
+---------------+  EEG  +---------------+  chunks  +----------------+
| Data Provider | ----> | Acquisition   | -------> | NeuroEngine    |
| device / file |       | connect/stream|          | epoch/filter/  |
+---------------+       +---------------+          | decode         |
                                                  +-------+--------+
                                                          |
                                  +-----------------------+-----------------------+
                                  |                                               |
                         trial control / feedback                    commands / state / acks
                         (bidirectional TCP IPC)                      (bidirectional TCP IPC)
                                  |                                               |
                                  v                                               v
                       +--------------------+                          +--------------------+
                       | Stimulation        |                          | Actuation          |
                       | paradigm/rendering |                          | validation/effector|
                       +--------------------+                          +---------+----------+
                                                                                |
                                                                        actions / feedback
                                                                                |
                                                                                v
                                                                      +--------------------+
                                                                      | External Device    |
                                                                      +--------------------+

+------------------------------------------------------------------------------------------+
| Plugin System: catalog -> descriptors -> loader                                         |
| SignalSource: Data Provider        | Epoch / Filter / Decoder: NeuroEngine               |
| Paradigm: Stimulation              | Effector: Actuation                                 |
+------------------------------------------------------------------------------------------+
```

Compared with the original subsystem model, the control GUI is now split into
a React frontend and a FastAPI boundary, while external-device control is
represented explicitly by the Actuation subsystem.

### Code Layers

The project follows domain-driven design and Clean Architecture. Dependencies
point inward:

| Layer | Directory | Responsibility |
| --- | --- | --- |
| Presentation | `omnibci/presentation` | FastAPI routes, request/response schemas, WebSocket endpoints, and error mapping |
| Application | `omnibci/application` | Use-case orchestration, commands, queries, application services, and ports |
| Domain | `omnibci/domain` | Sessions, signals, epochs, classification results, and core interfaces |
| Infrastructure | `omnibci/infrastructure` | Acquisition, signal processing, IPC, process management, plugins, persistence, and external-device adapters |
| Shared kernel | `omnibci/shared` | Cross-process event contracts and common utilities with no internal package dependencies |
| Configuration | `omnibci/conf` | Read-only defaults, user host settings, and worker snapshots |
| Composition root | `omnibci/bootstrap.py` | Selects concrete implementations from configuration and assembles the object graph |

The Application layer does not depend directly on Infrastructure.
Infrastructure implements ports defined by Domain and Application, while
concrete bindings are centralized in the composition root. The import-linter
contracts in `pyproject.toml` continuously enforce these boundaries.

### Plugins

Built-in plugins live in `omnibci/infrastructure/plugins`. The plugin catalog
reads each plugin's `manifest.toml` without importing its Python code. Concrete
implementations are loaded on demand at the relevant execution boundary.

To write a third-party Decoder, open **Extensions → Decoder developer guide**
in the app, or read [the authoring guide](docs/decoder-plugin-development.md).
The [executable starter](examples/decoder_plugin/) includes a commented manifest,
Python implementation and Profile parameter schema.

Configure the user plugin directory with the `OMNIBCI_PLUGIN_DIR` environment
variable or `PLUGINS.user_dir` in the user configuration directory's
`settings.toml`. Restart the backend after changing this deployment setting.

## Getting Started

### Requirements

- Python 3.14 or later.
- Node.js 18.x or 20 or later, with npm.
- An OpenGL-capable graphical environment for visual stimulation.

### 1. Install the Backend

Run the following commands from the repository root:

```bash
python3.14 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On Windows, create and activate the virtual environment with PowerShell:

```powershell
py -3.14 -m venv venv
venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Install the core runtime without visual-stimulation or development extras with:

```bash
python -m pip install -e .
```

Install the visual-stimulation dependencies without development tools with:

```bash
python -m pip install -e ".[stim]"
```

### 2. Install the Frontend

```bash
cd frontend
npm ci
cd ..
```

### 3. Configure the Data Source

After starting the backend and console below, use **Setup → Acquisition** to
choose an installed source plugin and a named Plugin Profile. Scan45, Curry8,
and File Reader can each have multiple saved profiles. For File Reader, set
`data_file_path` in the Profile to an EEG file available on the acquisition
machine. Save, review, and apply the selection before starting the engine.

Shared host controls are edited in **Settings → General**. They are saved in
the backend user's `settings.toml`, separately from Plugin Profiles and the
accepted `activation.toml`. Saving never changes an active experiment; use
Review and Apply to apply supported changes. Conflicting edits require a
refresh before saving again.

The user directory defaults to `~/Library/Application Support/OmniBCI` on macOS,
`~/.config/OmniBCI` on Linux (honoring `XDG_CONFIG_HOME`), and
`%LOCALAPPDATA%/OmniBCI` on Windows. `OMNIBCI_CONFIG_DIR` can select an absolute
directory. The packaged `omnibci/conf/settings.toml` supplies read-only defaults;
retired named Application Profile overlays are not loaded.

### 4. Start the Backend

Open a terminal, activate the virtual environment, and run this command from the
repository root:

```bash
uvicorn omnibci.presentation.main:app --host 127.0.0.1 --port 8000
```

Useful endpoints:

- Health check: <http://127.0.0.1:8000/healthz>
- API documentation: <http://127.0.0.1:8000/docs>
- OpenAPI schema: <http://127.0.0.1:8000/openapi.json>

Append `--reload` during API development if automatic reloads are useful.
Starting the API process initializes services and listeners. Start NeuroEngine
explicitly through the web console or API to resolve the selected profile and
build the engine graph.

### 5. Start the Web Console

Open another terminal:

```bash
cd frontend
npm run dev
```

Open <http://localhost:5173> and perform these steps:

1. Select source/decoder plugins and Profiles in Setup, then review and apply.
2. Start the engine.
3. Connect the data source.
4. Start reading.
5. Stop reading after playback finishes to end the active session.

Vite proxies `/api` and WebSocket traffic to
`http://127.0.0.1:8000` by default. To use a backend on another machine:

```bash
cd frontend
VITE_BACKEND=http://192.168.1.10:8000 npm run dev
```

### Distributed Stimulation and Actuation

To run stimulation on another machine, save `STIMULATION.auto_spawn = false`
and the engine's reachable `NEUROENGINE.message_server_ip` / `message_server_port`
in the host settings file. Review and apply these host settings while the engine
is stopped. Install the pinned Paradigm and Trigger packages on the stimulation
node, then run (replace the example address with your backend):

```bash
python scripts/run_stimulation.py --backend http://192.168.1.10:8000
```

For Actuation, save `ACTUATION.enabled = true`, `ACTUATION.auto_spawn = false`,
and its `server_ip` / `server_port` in host settings. Select the Effector Profile
in Setup, review and apply the configuration, and install the pinned Effector
package before running the following command on the device machine:

```bash
python scripts/run_actuation.py --backend http://192.168.1.10:8000
```

`OMNIBCI_BACKEND_URL` can supply the backend URL instead; the default is
`http://127.0.0.1:8000`. Nodes wait for the backend engine to start, fetch their
role-specific applied snapshot, and validate only their own plugins. No local
Activation or Profile files are needed. The backend retains the complete
configuration catalog for authoring; output platform checks run on their node.

The HTTP address and both configured IPC addresses must be reachable from the
respective nodes. Use the backend's LAN IP for the IPC settings when running
across machines: `127.0.0.1` is local-only, and `0.0.0.0` is a listener address,
not a remote destination. Allow the configured HTTP and TCP ports.

After plugin initialization, a socket handshake verifies the node's role,
Activation revision, and engine generation before accepting business events.
On disconnection the node tears down, fetches configuration again, and starts a
fresh instance only after cleanup succeeds. Stimulation clears its trigger;
Actuation requires the Effector to confirm that physical outputs have stopped.
Hardware/setup, output-settlement, or cleanup errors stop the script for inspection.
Saving a Profile does not change a running
node. To change configuration with external nodes, stop the engine, apply, then
start the engine; waiting node scripts synchronize automatically. Coordinated
live updates of external nodes remain a separate phase.
See [configuration ownership](docs/configuration-ownership.md) for the boundaries.
