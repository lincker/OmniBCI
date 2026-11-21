# Develop an OmniBCI plugin

This guide describes the implemented, single **Plugin API v1**. Install the selected OmniBCI host release in your Python 3.14 environment. It supplies `omnibci.sdk` and `omnibci-plugin`; no separate SDK runtime or copied SDK package is needed. The executable [Decoder starter](../examples/decoder_plugin/) is used by the automated tests and the app's offline guide.

## Choose a Contribution

A Package is an immutable release archive. Each Contribution in its manifest declares one capability. The owning module constructs an Instance for each binding; installing or enabling a package does not execute it.

| Kind | Owning module | Required role behavior |
| --- | --- | --- |
| `acquisition` | Acquisition | Fixed trigger/resume modes; async connect, observe, start/read/stop and disconnect; checkpoint operations when declared. |
| `decoder` | Processing | Setup, begin/reset each epoch, readiness check, decode, teardown. |
| `filter` | Processing | Preserve the window layout; accept explicit `pre_trigger_data`. |
| `epoch_detector` | Processing | Return an ordered batch of sample boundaries. |
| `paradigm` | Stimulation | Setup with drawing/trigger capabilities, present trial/result, render frame, report busy state, teardown. |
| `trigger` | Stimulation | Setup, send, clear, teardown on the host's owner thread. |
| `effector` | Actuation | Setup, execute, quiesce and teardown through one serial owner. |

Use the [API reference](plugin-api-v1.md) for signatures and value invariants. A package's `domain` is classification, not execution ownership; mixed-kind packages are allowed.

## Write the files and imports

Keep `manifest.toml` at the selected project root. Reference your Python module, parameter schema and packaged resources from that manifest. No registry decorator, build hook, `setup.py` or plugin wheel is needed.

Import only the supported contracts from `omnibci.sdk`. Use keyword-only constructor inputs:

```python
# Acquisition, Paradigm, TriggerEmitter or Effector
__init__(self, *, parameters, resource_root)

# Decoder, Filter or EpochDetector
__init__(self, *, parameters, resource_root, input_signal)
```

`parameters` is the selected Contribution's validated copy. `resource_root` is the fixed snapshot being executed; locate packaged assets beneath it instead of using the working directory. Processing receives only its mapped `SignalInfo`. Host IDs, revisions, services, original Acquisition metadata and transport envelopes stay private. Do not import backend implementation modules or vendor `omnibci`.

Construct local state, acquire role resources during the corresponding setup/connect call, and release partial initialization during teardown/disconnect. Decoder `begin_epoch()` resets working state without reloading its model. A Filter and Decoder borrow arrays read-only. Effector `quiesce()` must finish or cancel prior output; a timeout or process exit alone cannot prove that a physical device has stopped.

Declare `api_version = "1"`, `manifest_version = 2`, an exact release version, parameter schemas, and the kind-specific declarations in the [manifest guide](plugin-manifest.md). Empty parameters still use an empty closed object schema. Paradigm trial/result and Effector command schemas are separate from constructor parameters. Only Decoders declare channel requirements.

Paradigms implement `render_frame(frame: FrameTiming) -> bool`. Import `FrameTiming` from `omnibci.sdk`; its `refresh_index`, `target_time` and `refresh_period` describe a target display opportunity, not a successful presentation. Select content from this timeline, not callback counts. Return `True` after drawing ready content or `False` to omit this submission; the host also discards expired output. Keep decoding/loading outside the draw callback. The host owns the display clock and submission, and plugins own content policy. See the [timing contract and platform limits](stimulation-interface-design.md#9-display-timing-follow-up).

## Validate and package

From the repository root, with the host environment active:

```sh
omnibci-plugin validate examples/decoder_plugin
omnibci-plugin package examples/decoder_plugin --output dist/oz-polarity-demo.zip
```

You can also run `python -m omnibci.plugin_cli` with the same arguments. When receiving a source repository, select its actual plugin directory; an enclosing repository ZIP is not automatically unwrapped.

Both commands use the same static installer rules and report the exact package identity, Contributions, file inventory and archive SHA-256. They execute no plugin code, build hook or dependency installer. `validate` discards its temporary artifact. `package` publishes the requested archive only after validation succeeds.

Archives have deterministic file order, timestamps and modes. VCS, virtual environments, caches, bytecode and `.omnibci-dev` are excluded. Required runtime resources, including resource ZIPs, are retained. Put output archives outside the selected project root so previous outputs cannot be packaged recursively. Symlinks, unsafe/nonportable paths and bundled `omnibci` namespaces are rejected.

## Develop a fixed snapshot

Create a JSON file mapping module names to their existing **current configuration** objects: `expected_revision`, `settings`, `bindings`. Use `expected_revision: null`; every run begins with isolated configuration. Bindings contain exact selections and parameters. Decoder bindings also require `processing_input`.

This complete example uses the tested Decoder starter with built-in File Reader and epoch detection. Replace the recording path with an existing absolute CNT path whose mapped EEG channels include `Oz`:

```json
{
  "acquisition": {
    "expected_revision": null,
    "settings": {},
    "bindings": [{
      "slot": "acquisition.source",
      "selection": {"package_id": "OmniBCI.file_reader", "contribution_id": "default", "version": "0.1.0"},
      "parameters": {"data_file_path": "/absolute/path/recording.cnt", "file_reader_chunk_size": 1000}
    }]
  },
  "processing": {
    "expected_revision": null,
    "settings": {"ALGORITHM.baseline_duration": 0.0},
    "bindings": [
      {
        "slot": "processing.decoder",
        "selection": {"package_id": "example.oz-polarity-demo", "contribution_id": "oz-polarity", "version": "0.1.0"},
        "parameters": {},
        "processing_input": {"schema_version": 2, "outputs": []}
      },
      {
        "slot": "processing.epoch_detector",
        "selection": {"package_id": "omnibci.default_epoch_detector", "contribution_id": "default", "version": "0.1.0"},
        "parameters": {}
      }
    ]
  }
}
```

An empty output list keeps all source signal channels. Use an explicit verified mapping when the model needs selected, renamed or reordered channels; see the [Decoder guide](decoder-plugin-development.md#manifest-and-channel-requirements). The file path is a plugin parameter, not a host-supplied source directory. Relative recording paths resolve against the isolated configuration directory; use absolute paths for external recordings.

```sh
omnibci-plugin develop examples/decoder_plugin --contribution oz-polarity --configuration /absolute/path/development.json --command-timeout 60
```

`--command-timeout` sets the existing host command timeout for this isolated run only. The example allows 60 seconds for cold File Reader/model initialization; without this option, the normal host default is 10 seconds. Use a finite positive value suited to the intended device/model. This does not retry failed actions or change cleanup confirmation.

The command prints the fixed snapshot path/digest and starts the normal API at `http://127.0.0.1:8002`; `--port` selects another local API port. The requested Contribution must be selected in its owning module. Processing requires an explicit Acquisition companion configuration so the model receives prepared input. Output modules must use managed local hosts. Their existing connection settings remain in their module configuration.

The command connects/starts exactly the configured modules through normal application actions. It does not begin reading, dispatch a stimulation trial or replay a device command. Use normal API/console controls when ready. Point the console at this development API. Startup, validation and resource failures remain real host failures.

Use repeated `--companion /path/release.zip` arguments for non-builtin companions. The development catalog resolves exact identities only from these installations, the selected source snapshot and built-ins. It never borrows the normal mutable inventory. No marketplace account is needed.

## Edit, stop, restart

Press Ctrl+C and wait for the host to finish cleanup. Then edit source and run the same command again. A running Instance continues reading its original fixed resources. A new run gets new processes, configuration, inventory and digest, even when the project's development version is unchanged. There is no hot reload or automatic hardware replay.

Each project uses `.omnibci-dev` for its restart guard and temporary runs. Successful confirmed shutdown removes the run artifacts. An interrupted launch, abnormal exit or unresolved cleanup retains them and blocks another run. Stop the retained hosts and device operations before explicitly acknowledging recovery by removing the marker named in the error. The tool does not infer resource release from a dead PID or retry cleanup automatically. Keep the retained files while investigating.

Development is not an OS security sandbox. Plugin code runs with the host process's permissions and normal device admission. The fixed snapshot path supports source debugging without symlinking execution back to mutable workspace files.

## Share or install a release

Package the reviewed source and increment its release version whenever distributed bytes change. Install a received ZIP through **Plugins → Install ZIP…**, enable its exact version, select the Contribution and use its module's Connect/Start action. Saved Profiles are optional reusable parameter copies; they do not activate a plugin.

Market downloads and local ZIPs converge on the same installer and immutable release rules. Already installed eligible packages start offline; only marketplace acquisition needs repository access. A development snapshot does not overwrite an ordinary installed release of the same identity/version.

## Verify the role you implemented

Exercise construction from the packaged artifact, invalid parameters and inputs, required method results, partial setup failure, cancellation and confirmed cleanup. For Processing, test mapped/reordered channels, short windows, repeated epochs, absent pre-trigger data and read-only arrays. For outputs, test serialized ownership and failure to settle. Static validation proves package structure; it cannot prove role behavior or physical timing.

The repository tests cover all seven kinds, immutable source edits, companion isolation, interrupted startup, fresh processes and successful/uncertain shutdown. The installed-wheel check covers packaged resources and SDK identity. Display timing, hardware delivery and physical stopping require tests on the intended equipment; see [validation](validation.md).
