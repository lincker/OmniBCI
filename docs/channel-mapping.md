# Acquisition descriptors and Processing channel mapping

Acquisition adapts external data sources into a common signal stream and an
`AcquisitionDescriptor`. Its `descriptor` describes the adapter output; raw
plugin `metadata` remains available for inspection. Neither is an editable
Plugin Profile parameter.

## Acquisition workflow

Choose a source plugin, fill its address and port or file path, then Connect.
Get Metadata reads the connected source and displays the descriptor alongside
raw metadata on the same page. The response is:

```json
{
  "source_id": "neuroscan_curry8_reader",
  "connection_id": "connection-token",
  "metadata": {"channel_count": 2},
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
final normalized event row. Curry8 and File Reader derive actual dimensions
from their source instead of treating configured defaults as observed facts.
Scan45 currently marks its metadata as configuration-derived and unsupported
for actual device metadata discovery.

Each `ChannelDescriptor` has a unique `channel_id`, source row `index`, display
`name`, `signal_type`, and optional `unit` and `name_provenance`. Names may repeat.
Adapters use source-provided string IDs when available, or deterministic
`channel-{index}` IDs within that source. A descriptor fingerprint and connection
identity protect mapping preparation from changed or stale sources.

Missing source information is not guessed. Curry8 currently supplies labels
without channel types, so its descriptor reports `unknown`. A failed descriptor
conversion does not prevent inspection of the raw metadata. Cached descriptors
belong to the exact source binding and connection; reconnect, source changes and
metadata refresh invalidate readiness. Saved observations never substitute for
current source metadata.

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

Compilation produces ordered `source_indices` and output descriptors. Applying
the plan copies those rows and the original event row. It preserves sample
values and dtype; there is no reference subtraction, bipolar derivation,
position template, interpolation, scaling or other channel arithmetic.
The mapped signal then enters Epoch processing, optional Filters and Decoder.

Saving a profile does not change a running experiment. Review and Apply pins
its mapping, rebuilds when necessary, and prepares Processing against the
connected descriptor. Generation, configuration revision, connection identity,
source fingerprint and plan fingerprint must match before streaming.

## API and compatibility

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
