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
- Pluggable epoch boundary detection, preprocessing filters, and decoding algorithms.
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
choose an installed source plugin and edit its current values. Scan45, Curry8,
and File Reader can each have multiple saved profiles. For File Reader, set
`data_file_path` to an EEG file available on the acquisition machine. Start the
Engine, then Connect to apply current values. Saving a reusable Profile is optional.

Shared controls are edited in **Settings → General** and use one backend user
`settings.toml`. Engine current values go directly to Review and Apply; successful
application stores them in `[engine]`. Backend startup settings are saved separately
on the page and take effect after restarting the backend. The page distinguishes
saved and active startup values. The four modules keep independent accepted files
in `activation/`, alongside separate per-plugin Profiles and module history.

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

1. Edit current source and processing parameters in Setup. Saving Profiles is optional.
2. Start the engine. This starts shared infrastructure only.
3. Connect the source and start Processing using their independent controls.
4. Start reading. Triggered epochs are decoded and published directly to Run.
5. Stop reading to drain queued samples, or let file playback reach its end.

Stimulation and Actuation have independent start/stop controls and do not require
Acquisition or Processing to run. Source-only reading works with Processing
stopped. Stopping the engine stops all modules.

Run shows the latest 200 decoded trials for the current engine generation.
Restarting the engine clears this view. There is no experiment Session,
automatic raw recording, or result history export in this minimal runtime.

Open **Settings → Logs** to view recent application logs from the connected
backend. The page refreshes every five seconds while visible and also has a
manual refresh button. It shows up to 200 lines from the last 64 KiB of the
current backend process's log file.

Vite proxies `/api` and WebSocket traffic to
`http://127.0.0.1:8000` by default. To use a backend on another machine:

```bash
cd frontend
VITE_BACKEND=http://192.168.1.10:8000 npm run dev
```

### Distributed Stimulation and Actuation

To run stimulation on another machine, choose external mode in the Stimulation
current configuration and set `STIMULATION.server_ip` / `STIMULATION.server_port`
to the backend machine's reachable listener address (default port 8765).
Install the pinned Paradigm and Trigger packages on the stimulation node.
Start the engine and use **Prepare external stimulation** in Run, then run
(replace the example address with your backend):

```bash
python scripts/run_stimulation.py --backend http://192.168.1.10:8000
```

For Actuation, choose external mode and set `ACTUATION.server_ip` /
`ACTUATION.server_port` in its current configuration (default port 8001).
Configure the current Effector values in Setup,
start the engine, and use **Prepare external actuation** in Run. Install the pinned
Effector package before running the following command on the device machine:

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
own-module revision, shared system revision, and engine generation before
accepting business events. After the node connects, enable Stimulation or start
Actuation in Run. No Acquisition source or A/P worker is required.
On disconnection the node tears down, fetches configuration again, and starts a
fresh instance only after cleanup succeeds. Stimulation clears its trigger;
Actuation requires the Effector to confirm that physical outputs have stopped.
Hardware/setup, output-settlement, or cleanup errors stop the script for inspection.
Saving a Profile does not change a running node. To change its configuration,
stop/disconnect that external node, prepare the new current values, then restart
and reconnect the node. Shared system changes also require disconnecting external
nodes and restarting the engine.
See [module configuration](docs/module-config.md) for the boundaries and the
distinction between Backend, Engine, and the configuration identifier `system`.
