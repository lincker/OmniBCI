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
| Configuration | `omnibci/conf` | Base TOML settings and active-profile overlays |
| Composition root | `omnibci/bootstrap.py` | Selects concrete implementations from configuration and assembles the object graph |

The Application layer does not depend directly on Infrastructure.
Infrastructure implements ports defined by Domain and Application, while
concrete bindings are centralized in the composition root. The import-linter
contracts in `pyproject.toml` continuously enforce these boundaries.

### Plugins

Built-in plugins live in `omnibci/infrastructure/plugins`. The plugin catalog
reads each plugin's `manifest.toml` without importing its Python code. Concrete
implementations are loaded on demand at the relevant execution boundary.

Configure the user plugin directory with the `OMNIBCI_PLUGIN_DIR` environment
variable or `PLUGINS.user_dir` in `omnibci/conf/settings.toml`.

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

The base configuration is stored in `omnibci/conf/settings.toml`. Values in
the active `omnibci/conf/profiles/<name>.toml` profile override matching base
settings.

For file playback, make sure the following configuration points to an EEG data
file that exists on the local machine:

```toml
[ACQUISITION]
signal_source = "file_reader"
data_file_path = "/absolute/path/to/train.cnt"
```

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
Starting the API process initializes the engine graph but does not start EEG
acquisition. Start NeuroEngine explicitly through the web console or API.

### 5. Start the Web Console

Open another terminal:

```bash
cd frontend
npm run dev
```

Open <http://localhost:5173> and perform these steps:

1. Select a data source and decoder.
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

To run stimulation on another machine, set `STIMULATION.auto_spawn` to `false`,
configure `NEUROENGINE.message_server_ip` and `message_server_port`, and run the
following command on the stimulation machine:

```bash
python scripts/run_stimulation.py
```

To run actuation on another machine, set `ACTUATION.enabled` to `true` and
`ACTUATION.auto_spawn` to `false`. Configure `server_ip`, `server_port`, and
`effector`, then run the following command on the device machine:

```bash
python scripts/run_actuation.py
```

All nodes in a distributed deployment must use compatible configuration, and
the corresponding TCP ports must be allowed through their firewalls.
