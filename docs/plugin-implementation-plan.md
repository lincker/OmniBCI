# Plugin configuration implementation

Each numbered step is verified and committed separately. Steps 1 and 2 were
reviewed individually. Subsequent steps are authorized for automatic commits.
Accounts, cloud synchronization, and montage remain outside this implementation.

| Step | Deliverable | Status |
| --- | --- | --- |
| 1 | Reproducible bundled plugin baseline | Complete: `6a7e48f` |
| 2 | Package/contribution identity and exact selection | Complete: `0f99740` |
| 3 | Complete built-in manifests and capability compatibility | Complete |
| 4 | Minimal SDK and explicit construction inputs | Complete |
| 5 | Plugin-owned schemas, defaults, and validation | Pending |
| 6 | Versioned profiles, ordered bindings, and activation snapshots | Pending |
| 7 | Recoverable legacy configuration migration | Pending |
| 8 | Explicit Acquisition loading and parameters | Pending |
| 9 | Explicit Processing loading and parameters | Pending |
| 10 | Explicit Stimulation and Actuation loading | Pending |
| 11 | One complete configuration snapshot across workers | Pending |
| 12 | Worker readiness and revision acknowledgement | Pending |
| 13 | Schema-driven profile editor | Pending |
| 14 | Ordered stream draining and safe pause boundaries | Pending |
| 15 | Session continuation and file replay checkpoints | Pending |
| 16 | Runtime application plan and status API | Pending |
| 17 | Automatic pause, rebuild, restore, and rollback | Pending |
| 18 | Trial-boundary and module-local configuration updates | Pending |
| 19 | Running-system Save/Apply UI | Pending |
| 20 | Cross-platform and end-to-end validation | Pending |
| 21 | Offline package installation | Pending |
| 22 | Enable, upgrade, rollback, and uninstall lifecycle | Pending |
| 23 | Static online repository and verified download | Pending |
| 24 | Market UI and end-to-end installation flow | Pending |

## Invariants

- Saving a profile does not modify the applied configuration or silently change
  the last explicitly selected startup snapshot.
- Runtime application confirms worker readiness before committing activation.
- An epoch keeps one configuration revision throughout its lifetime.
- Source/format changes and full graph rebuilds create a continuation session;
  ended sessions and already emitted physical outputs are never replayed.
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
