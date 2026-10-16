# Montage and channel mapping implementation plan

Status: the minimum runnable path is implemented. The phases below describe the
design and acceptance boundaries; physical-device and cross-platform release
evidence remains required.

## Using the implemented minimum

1. Select the existing acquisition and processing profiles. In **Setup >
   Acquisition > Source montage**, read the connected source or fill missing
   facts manually. Both the read-only observation and manual declarations are
   saved with the acquisition profile. Save, review, and apply manual changes.
2. In **Setup > Signal Processing > Decoder > Channel input**, configure the selected
   decoder profile's ordered outputs, exclusions, and original, single, average,
   or bipolar software reference. Channel names resolve against the current
   selected Acquisition montage automatically; source facts are read-only here.
   Decoder profiles store no source reference and need no rebind action. The
   source summary identifies the selected or active Acquisition profile and
   whether its information comes from a connection or the saved profile.
3. Average reference uses an explicit pool. Reference-only channels need not be
   algorithm outputs. Rereferencing requires established voltage units and a
   compatible original reference; unknown facts stay unknown.
4. Preview, save the decoder profile, then review and apply it. A source change
   invalidates an earlier preview; refresh source information and preview again.
   Switching decoder profiles restores their independent input configurations.
   Saving alone never changes accepted or running values. A changed input
   requires a rebuild and
   fresh preparation; a missing channel blocks preparation before recording.
5. Start recording. The connected source and processing worker must acknowledge
   preparation before the session is created or the source starts fetching.
   **Run** displays readiness; sessions and their JSON/CSV exports retain the
   resolved mapping definition, including references and template provenance.

The implementation uses the existing Scan45, CURRY, and CNT adapters. Scan45
header discovery and other vendors remain deferred. The layout view is a table
with position availability; a two-dimensional scalp preview remains deferred.
Old activations without `processing_input` keep their previous global setup or
identity behavior and stored revision hashes. New decoder profiles default to
all acquired channels with their original reference. The current session repository is in memory.

OmniBCI will discover the acquisition source, establish the correspondence
between signal rows and electrodes, and prepare the channels required by the
selected algorithm. Channel preparation may include rereferencing. Preparation
must finish before samples enter baseline or epoch buffers.

The first release covers the bundled Scan45, CURRY 8, and CNT sources, manual
metadata where discovery is unavailable, channel selection and ordering, and
fixed rereferencing. Existing template-based plans remain readable; new source
coordinates are entered or read in Acquisition. It preserves the current internal
convention of one final trigger row. A physical source can have a different
event representation; its adapter remains responsible for that conversion.

Real-time scalp maps, raw EEG recording/export, resampling, missing-channel
interpolation, adaptive bad-channel detection, arbitrary expression editors,
Laplacian/REST transforms, and live montage switching are separate work.

## Target behavior

Before a recording starts, the user can inspect the source's actual signal
channels, event representation, electrode bindings, original reference, and the
ordered signals that the algorithm will receive. Reported device information,
user declarations, and template coordinates remain distinguishable.

The preparation sequence is:

```text
Start worker control loops
  -> connect source and read metadata
  -> resolve electrode bindings and algorithm input configuration
  -> validate and compile the signal plan
  -> construct processing with the resolved input context
  -> acknowledge readiness for this connection and configuration
  -> create the recording session and start streaming
```

During streaming, the acquisition adapter supplies the original signal rows and
the normalized trigger row. Signal Processing applies the prepared signal transform
once per chunk before baseline storage or epoch assembly. Events retain their
values and sample positions. The acquisition payload is not modified in place.

The release is complete when different source row orders produce the intended
algorithm input, rereferencing uses the correct reference pool, and reconnects,
configuration changes, and session history cannot silently change row meaning.

## Integration constraints addressed

| Behavior before implementation | Required consequence |
| --- | --- |
| [SignalChunk](../omnibci/domain/signal.py) treats its final row as trigger | Normalize device-specific events in the adapter and keep trigger outside signal arithmetic |
| [Acquisition metadata handling](../omnibci/infrastructure/acquisition/acquisition.py) currently logs the result | Return typed metadata through command results and expose its availability to the application |
| [SignalProcessingWorker](../omnibci/infrastructure/neuroengine/signal_processing/worker.py) constructs plugins before device connection | Defer signal-dependent construction until source and mapping preparation completes |
| [SignalProcessor](../omnibci/infrastructure/neuroengine/signal_processing/signal_processor.py) fixes sample rate and baseline capacity at construction | Construct it from resolved metadata; updating a status field is insufficient |
| [ProcessingPipeline](../omnibci/infrastructure/neuroengine/signal_processing/epoch/pipeline.py) transforms the window but leaves supplied baseline and latest unchanged | Apply this channel transform before all three are accumulated |
| [PluginContext](../omnibci/sdk/__init__.py) documents channel names as acquired channels | Add an explicit description of the actual plugin input instead of silently changing the existing field |
| [ActivationSnapshot](../omnibci/application/plugin_profiles.py) hashes settings and pinned bindings | Include signal configuration in the accepted snapshot and retain old revision semantics |
| [Profile review](../frontend/src/PluginProfiles.tsx) normally excludes saved host changes | Include channel input in the selected decoder binding refresh |
| [Activation storage](../omnibci/infrastructure/repositories/plugin_profile_repository.py) replaces the current activation file | A session must retain its resolved signal description; a revision alone cannot reconstruct history |

## Configuration ownership and data contracts

Acquisition profiles own `acquisition_montage` (the latest read-only source
observation) and `manual_montage` (explicit missing-field declarations). Both use
`SourceMetadata`; persisted observations omit the ephemeral connection ID.
Manual values are copied into the accepted acquisition binding. A generated
`CH1` label is marked as generated, so it cannot be confused with a reported
electrode name. Known device/file facts cannot be edited through the profile API.

Decoder profiles own `processing_input`: ordered outputs, software reference,
and excluded channels. The field is copied into the accepted decoder binding.
Signal Processing reads that pinned input directly and binds it to `SourceMetadata`
from the currently selected Acquisition. Managed and standalone engine nodes
retain the same canonical binding and generate no `SIGNAL.setup` projection for
modern input. `compile_processing_plan` and the legacy compiler share channel
transformation logic. A generated plan is an immutable runtime result, not
another editable configuration source.

`GET /api/v1/config/signal` exposes `source_binding` with the Acquisition profile,
selection state, and metadata origin, plus an opaque `source_revision` freshness
token. `processing_input`, `accepted_input`, and `active_input` describe pinned
configuration; saved profile drafts do not replace those values. The legacy
`setup`, `accepted_setup`, and `active_setup` read views remain available for
compatibility. `POST /api/v1/config/signal/preview` accepts the draft
`processing_input` and optional `expected_source_revision`. Source metadata is
resolved by the server from Acquisition; metadata overrides are rejected. The
revision guard rejects source changes before preview, and a change during
compilation also rejects the stale result.
Conflicting source facts are exposed as `source_error` without hiding the selected
profile. Preview and preparation reject that conflict.

Existing profiles without the field retain legacy `SIGNAL.setup` behavior until
explicit adoption. The retired global editor, global write API, separate signal
review flag, and unused template-list API have been removed. Legacy assignments,
manual metadata, and templates remain supported when reading existing plans.
Import copies only processing choices; source declarations must be reviewed in
Acquisition before adopting the new profile field. No user files are rewritten.

Source plugin parameters continue to own connection and protocol parameters.
Their existing sampling/count declarations remain explicit expectations or
protocol inputs until a separate migration removes their duplication with
discoverable metadata. Do not silently reinterpret Scan45's count, which
includes the event row.

| Contract | Minimum content | Owner |
| --- | --- | --- |
| `SourceMetadata` | Source identity; connection identity; sample rate; ordered signal descriptors; adapter output row count; native event description; normalized trigger index; field provenance | Acquisition observation |
| Signal descriptor | Source row identifier/index, original label, signal type, unit and established scale; missing optional facts remain unknown | Acquisition observation plus explicit missing-field declarations |
| `ProcessingInput` | Schema version; ordered outputs; software reference; exclusions | Decoder profile and pinned binding |
| `SignalSetup` | Legacy source identity, assignments, processing input, layout and source declarations | Read-only legacy configuration and API compatibility |
| `ResolvedSignalPlan` | Source description; modern processing input or legacy setup; resolved acquisition and processing layouts; input dependencies; transform; original/software references; template provenance; configuration revision and semantic fingerprint | Application preparation result |
| Plugin input description | Effective sample rate, ordered channels/types/units/reference and optional positions actually presented to the plugin | Derived processing context |

Keep these as a small set of immutable value objects and nested records. Domain
types must not depend on MNE, HTTP models, or storage. Application coordinates
resolution and lifecycle. Infrastructure implements devices, MNE conversion,
IPC, and persistence. No montage plugin kind, new worker process, or independent
named montage repository is needed for this release.

Persist optional fields by omission, following the existing TOML contract;
reject non-finite numbers. A scalar row name is not sufficient to represent a
bipolar output: retain the positive/negative electrode or derivation identity.

## Phase 1 Establish signal and event contracts

Deliver the value objects, validation rules, fixtures, and compatibility
conversion without changing streaming values or order.

1. Define signal row indices after source event normalization, explicitly
   excluding the final trigger row. Store native event indices separately.
   UI row numbers may be one-based, but serialized indices are zero-based.
2. Define `signal_channel_count`, normalized `output_row_count`, and
   `trigger_row_index`. Validate `output_row_count = signal_channel_count + 1`
   for the current internal format. Native packet dimensions are source-specific.
3. Distinguish device-reported, file-reported, protocol-defined, user-declared,
   and template-derived fields. Unknown position/reference/unit is not a
   fabricated default. Distinguish reference and ground electrodes from
   independently sampled signals.
4. Define the metadata availability/error states and serialization across
   multiprocessing. Preserve compatibility with sources whose existing
   `get_metadata()` does not yet return the new structure.
5. Keep one normalized event sequence in this release. Multiple native trigger
   channels require an explicit adapter decoding rule. Unsupported combinations
   must fail clearly rather than silently choosing or combining channels.

Primary seams: [acquisition port](../omnibci/domain/ports/acquisition.py),
[signal value object](../omnibci/domain/signal.py),
[application events](../omnibci/application/events.py), and SDK.

Acceptance: reject invalid dimensions, duplicate source identifiers, invalid
trigger indices, non-finite rates/scales, and malformed provenance. Round-trip
the objects through serialization and multiprocessing. Existing SignalChunk
and acquisition tests retain identical signal and event arrays.

## Phase 2 Expose minimal metadata for existing sources

Reuse existing metadata and explicit user declarations to make channel mapping
work with the bundled sources. Keep one shared metadata structure behind the
existing `SignalSource` interface and the current single-stream convention.
Additional device families, protocol integrations, capability catalogs, and
multi-stream discovery are deferred until a concrete device requires them.

1. CURRY: adapt the existing device metadata, retaining signal count, labels,
   sampling rate, and the fact that events arrive separately and become a final
   row. Do not assume that labels imply coordinates or a known voltage scale.
2. CNT: extract available names, types, rate, units, locations, and reference
   information from the loaded MNE object. Preserve unknowns and the existing
   annotation-to-event conversion.
3. Scan45: initially describe the established packet/event convention and
   explicit profile declarations. Use manual channel declarations where the
   protocol reader cannot discover them. Automatic header discovery is deferred;
   the current TODO is not a source of trustworthy metadata.
4. Serialize metadata reads with source lifecycle commands. Do not introduce a
   second reader competing with the stream fetch loop on the device socket.
5. Return metadata through the existing command result payload. Extend
   acquisition status with metadata availability, observation provenance, and
   declared-versus-observed discrepancies. Retain existing status fields during
   migration, but identify whether they are declarations or observations.
6. Add a read-only metadata endpoint under the acquisition API. A refresh that
   accesses the device is an explicit command with a bounded timeout. Invalidate
   cached metadata and readiness on disconnect; an old response cannot describe
   a new connection. Cache observations under the owning acquisition profile,
   exact plugin selection, parameters, and manual montage. A different profile
   cannot inherit connected metadata from the same plugin. Persist successful
   observations to the actual applied source's profile. A saved profile changed
   since Apply must not receive observations from the old connection. Matching
   saved observations and pinned manual montage
   remain available offline as profile-origin metadata, without readiness.

Relevant adapters: [Scan45](../omnibci/infrastructure/plugins/acquisition/neuroscan/scan45/scan45.py),
[CURRY](../omnibci/infrastructure/plugins/acquisition/neuroscan/curry8/curry8.py),
and [CNT](../omnibci/infrastructure/plugins/acquisition/file/adapter.py).
Application/API seams: [acquisition service](../omnibci/application/services/acquisition_service.py),
[router](../omnibci/presentation/routers/acquisition.py), and
[response schema](../omnibci/presentation/schemas/acquisition.py).

Acceptance: fixture-backed tests establish counts, row order, event origin, and
actual rates for each adapter. A configured/observed rate or count conflict
blocks start with the offending values; it never overwrites the configuration
silently. Test disconnected, timeout, incomplete, malformed, and stale metadata.
For now the processing engine supports positive integer rates; report an
unsupported fractional rate rather than rounding it.

## Phase 3 Save processing input with the decoder profile

1. Validate and persist `processing_input` through the existing profile API and
   revision-checked TOML repository. Source declarations remain in Acquisition.
2. Pin input choices in the accepted decoder binding. Omitted fields in existing
   documents stay omitted, preserving old activation hashes and legacy behavior.
   New decoder profiles explicitly select all channels and original reference.
3. Automatically bind to the current selected Acquisition without storing a
   source reference in decoder profiles. Resolve channel names against the
   applied connected source at preparation. Source reordering can change source
   indices without changing algorithm output order.
   Missing channels, invalid references, or conflicting source facts block start.
4. Preview decoder drafts without mutation using Acquisition-owned metadata and
   the source revision guard. Source coordinates retain their observed/manual
   provenance; legacy templates remain supported by the compiler.
5. Consume the pinned input consistently in managed and standalone engine nodes,
   without a derived global setup. Review shows the derived channel plan. Input
   changes require a rebuild; unrelated decoder parameters retain their supported
   trial-boundary behavior.
6. Preserve old global configurations as read-only compatibility data. Explicit
   adoption saves processing choices in a decoder profile; manual source fields
   must be completed in Acquisition. Remove replaced writers after validation.

Primary seams: [profile service](../omnibci/application/services/plugin_profile_service.py),
[signal preparation](../omnibci/application/services/signal_setup_service.py),
[node projection](../omnibci/application/node_configuration.py), and
[runtime planning](../omnibci/application/services/runtime_configuration_service.py).

Acceptance: decoder A/B inputs survive switching and repository reload; saved
edits do not mutate accepted bindings; source switching resolves the same input
names against the new source; missing channels block recording; legacy hashes
remain unchanged. Profile persistence, preview, runtime classification, and real
CNT integration cover these boundaries.

## Phase 4 Compile and verify channel transformations

Deliver a pure compiler and executor, tested independently of devices and UI.

The modern compiler accepts Acquisition's source metadata and the pinned
`ProcessingInput` directly. Legacy row assignments remain supported by
`compile_signal_plan`. Ordered outputs are configured for the selected
decoder. Changing decoder triggers revalidation. The bundled CCA/FBCCA receive
the configured ordered subset; do not invent a mandatory anatomical channel set
for them. Their final input validation occurs during prepared construction.

Implement small, explicit operations:

- Preserve the original reference, optionally selecting/reordering channels.
- Subtract one explicitly sampled reference channel.
- Subtract the mean of an explicit reference set, including CAR or two mastoids.
- Produce explicitly named bipolar pairs.

Resolve every output and reference dependency before discarding unused inputs.
Reject duplicate output names, missing dependencies, an empty reference pool,
trigger participation, and unsupported mixtures of units or original reference
schemes. CAR uses the selected valid EEG reference pool and excludes designated
bad/auxiliary channels. Its effective pool is frozen for the run; changes require
repreparation. Require an explicit compatible original-reference declaration
when the device cannot establish it. Ground is not a substitute signal.

Where scaling is verified, normalize contributing signals to a common unit
before subtraction. Unknown units can remain in a legacy identity path but
cannot justify mixing signals whose scales are unestablished. This phase does
not introduce undocumented device gain conversions.

Fixed selection, ordering, scaling, and linear rereferencing can be compiled to
indices/coefficients once. Runtime executes an array operation without repeated
name lookup or constructing MNE Raw objects. Keep a direct selection/identity
path; a general expression language is unnecessary.

Example: source signals `O1, Oz, O2, M1, M2` share an established acquisition
reference and unit. The algorithm requests `O1, Oz, O2` referenced to the average
of `M1, M2`. Both mastoids remain input dependencies although neither is output.
For a sample `[10, 20, 30, 2, 4]`, the expected output is `[7, 17, 27]`.

Acceptance: exact identity/reorder tests; analytical single-reference, average,
CAR, and bipolar examples; reference inputs outside the output subset; exclusions;
mixed-unit rejection/conversion; unchanged sample count; no source-array mutation.
Output metadata must describe the resulting signals, including derivations.
Do not assign a bipolar derivation a fabricated physical electrode coordinate.

## Phase 5 Prepare processing before streaming

Deliver the complete backend path. This is the main lifecycle change and should
be split into a preparation-handshake commit and an execution/session commit.

1. Separate process readiness from signal readiness. Start a processing control
   loop that can stop and receive commands before plugins requiring signal
   context are constructed. Static package/schema validation remains available
   during bootstrap.
2. Use the existing command/result transport for a preparation command carrying
   the resolved plan. Construct decoder, detector, filters, processor, and
   baseline buffer only after validating the plan. Use the observed sample rate
   or an explicitly declared fallback. Known conflicting expectations remain
   errors until the user corrects the configuration.
3. Add explicit acquired-signal and plugin-input descriptions to the SDK.
   Acquisition retains original semantics. Decoder and downstream filters receive
   the transformed input description; event detection receives the description
   of the chunk it actually inspects. Keep legacy context field semantics stable.
   Add an optional manifest capability such as `signal_input_contract = 1` for
   every plugin that consumes transformed signals. Undeclared API 1/2 plugins
   retain the compatible identity path; reject unsupported transformed input
   before streaming. Update bundled plugins and catalog validation together.
   A declaration requires context/shape tests, not just a manifest flag. It does
   not promise compatibility with every electrode set or reference. No new plugin
   API version or kind is needed solely for this additive context contract.
4. A successful preparation receipt identifies the worker generation,
   configuration revision, connection identity, source semantic fingerprint, and
   resolved plan fingerprint. Repeated identical requests are idempotent.
   Timeouts and late acknowledgements never grant readiness to another connection.
5. Gate direct stream start, recording start, session start where applicable,
   and continuation through this same preparation check. The worker also checks
   the receipt before starting fetch. Complete preparation before creating a
   recording session so preparation failure leaves no phantom recording. Use
   the existing lifecycle admission/serialization discipline across the final
   preparation check, session creation, and start. A disconnect or start failure
   in that interval invalidates the receipt and invokes recording rollback.
6. Apply the transform exactly once to every signal chunk at processing ingress,
   before any pretrigger/state-machine/epoch consumer. Preserve timestamp,
   sequence, sample count, and trigger samples. Stream barriers bypass numerical
   transformation. Validate incoming dimensions against the bound descriptor.
7. Freeze the resolved source description, output description, layout provenance,
   original/software references, and transform definition into session metadata.
   Retain the configuration revision and plan fingerprint. Store the definition,
   not only a hash or a reference to the replaceable activation file. Use the
   existing session repository/export path; this does not add raw EEG storage.
   The default in-memory repository retains this history only for its lifetime;
   durable recording persistence remains a separate capability.
8. Layout, order, units, reference pool, or transform changes require a stopped
   stream and rebuild. Existing decoder trial-boundary parameter updates may
   remain when their input contract is unchanged; construct replacements with
   the already resolved processing context, not raw `channel_names`. Advance
   receipt/context revisions through the existing coordinated update when the
   signal plan is unchanged; a stale receipt cannot authorize a later stream
   restart merely because a trial update was compatible.
9. When Apply restores a connected or streaming source, it finishes only after
   the new source/processing preparation and existing required peer
   acknowledgements succeed. If the engine was running with its source
   disconnected, preserve that disconnected state: Apply can finish installing
   configuration with signal readiness explicitly pending. Do not connect
   automatically or wait indefinitely for hardware. Offline selection likewise
   remains unprepared. A later Start performs preparation and can report a
   constructor/device error. Expose configuration application and signal
   readiness separately so process startup is never presented as signal readiness.

Runtime continuity rules:

- Disconnect invalidates readiness. Reconnect obtains current metadata and a
  receipt for the new connection, even when channel content is unchanged.
- A source switch invalidates connected metadata ownership and preview freshness.
  Matching offline profile metadata remains inspectable but cannot authorize a
  stream or reuse a receipt from the previous source.
- Unexpected rate/order/type/unit changes invalidate the old binding. Reject
  automatic resume under the old context. Structural changes during streaming
  stop processing before a mismatching chunk is decoded. Unreported physical
  changes cannot be detected from a manual declaration alone.
- Explicitly accepted mapping changes can reuse a source checkpoint when the
  source resource/content still satisfies its existing restore contract. Drain
  the old stream, rebuild, prepare the new plan, and create a continuation with
  new provenance. A mapping change does not itself mean the file cursor is invalid.
- Preserve existing rollback, unknown-outcome, and recovery-required behavior.
  No old queued chunk, buffer, or preparation receipt crosses into a new plan.

Primary seams: [workers and builder](../omnibci/infrastructure/neuroengine/builder.py),
[signal processor](../omnibci/infrastructure/neuroengine/signal_processing/signal_processor.py),
[recording service](../omnibci/application/services/recording_service.py),
[runtime apply](../omnibci/application/services/runtime_apply_service.py),
[pipeline replacement](../omnibci/infrastructure/neuroengine/signal_processing/pipeline_configuration.py),
[checkpoint policy](../omnibci/application/source_checkpoint.py), and
[session records](../omnibci/domain/repositories.py).

Acceptance: no sample is consumed before preparation acknowledgement; failed
preparation creates no recording; duplicate/late commands are handled safely;
500 Hz and a 0.2 second baseline produce 100 baseline samples. Known numerical
transforms must reach baseline, window, and latest consistently. Test mid-chunk
epoch boundaries, standalone streaming, drains, unprepared shutdown, reconnect,
intentional continuation, rollback, and trial-boundary decoder replacement.

## Phase 6 Add the setup workflow

Deliver the user-facing workflow after backend preparation is available.

1. Acquisition shows connection/metadata status, observed and declared rate/count,
   native event origin, and the normalized trigger row. Add an explicit inspect
   or refresh action that respects source lifecycle restrictions. Save missing
   names, types, units, reference and optional coordinates in this profile.
2. Acquisition shows source row, label, type, unit, field provenance, and optional
   position. There is no second writable source-description editor in Signal Processing.
3. Each decoder profile shows ordered algorithm outputs, reference mode/pool,
   and excluded channels. Source information identifies the current Acquisition
   binding and its connected, profile, or unavailable metadata origin. It is
   read-only and follows Acquisition selection automatically. Its existing profile
   revision and Review/Apply workflow also govern channel input changes.
4. Preview displays the resolved source-to-output mapping and actionable errors.
   It uses the current source revision and clears obsolete results after a source
   change; the browser cannot supply replacement source metadata.
   Offline editing is allowed, but it is not evidence that the connected source
   has been validated. Start remains a backend-gated action.
5. Review includes the selected decoder input, shows semantic changes
   such as output order/reference pool, and explains rebuild/continuation effects.
   Saving alone never changes the active plan. Run displays signal readiness and
   the active plan summary, not an unapplied draft.
6. Regenerate OpenAPI and frontend REST types. Reuse the existing web UI for the
   desktop app; no separate desktop configuration logic is needed.

Primary seams: [PluginProfiles](../frontend/src/PluginProfiles.tsx),
[RuntimeConfiguration](../frontend/src/RuntimeConfiguration.tsx),
[RunConsole](../frontend/src/RunConsole.tsx), and
[REST types](../frontend/src/api/rest-types.ts).

Acceptance: complete inspect, map, preview, save, review, apply, and start against
a fixture source. Verify stale edits, conflicting metadata, unmapped channels,
offline editing, absent coordinates, reference-only dependencies, active/draft
distinction, and failure recovery. A missing coordinate must not disable simple
selection or rereferencing that requires no geometry.

## Phase 7 Validate compatibility and release

Deliver migration coverage, cross-process integration tests, hardware evidence,
and updated user/developer documentation.

| Scenario | Required result |
| --- | --- |
| Old settings, profiles, and activation | Read unchanged; identity signals/events preserved; original revisions still validate |
| Source order changes | Old mapping is rejected or explicitly re-resolved and accepted; electrode meaning never follows a stale index silently |
| Reference outside output subset | Numerical output uses all intended reference inputs |
| Trigger/event normalization | Exact values and sample positions survive mapping, including chunk boundaries |
| Baseline, window, latest | All three use the same prepared channel semantics |
| Save while running | Running plan and session provenance remain unchanged |
| Same number of channels but new order/reference | Semantic change requires rebuild |
| Reconnect or late acknowledgement | Old readiness cannot authorize a new connection |
| Explicit mapping change on the same file | Valid cursor can resume in a new prepared continuation |
| Device/file content changes unexpectedly | Existing source restore and preparation checks reject unsafe resume |
| Historical session after another Apply | Its original and processed channel descriptions remain reconstructable |
| Legacy processing plugin | Identity compatibility remains; unsupported transformed input is rejected before streaming |

Extend existing acquisition, source checkpoint, processing pipeline, runtime
configuration, worker readiness, SDK, API, and frontend profile tests. Add
analytical transform fixtures and real-process preparation tests. Run relevant
tests for each phase; the final integration phase runs the required suites from
[validation.md](validation.md), including architecture and generated API checks.

Run subprocess coverage on macOS, Windows, and Linux through the existing CI
matrix. Validate template availability in an installed wheel and offline runtime.
Measure transform latency on representative configured channel counts and block
sizes against the sample-block duration; choose an acceptance budget from the
actual deployment rather than inventing a universal threshold.

Hardware checks must establish Scan45/CURRY counts, event alignment, scaling,
labels, and reconnect behavior for the supported protocol versions. Simulated
packets establish parser behavior but do not establish physical wiring or timing.

## Delivery order and boundaries

| Delivery | Depends on | Reviewable outcome |
| --- | --- | --- |
| Phase 1 | None | Shared vocabulary, typed contracts, unchanged legacy arrays |
| Phase 2 | Phase 1 | Real source inspection and visible discrepancies |
| Phase 3 | Phases 1 and 2 | Saved setup and deterministic layout preview |
| Phase 4 | Phases 1 and 3 | Numerically verified mapping/reference plan |
| Phase 5 | Phases 2 through 4 | Prepared backend streaming and session provenance |
| Phase 6 | Phase 5 for end-to-end use | Complete web/desktop setup workflow |
| Phase 7 | Phases 1 through 6 | Compatibility and release evidence |

Phase 2 source adapters can be implemented independently once Phase 1 is stable.
UI table work can proceed against the Phase 3 contracts while Phase 5 is built,
but it must not expose unimplemented Apply/start semantics. Each delivery can be
reviewed separately; Phase 5 should not be merged as a collection of disconnected
readiness checks.

The minimum usable mapping milestone is implemented across Phases 1 through 6.
Phase 7 still requires release evidence from the supported operating systems and
physical devices. Protocol and hardware details include the actual supported
versions, labels/event encoding, and verified physical units; fixtures alone do
not establish those facts.

## Reference basis

The separation of electrodes, sampled channels, and recording reference follows
the [BIDS EEG specification](https://bids-specification.readthedocs.io/en/stable/modality-specific-files/electroencephalography.html).
This plan does not claim BIDS export compliance.

Template loading can reuse the project's pinned MNE dependency through its
[montage API](https://mne.tools/1.12/generated/mne.channels.make_standard_montage.html).
Template names and coordinates are versioned inputs: the
[MNE 1.13 changes](https://mne.tools/dev/changes/v1.13.html) renamed the older
`standard_1020` and `standard_1005` templates. Do not substitute the current online
catalog for the installed version or re-resolve historical positions silently.

The distinction between a reference pool and selected output channels is also
explicit in [BCI2000 SpatialFilter](https://bci2000.org/mediawiki/index.php/User_Reference%3ASpatialFilter).
[FieldTrip rereferencing](https://www.fieldtriptoolbox.org/example/preproc/rereference/)
describes fixed linear channel derivations, while
[MNE reference handling](https://mne.tools/1.12/auto_tutorials/preprocessing/55_setting_eeg_reference.html)
is separate from assigning electrode positions.
