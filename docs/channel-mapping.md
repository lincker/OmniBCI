# Acquisition descriptors and Processing channel mapping

Acquisition adapts external data sources into a common signal stream and an
`AcquisitionDescriptor`. Its `descriptor` describes the adapter output; source
`metadata` is opaque display text. Neither is an editable
Plugin Profile parameter.

## Acquisition workflow

Choose a source plugin, fill its address and port or file path, then Connect.
Get Metadata reads the connected source and displays the descriptor alongside
source metadata text on the same page. The response is:

```json
{
  "source_id": "neuroscan_curry8_reader",
  "connection_id": "connection-token",
  "metadata": "Channel count: 3\nSample rate: 1000 Hz\n",
  "descriptor": {
    "source_id": "neuroscan_curry8_reader",
    "connection_id": "connection-token",
    "sample_rate": 1000,
    "channels": [
      {"channel_id": "channel-0", "index": 0, "name": "O1", "signal_type": "unknown"},
      {"channel_id": "channel-1", "index": 1, "name": "O2", "signal_type": "unknown"}
    ],
    "trigger_row_index": 2
  }
}
```

This abbreviated example omits optional provenance and native-event details.
`signal_channel_count` counts signal rows only. `output_row_count` includes the
final normalized event row. Plugins pass only `signal_channel_count` to the
descriptor builder; the descriptor derives `output_row_count` by adding one
for the event row. Curry8 and File Reader derive actual dimensions
from their source instead of treating configured defaults as observed facts.
Curry8's source channel count and labels already include the final event row.
Its adapter preserves that row without appending another one, and excludes it
from the descriptor's signal channels. Separate event packets do not overwrite
the native event row.
Scan45 returns text explaining that device metadata querying is not implemented.
Its separate output descriptor uses configuration and is marked `user-declared`.

`SignalSource.get_metadata() -> str` queries source information for inspection.
Native text is passed through unchanged. Curry8 renders its decoded protocol
fields as readable JSON text; File Reader displays file information captured
before output channel selection. Acquisition-derived fields such as the extra
event row are not inserted into that source text.

`SignalSource.describe_output(...) -> AcquisitionDescriptor` describes the
adapter's normalized output from the same observation, without querying again.
The runtime, application and Processing never parse the display text. Plugins
without a valid descriptor can still show metadata, but cannot prepare channel
mapping.

Each `ChannelDescriptor` has a unique `channel_id`, source row `index`, display
`name`, `signal_type`, and optional `unit` and `name_provenance`. Names may repeat.
Adapters use source-provided string IDs when available, or deterministic
`channel-{index}` IDs within that source. A descriptor fingerprint and connection
identity protect mapping preparation from changed or stale sources.

Missing source information is not guessed. Curry8 currently supplies labels
without channel types, so its descriptor reports `unknown`. A failed descriptor
construction does not prevent inspection of the source text. Cached descriptors
belong to the exact source binding and connection; reconnect, source changes and
metadata refresh invalidate readiness. Saved observations never substitute for
current source metadata.

## Acquisition runtime boundary

`AcquisitionWorker` owns configuration and plugin construction. It pins one
settings snapshot, loads the source, and passes only the required context to
`AcquisitionRuntime`: `source_id`, `generation`, `config_revision`, and
`preparation_required`.

`source_id` identifies the configured source. `generation` identifies the current
engine instance, while `config_revision` identifies its applied configuration.
`preparation_required` determines whether starting a stream requires a valid
Processing preparation receipt. The Worker derives it from the Processing input;
the runtime does not read configuration or interpret decoder bindings. Changes
to Processing input rebuild the runtime with a new preparation requirement.

The runtime owns connection and stream lifecycle, source metadata inspection,
and delivery of `SignalChunk` objects. Stopping still waits for the read task,
and stream draining still sends a barrier after the final chunk. Channel mapping
and signal processing remain downstream.

## Processing mapping

A decoder profile owns an ordered `ProcessingInput` with schema version 2:

```json
{
  "schema_version": 2,
  "outputs": [
    {"source_channel_id": "channel-1", "name": "Right occipital", "signal_type": "eeg"},
    {"source_channel_id": "channel-0", "name": "Left occipital", "signal_type": "eeg"}
  ]
}
```

Bindings identify Acquisition channels by ID, never by label. Explicit output
names and selected source IDs must be unique. Missing IDs block preparation.
An empty `outputs` list selects all source signal rows in their original order;
it does not mean zero channels. Decoder `default_names` are suggestions that the
user can match to unique source labels after a descriptor is available.

A binding may declare `eeg`, `eog`, `ecg`, `emg`, or `misc` for an unknown source
type. Without a declaration, it inherits the source type. A declaration cannot
override a conflicting known source type and never mutates Acquisition metadata.
Decoder requirements validate the resulting types, names and channel count.

Compilation produces ordered `source_indices`, output descriptors, and a derived
`requires_data_mapping` flag. Processing reuses the incoming `SignalChunk` when
all signal channels remain in their physical order, including when only names
or type declarations change. Selecting a subset or changing the order requires
mapping, which copies the selected rows and the original event row. It preserves
sample values and dtype; there is no reference subtraction, bipolar derivation,
position template, interpolation, scaling or other channel arithmetic.
The mapped signal then enters Epoch processing, optional Filters and Decoder.

The flag is computed when constructing a plan, including after IPC
deserialization. It is not configurable, is omitted from serialized plan
dictionaries, and does not change plan fingerprints. This avoids the mapping
copy within Processing; cross-process queue transport retains its existing
behavior.

Saving a profile does not change a running experiment. Review and Apply pins
its mapping, rebuilds when necessary, and prepares Processing against the
connected descriptor. Generation, configuration revision, connection identity,
source fingerprint and plan fingerprint must match before streaming.

## API and compatibility

Source plugins must return a string from `get_metadata()` and implement
`describe_output()` to support Processing mapping. The REST metadata response
keeps its connection envelope, with `metadata` now a string and `descriptor`
separate. Third-party sources returning the previous metadata dictionary must
update this contract; the host does not guess a descriptor by parsing text.

`GET /api/v1/config/signal` exposes `acquisition_descriptor`, source identity,
`processing_input`, `accepted_input`, `active_input`, and preparation readiness.
Preview accepts a draft mapping with the expected source revision and decoder
selection. The server resolves the descriptor; callers cannot supply manual
source metadata. The former setup/template/reference API fields are removed.

Old montage domain code, templates and UI are removed. Historical profile and
activation fields remain opaque storage data solely to preserve existing
revision hashes. Version 1 processing inputs are exposed as review-required,
not executed. The backend and profile editor remain available, but old montage
configuration blocks engine start until the user saves and applies a version 2
mapping. No existing configuration file is silently rewritten.
