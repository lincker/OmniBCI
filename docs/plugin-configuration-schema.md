# Plugin configuration schemas

Plugin parameters use a deliberately small, closed JSON Schema subset. Validation
is implemented by `omnibci.application.plugin_schema`; it does not execute plugin
code, access files, fetch URLs, resolve references, or coerce values.

## Public API

```python
from omnibci.application.plugin_schema import (
    PluginSchemaError,
    validate_parameters,
    validate_schema,
)

validated_schema = validate_schema(schema)
parameters = validate_parameters(validated_schema, supplied_parameters)
```

Both functions return independent copies. Invalid declarations and parameters
raise `PluginSchemaError`, a `ValueError` subclass. Messages identify the field:
`schema.properties.port.default` for a declaration and `$.devices[1].port` for a
parameter. `validate_parameters` also validates the schema, so invalid defaults
cannot be bypassed by supplying an explicit parameter value.

## Supported declarations

Every schema node requires one `type`. The root must be an `object`.

| Type | Supported keywords |
| --- | --- |
| `object` | `properties`, `required`, `additionalProperties` |
| `array` | `items`, `minItems`, `maxItems` |
| `integer`, `number` | `minimum`, `maximum` |
| `string` | `minLength`, `maxLength`, `x-widget` |
| `boolean` | No additional type-specific keywords |
| All types | `title`, `description`, `default`, `enum`, `x-scope`, `x-apply` |

- Every object must explicitly declare `additionalProperties: false`.
- `required` lists unique names declared in that object's `properties`.
- Arrays require one `items` schema; tuple schemas are unsupported.
- Integer values must be integers, excluding booleans and floating-point values.
  Numbers accept integers and finite floating-point values. Strings are not
  converted to numbers, and booleans are not converted to integers.
- Numeric bounds are inclusive. Length and item-count bounds are non-negative
  integers. Minimum bounds cannot exceed maximum bounds.
- Enums must be non-empty and contain unique values. Each enum value and default
  must satisfy the declared type and constraints, including nested schemas.
- Only JSON-compatible data is accepted; non-finite numbers, non-string object
  keys, tuples, and arbitrary Python objects are rejected.
- Unsupported keywords are rejected. This includes `$ref`, `$schema`, `$id`,
  `format`, `pattern`, union types, and schema composition such as `oneOf`.
- Schema nesting is limited to 16 child levels below the root. JSON container
  nesting is limited to 64 levels; cyclic Python objects are rejected as well.

## Defaults

`validate_parameters(..., apply_defaults=True)` fills missing fields with their
explicit defaults. This also satisfies a required field when a valid default
exists. Defaults in nested objects and array items are applied recursively.

An optional object is not created merely because its child fields have defaults.
Declare `default: {}` on that object if it should be created automatically.
Without `apply_defaults`, required fields must be supplied explicitly. Defaults
are still checked when validating the schema itself.

Returned dictionaries and lists never share mutable containers with the input
parameters or schema defaults.

## Host metadata

- `x-scope`: `portable`, `machine`, or `secret`. These classify parameter handling
  for future storage and synchronization policies. The validator does not perform
  synchronization or provide secret storage.
- `x-apply`: `trial`, `module`, or `restart`. These describe the plugin's suggested
  application boundary. The host determines whether it can safely apply a change
  at a trial boundary, rebuild a module, or rebuild a larger runtime graph.
- `x-widget`: `file-path`, supported only on string fields. The desktop client
  offers a native file picker when connected to its own local backend. Remote
  connections keep a text field for a path on the backend machine. This hint
  neither reads files nor changes parameter validation.

Metadata is preserved without changing parameter values. These annotations do
not by themselves activate runtime reconfiguration.

The bundled File Reader starts with an empty recording path so a new installation
can open its configuration interface without test data. An empty path can be
saved, but the acquisition worker rejects it before starting. Existing saved
recording paths are retained; files are validated on the machine running the
backend.

## Acquisition source context

Source schemas retain `sample_rate` and `channel_count` so Profiles can record
expected experiment parameters. `channel_count` counts all adapter output rows,
including the final event row. Declared values must be positive integers and
satisfy plugin-specific bounds.

Curry8 and File Reader start with no expected dimensions in a new configuration.
Their schema fields are optional and have no defaults, allowing connection before
confirming an experiment baseline. Users can explicitly enter expected values or
adopt them from observed metadata. Acquisition can **Review and apply
configuration** directly from these parameters without an upfront save. The final
Review proposes a name for a new Profile, or shows changed parameters with a choice
to save a new Profile (default) or update the original. Confirmation saves the
Profile, checks the final plan, and applies its pinned values. An unchanged Profile
is reused without another write. Legacy inline bindings remain readable.

**Connect** and **Disconnect** control the source independently of metadata
inspection. **Get Metadata** explicitly compares expectations in the applied source
binding with actual `SignalInfo` after connecting; connecting alone does not request
metadata. Missing expectations are reported as unconfirmed; Processing uses the
observed source values. Conflicting provided expectations block processing
preparation and streaming; the user must correct the source or review and apply
the observed values. Adopting them updates the draft; final Review defaults to
saving a new Profile, preserving the original Profile and its expectations.
Metadata reads never overwrite a Profile or apply configuration changes.

Processing initialization and trial updates receive the actual sample rate and
mapped channels from the prepared `SignalInfo`. They do not fall back to expected
Profile values or an engine default. Scan45 remains unchanged: it requires its
configured dimensions to parse data and reports `user-declared` information, so
the comparison is explicitly unverified rather than claiming a device match.

## Example

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["host", "port"],
  "properties": {
    "host": {
      "type": "string",
      "title": "Device host",
      "minLength": 1,
      "default": "127.0.0.1",
      "x-scope": "machine",
      "x-apply": "module"
    },
    "port": {
      "type": "integer",
      "minimum": 1,
      "maximum": 65535,
      "default": 4000,
      "x-scope": "machine",
      "x-apply": "module"
    }
  }
}
```
