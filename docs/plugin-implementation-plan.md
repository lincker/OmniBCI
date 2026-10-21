# Plugin configuration implementation

The table records the original implementation checkpoints. The current minimal
runtime retains plugin configuration and source checkpoints while removing
Session lifecycle and storage.
Accounts, cloud synchronization, and montage remain outside this implementation.

| Step | Deliverable | Status |
| --- | --- | --- |
| 1 | Reproducible bundled plugin baseline | Complete: `6a7e48f` |
| 2 | Package/contribution identity and exact selection | Complete: `0f99740` |
| 3 | Complete built-in manifests and capability compatibility | Complete: `70d25ef` |
| 4 | Minimal SDK and explicit construction inputs | Complete: `6a5bdaa` |
| 5 | Plugin-owned schemas, defaults, and validation | Complete: `ebbc5a8` |
| 6 | Versioned profiles, ordered bindings, and activation snapshots | Complete: `da4406a` |
| 7 | Recoverable legacy configuration migration | Complete: `280672f` |
| 8 | Explicit Acquisition loading and parameters | Complete: `0b4bd5b` |
| 9 | Explicit Signal Processing loading and parameters | Complete: `d92615b` |
| 10 | Explicit Stimulation and Actuation loading | Complete: `61635d8` |
| 11 | One complete configuration snapshot across workers | Complete: `99309e6` |
| 12 | Worker readiness and revision acknowledgement | Complete: `7ad4e8d` |
| 13 | Schema-driven profile editor | Complete: `1812183` |
| 14 | Ordered stream draining and safe pause boundaries | Complete: `90fc63b` |
| 15 | File replay checkpoints | Retained from `c88e405`; Session continuation removed |
| 16 | Runtime application plan and status API | Complete: `0d388f1` |
| 17 | Automatic pause, rebuild, restore, and rollback | Complete: `9edb5e2` |
| 18 | Trial-boundary and module-local configuration updates | Complete: `a453e8d` |
| 19 | Running-system Save/Apply UI | Complete: `0ef0852` |
| 20 | Cross-platform and end-to-end validation | Complete: `6af23be` |
| 21 | Offline package installation | Complete: `e72bccb` |
| 22 | Enable, upgrade, rollback, and uninstall lifecycle | Complete: `c11b05b` |
| 23 | Static online repository and verified download | Complete: `9d29f29` |
| 24 | Market UI and end-to-end installation flow | Complete: `ee92163` |

## Invariants

- Saving a profile does not modify the applied configuration or silently change
  the last explicitly selected startup snapshot.
- Runtime application confirms worker readiness before committing activation.
- An epoch keeps one configuration revision throughout its lifetime.
- Source/format changes and full graph rebuilds create a new engine generation.
  Decoder/filter updates retain the generation and result numbering. Checkpoint
  restoration must not repeat already delivered samples or physical outputs.
- Plugin code, user profiles, state, and download cache have separate lifetimes.
- Installation does not enable or run a plugin. Package versions are immutable.
- Portable/machine/secret field metadata and storage ports provide the minimal
  future synchronization boundary, without accounts or a synchronization service.

## Validation limits and follow-ups

- Physical EEG devices, parallel-port output, and real display timing require
  hardware validation outside the headless suite.
- Windows and Linux behavior is covered by CI configuration and portable tests;
  local execution currently uses macOS.
- A production market repository and trusted signing keys must be configured by
  the operator; tests use controlled local repositories and package fixtures.
