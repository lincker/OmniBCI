# Static plugin repositories

The market client reads an operator-configured HTTPS index. It requires trusted
Ed25519 public keys supplied separately by the operator. There is no default
public repository, account service, synchronization service, or downloaded trust
store. A repository is not usable until its URL and trusted keys are configured.

Listing reads metadata only. Installing an exact package version downloads and
verifies its archive, then calls the same offline installer used for local ZIPs.
Installation does not enable a plugin or change any running module.

## Index format

Serve a UTF-8 JSON document such as `index.json`:

```json
{
  "schema_version": 1,
  "repository_id": "example.plugins",
  "issued_at": "2026-09-23T00:00:00Z",
  "expires_at": "2026-09-24T00:00:00Z",
  "packages": [
    {
      "package_id": "example.device-kit",
      "version": "1.0.0",
      "display_name": "Example Device Kit",
      "description": "Acquisition and trigger capabilities.",
      "download": "packages/device-kit-1.0.0.zip",
      "sha256": "REPLACE_WITH_64_LOWERCASE_HEXADECIMAL_DIGITS",
      "size": 12345,
      "contributions": [
        {"id": "reader", "kind": "acquisition"},
        {"id": "trigger", "kind": "trigger"}
      ],
      "api_version": "2",
      "platforms": ["darwin", "linux", "win32"],
      "host_version": "1.0.0",
      "requires": {"numpy": "2.4.4"}
    }
  ],
  "signature": {
    "algorithm": "ed25519",
    "key_id": "operator-configured-key-id",
    "value": "REPLACE_WITH_BASE64_ED25519_SIGNATURE"
  }
}
```

The placeholders are intentionally invalid; this example is not a configured
repository or a usable signing key.

- `repository_id` is a stable identifier. Configure `expected_repository_id`
  on the client to pin this identity in addition to the URL and public keys.
- Timestamps use UTC `YYYY-MM-DDTHH:MM:SSZ`. An index issued in the future,
  an expired index, or an expiry at or before the issue time is rejected. The
  client checks the host clock and re-fetches the index on every installation.
- Package identity is the case-sensitive `publisher.id` from the archive's v2
  manifest. Versions are exact; the client never interprets a version as latest.
  Package/version pairs must be unique throughout the index.
- `contributions` lists every contribution ID and kind in that archive. IDs are
  unique within the package. Installation checks the complete contribution map,
  package identity, and version before publishing any installed files.
- `download` is a plain relative URL path beneath the index's directory. Absolute
  URLs, parent traversal, encoded paths, queries, fragments, and redirects are
  rejected. Static hosting must serve the index and archives directly from their
  configured URLs. There is no cross-origin download fallback.
- `sha256` and `size` describe the exact ZIP bytes, including ZIP metadata. Both
  are checked during downloading. SHA-256 is checked again by the offline
  installer, so changing a cached file cannot bypass verification.
- `description`, `api_version`, `platforms`, `host_version`, and `requires` are
  optional display/compatibility metadata. The offline installer validates the
  actual archive manifest and host compatibility. `host_version` and `requires`
  use exact versions, matching the offline package contract. Dependencies are
  never installed automatically.
- Unsupported index/package fields, duplicate JSON keys, unknown contribution
  kinds, and non-finite JSON values are rejected.

An empty package list is valid. Removing a package from a newly signed index
prevents a new installation through that index, including from cached archives.
Already installed versions retain their separate enablement and retention
lifecycle.

## Signing and trust

The signature covers the entire index except the top-level `signature` object.
The exact bytes to sign are UTF-8 JSON with recursively sorted object keys, no
ASCII escaping of Unicode, no whitespace separators, and no non-finite values:

```python
payload = json.dumps(
    {key: value for key, value in index.items() if key != "signature"},
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode("utf-8")
```

`canonical_index_bytes(index)` implements this signing format. It is an explicit
format contract, not a claim of compatibility with other JSON canonicalization
standards. Sign those bytes with an Ed25519 private key and encode the 64-byte
signature as standard Base64. The index itself may use readable indentation;
verification reconstructs the canonical bytes.

The client accepts trusted public keys as raw 32-byte keys, Base64-encoded raw
keys, or `Ed25519PublicKey` instances. It selects only from the operator's mapping
using `signature.key_id`. The index cannot introduce or replace trusted keys.
Private keys remain in the publisher's signing workflow and are never shipped
with the application or served by the repository. Rotate trust by updating the
operator-controlled key mapping, then publish indexes using the new key ID.

A verified signature establishes repository approval and byte integrity. It does
not sandbox plugin code or independently verify every claimed publisher identity.
Repositories must contain code the operator trusts to run with the application's
permissions.

## Client API

```python
client = StaticMarketRepository(
    repo_url=operator_repository_url,
    trusted_keys=operator_public_keys,
    installer=offline_installer,
    cache_dir=download_cache_directory,
    expected_repository_id=operator_repository_id,
)

packages = client.list_packages()
installed = client.install("example.device-kit", "1.0.0")
```

`list_packages()` returns immutable `MarketPackage` values with exact
`PluginSelection` values for their contributions. `install()` re-verifies the
current index, selects the requested version, and returns `InstalledPackage` from
the offline installer. It never selects a replacement version or enables code.

Archives are cached as `<sha256>.zip` in the configured download cache directory.
Downloads use temporary files and become visible only after their complete size
and SHA-256 have been verified. Interrupted or failed downloads leave no partial
cache entry. A matching existing cache entry is re-hashed before reuse. Profiles,
installed plugin code, runtime state, and this cache have separate lifetimes.

Default limits are a 1 MiB index, 2,000 package entries, a 256 MiB download, a
10-second socket timeout, and a 120-second transfer deadline. The offline
installer applies its additional ZIP expansion, file count, compression ratio,
manifest, and path constraints. Index responses must be uncompressed at the HTTP
layer. Transport failures are reported; there is no stale-index fallback.

Production requests require HTTPS and normal certificate validation. The explicit
`allow_http=True` exception accepts only literal loopback IP addresses, such as
`127.0.0.1` or `::1`, for controlled local tests. It does not accept `localhost`
names, arbitrary HTTP hosts, or redirects. Tests generate temporary Ed25519 keys
and serve controlled fixtures; they do not contact or provision a public market.

The implementation depends on the maintained `cryptography` library for Ed25519;
it does not implement signature primitives itself.

## Console and operator configuration

The REST routes are `GET /market/catalog` and `POST /market/install` beneath the
application's normal API prefix. Installation accepts only
`{"package_id": "example.device-kit", "version": "1.0.0"}`. Requests cannot choose
an arbitrary repository, replace trust keys, or submit download URLs. Responses
expose verified catalog metadata and installed package state; a newly installed
version remains disabled until a separate package enablement action.

Configure all three environment values together:

| Variable | Value |
| --- | --- |
| `OMNIBCI_MARKET_URL` | Operator-approved HTTPS URL of the signed index |
| `OMNIBCI_MARKET_REPOSITORY_ID` | Exact expected `repository_id` |
| `OMNIBCI_MARKET_TRUSTED_KEYS` | JSON object mapping key IDs to Base64 raw Ed25519 public keys |
| `OMNIBCI_MARKET_CACHE_DIR` | Optional local download-cache directory override |

These names intentionally have no preset endpoint or trust key. Omit the three
required values to disable market access (HTTP 503). Partial or invalid
configuration fails closed and reports field-level errors without echoing key
values. The default cache directory is
`platformdirs.user_cache_dir("OmniBCI", "OmniBCI") / "plugin-market"`. Public keys
belong to local operator-controlled startup configuration; they are never supplied
by a catalog response, profile, synchronized document, or normal install request.
There is no environment switch to allow insecure HTTP. The loopback exception is
available only to explicit test construction of `StaticMarketRepository`.

Bootstrap calls `configured_market_service(store, admission_guard=...)` and stores
the result as `app.state.plugin_market_service`; `None` means disabled. The same
managed store/installer used by local uploads must be passed. Package mutation
uses the same exclusive admission guard as local package lifecycle operations.

`PluginMarketService` downloads outside mutation admission, then enters the guard
for publication. `prepare_install()` retains signed proof; `install_prepared()`
checks its signature, exact metadata and expiry again before the installer hashes
and publishes the archive. A runtime Apply that wins admission prevents publication
without discarding an already verified cache download. REST adapters run repository
work in the thread pool, so HTTPS reads and archive validation do not block the
server's asynchronous event loop. Prepared artifacts are internal values and are
never accepted from API callers.

Tests exercise an actual signed loopback HTTP repository through verified download,
disabled installation, explicit enablement, exact-profile creation and runtime-plan
validation. This demonstrates the local workflow; it does not configure or claim
availability of any production marketplace, payment system or account service.
