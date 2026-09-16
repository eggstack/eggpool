# Plan 212: `eggpool-connect` transactional installer and recovery

> **Status:** ready for implementation
>
> **Baseline:** EggPool `main` at `6997cdbb4cee4ec69c71cab15b53b039c7e3309a` (2026-09-16)
>
> **Parent:** Plan 209
>
> **Depends on:** Plans 210–211
>
> **Primary authority:** new `rust/crates/eggpool-connect/`, `rust/crates/eggpool-client-config/`, existing safety patterns in `rust/src/operations/backup.rs`, `rust/src/operations/integrations.rs`, and `rust/src/operations/paths.rs`
>
> **Priority:** P0 — desktop-side safety and usability
>
> **Scope:** add a small native desktop configurator that consumes EggPool connection profiles, fetches the current integration projection, performs minimal client config mutation transactionally, and provides explicit backup/restore/remove/verify operations.

## Objective

The desktop helper should make this workflow safe and routine:

```text
$ <bootstrap command emitted by eggpool configremote codex>

EggPool endpoint: https://pool.example.internal/v1
Client: Codex 0.154.0
Config: /Users/alice/.codex/config.toml
Credential: required, not included in profile

Proposed changes:
  - add/update EggPool model provider
  - install generated EggPool Codex model catalog
  - preserve all unrelated config

Backup: ~/.local/state/eggpool-connect/backups/...
Apply? [y/N]
```

The helper is not an agent harness and is not a second EggPool proxy. It owns only receiving a connection profile and safely configuring a supported local client.

Prefer a separate workspace binary crate:

```text
rust/crates/eggpool-connect/
```

linked to `eggpool-client-config` from Plan 210.

Do not put desktop-only process detection and backup history back into the main EggPool server binary unless code reuse clearly requires a small shared utility.

---

# Workstream 1 — CLI contract

Initial CLI:

```text
eggpool-connect install --profile <epc-token>
eggpool-connect plan --profile <epc-token>
eggpool-connect verify --client codex|opencode
eggpool-connect backups [--client ...]
eggpool-connect restore <backup-id>
eggpool-connect remove --client codex|opencode
eggpool-connect help
```

Recommended common flags:

```text
--yes                  non-interactive approval after all safety checks
--config PATH          explicit client config path override
--api-key-stdin        read credential from stdin without argv/history
--no-verify-network    skip optional pre-write remote reachability check
--json                 machine-readable result where useful
```

Avoid accepting the API key as a normal positional/argv option because process listings/shell history can expose it. If an environment variable already exists, use it. Otherwise prompt securely from the TTY or accept stdin.

`install` should show the plan and require confirmation by default. `plan` is read-only and should be suitable for workgroup troubleshooting.

---

# Workstream 2 — OS and client detection

Support receiving-host detection for:

- Linux;
- macOS;
- Windows.

Do not infer client config paths from the SBC profile. Resolve them locally using the current client contract.

### Codex

Detect:

- executable availability when possible;
- version (`codex --version` or current stable equivalent);
- `CODEX_HOME` override;
- default config location;
- current config parseability.

The helper may configure Codex even when the executable is absent only if explicitly requested with a config path and the adapter can safely render a supported schema. Default interactive behavior should tell the user that native post-write verification cannot run without Codex and require explicit confirmation rather than pretending full verification succeeded.

### OpenCode

Detect:

- executable availability/version;
- `OPENCODE_CONFIG` override;
- current platform-specific global config path;
- schema variant/version using the rules defined in Plan 213;
- config parseability including JSONC.

Do not use Unix-only `$HOME/.config` assumptions on Windows.

### Version handling

A client version is evidence for adapter selection, not a reason to silently rewrite unknown formats. Maintain a tested compatibility range/variant table in code or fixtures. If a new version cannot be safely classified, fail with a message directing the user to update the helper or use `plan`/manual `configsetup` output.

Do not guess “V2” solely from a major version number if OpenCode's real config contract says otherwise; qualify against current source/docs/fixtures.

---

# Workstream 3 — Remote profile fetch and credential handling

Installation sequence before any filesystem mutation:

1. decode and validate `ConnectionProfileV1`;
2. resolve target and advertised EggPool URL;
3. obtain a credential separately from the profile;
4. call the versioned integration-profile endpoint from Plan 211;
5. authenticate and validate the response schema/bounds/revision;
6. select the local client adapter/version;
7. construct the proposed mutation;
8. only then proceed to backup/write.

### HTTP client choice

Do not import EggPool's full provider transport stack simply to fetch one small JSON document. Prefer the narrowest existing reusable HTTP facility that preserves TLS verification and bounded response behavior. If `eggfetch` is adopted elsewhere in EggPool by implementation time and exposes an appropriate general Rust API, evaluate it normally; do not make this plan depend on an unfinished cross-repo migration.

A new HTTP dependency must be justified against binary size and maintenance. Reuse existing Hyper/Rustls components only if doing so does not drag the full server stack into the helper.

### Credential sources

Order should be explicit and safe:

1. existing `EGGPOOL_API_KEY` environment variable;
2. secure TTY prompt;
3. `--api-key-stdin` for automation.

Do not print the key. Do not include it in backup metadata, connection-profile fingerprints, logs, panic/debug dumps, or generated Codex/OpenCode config.

### Persistence is opt-in

The helper may offer an explicit credential-persistence operation/flag, but default installation must not rewrite shell profiles or system/user environment settings.

If implemented in this plan, use a narrow reversible contract such as:

```text
eggpool-connect auth set --client codex
 eggpool-connect auth remove --client codex
```

or `install --persist-auth`.

On POSIX, prefer an EggPool-owned user-only environment file plus a minimal clearly owned integration mechanism rather than arbitrary shell-profile edits. On Windows, use a user-scoped environment mechanism only with explicit consent and capture enough previous-state evidence to restore it. If cross-platform persistence cannot be made cleanly reversible, defer it and provide instructions instead.

---

# Workstream 4 — Transaction state machine

Implement mutation as an explicit state machine. Do not interleave backup/write/verification ad hoc.

Required phases:

```text
Decoded
  -> RemoteProfileValidated
  -> ClientDetected
  -> MutationPlanned
  -> BackupCommitted
  -> ConfigWritten
  -> LocalParseValidated
  -> ClientNativeValidated
  -> Committed
```

On failure after `BackupCommitted`, attempt automatic restoration and report both the original failure and rollback result.

If rollback itself fails, preserve all recovery metadata and return a distinct high-severity error with the exact backup ID/path (but not backup contents/secrets).

Before `BackupCommitted`, no target client file may change.

---

# Workstream 5 — Byte-exact backups

Current EggPool lifecycle manifests are useful ownership evidence, but the desktop helper promises stronger recovery. Save an actual byte-exact snapshot before every mutation.

Recommended state root, resolved portably:

```text
<user-state>/eggpool-connect/
  state/
  backups/
    <backup-id>/
      manifest.json
      config.bin
      generated-artifacts/...
```

The exact platform path follows native/XDG conventions and must be documented.

### Backup manifest

Record only necessary recovery metadata:

- schema version;
- backup ID/time;
- target client;
- normalized config path;
- pre-write SHA-256;
- file existence state;
- file permissions/mode or portable ACL metadata where safely obtainable;
- client version/schema variant;
- EggPool profile **fingerprint**, not credential/token if that token could later become secret-bearing;
- generated artifact paths/hashes owned by this install;
- parent/previous backup ID if useful.

Do not serialize environment credentials or inspect unrelated secret values inside the config.

### Permissions

Backup/state roots must be user-private. On POSIX create directories/files with owner-only permissions before writing sensitive existing config bytes. On Windows use appropriate user-scoped ACL defaults and avoid world-readable temp files.

Do not stage backups in a globally readable temporary directory.

### Retention

Do not silently delete the only usable recovery point. A bounded retention policy can keep the newest N backups per target after at least one known-good committed state exists. Make retention conservative and documented; explicit `backups prune` can be added later if needed.

---

# Workstream 6 — Atomic mutation

For each target:

1. open/read existing config without following unsafe path changes where platform APIs permit;
2. ensure the resolved target is a regular file or absent; reject device/FIFO/socket and suspicious symlink scenarios unless explicitly supported;
3. construct complete proposed bytes in memory;
4. parse/validate the proposed document before write;
5. write to a same-directory temporary file with restrictive permissions;
6. fsync/flush according to existing project portability standards where practical;
7. atomically replace/rename;
8. preserve intended permissions;
9. continue immediately to post-write validation.

A config-path override does not authorize writes to arbitrary profile-supplied paths; it is a local user's explicit input.

Do not use in-place seek/truncate writes.

---

# Workstream 7 — Post-write validation and rollback

Validation layers:

### Layer 1: local format/ownership validation

Use the shared adapter to re-open and parse the installed config/catalog. Verify only EggPool-owned fields plus generated artifacts; unrelated settings are not reinterpreted.

### Layer 2: client-native validation

Codex current qualification should run equivalent checks to:

```text
codex debug models
codex doctor --json
```

using the real effective config environment/path. Validate that the generated catalog parses and EggPool provider resolves as expected without initiating an inference request.

OpenCode should use its current model/config inspection command (at Plan 206 baseline `opencode models`) to verify that EggPool models/providers are visible.

Capture bounded stdout/stderr for diagnostics. Never echo credentials or full unrelated configs. Time-bound child processes.

### Layer 3: optional EggPool connectivity/auth check

The profile fetch already proves basic authenticated HTTP reachability. Optionally verify a cheap read-only endpoint before mutation. Do not send an inference request or consume upstream quota as part of installation.

### Rollback

Any required post-write validation failure restores the byte-exact backup automatically unless the user explicitly used a future expert flag that disables rollback. Do not add such a flag initially.

After restoration, re-parse the restored config and report whether restoration was verified.

---

# Workstream 8 — Explicit restore/remove semantics

## `backups`

List backup IDs, timestamps, target, config path, and hashes. Never print file contents.

## `restore <id>`

Before restoring an older backup:

1. inspect current target state;
2. create a new byte-exact “pre-restore” backup;
3. restore the selected snapshot atomically;
4. validate restored bytes/hash;
5. run client parse validation when available.

This makes restore itself reversible.

## `remove --client ...`

Prefer ownership-aware removal from the shared adapter rather than restoring an arbitrarily old whole-file backup. Remove only EggPool-owned fields/artifacts and restore captured previous values when ownership evidence is valid.

If ownership/drift is ambiguous, refuse and direct the user to explicit backup restore/manual review. `remove` must not delete unrelated providers/settings.

---

# Workstream 9 — Idempotence and sync behavior

Installing the same current profile twice should be a no-op after validation.

If only the remote integration profile revision/model catalog changed, update only generated EggPool artifacts/provider model entries. Do not create noisy rewrites of an otherwise unchanged user config.

If the user edited unrelated config after install, sync should preserve those edits. If the user edited EggPool-owned fields, treat it as drift and show a plan/refuse by default.

Do not use a whole-file post-hash as the only ownership test; byte-exact backup hashes are recovery evidence, while semantic owned-field checks determine whether a narrow update is safe.

---

# Workstream 10 — Error and output contract

Human output should distinguish:

- nothing changed;
- installed and verified;
- installed but client-native verification unavailable (only after explicit consent);
- refused before mutation;
- write failed and rollback succeeded;
- write failed and rollback failed;
- restore succeeded/failed.

Use stable non-zero exit codes for invalid profile, auth/network failure, unsupported client, unsafe existing config, backup failure, mutation failure, validation failure, and rollback failure where the project's CLI conventions make this practical.

Machine-readable JSON must redact secrets and not include captured arbitrary client output unless sanitized/bounded.

---

# Tests

Create deterministic tests using temporary homes/config roots for:

- Linux/macOS/Windows path resolution abstractions;
- absent and existing config;
- read-only/unwritable destinations;
- symlink/special-file refusal where portable;
- backup-before-write ordering;
- byte-exact restore including “file originally absent”;
- permission preservation;
- interrupted/write failure injection;
- local parse failure rollback;
- simulated client-native verification failure rollback;
- rollback failure reporting;
- repeated install no-op;
- remote revision update with unrelated local edits;
- EggPool-owned drift refusal;
- `restore` creating a pre-restore backup;
- malformed/oversize profile and remote profile responses;
- credential absence/redaction.

Use injectable filesystem/process/network seams rather than sleeps or destructive tests against real user homes.

No deterministic test may mutate the contributor's real Codex/OpenCode config.

---

# Acceptance criteria

1. `eggpool-connect` builds as a small standalone desktop binary from the workspace.
2. It depends on `eggpool-client-config` rather than duplicating target renderers.
3. `plan` performs no target filesystem mutation.
4. `install` obtains credentials separately from the connection token and never accepts a default secret-bearing token path.
5. No config write occurs before a durable byte-exact backup is committed.
6. Config replacement is atomic and preserves intended file permissions/ownership semantics.
7. Required post-write validation failure automatically restores the prior state.
8. Rollback success and rollback failure are distinguishable and leave recovery evidence intact.
9. `restore` is itself reversible by taking a pre-restore backup.
10. `remove` is ownership-aware and never deletes unrelated client config.
11. Repeated install/sync is idempotent and does not rewrite unchanged files.
12. Unsupported/unknown client schemas fail closed rather than being guessed.
13. No TTY/stdout/stderr/log/manifest contains the resolved EggPool key.
14. Default setup does not modify shell profiles or persistent user environment variables.
15. The helper performs no inference calls and consumes no provider quota during setup/verification.
16. The helper contains no proxy server, agent loop, background daemon, tool execution, or conversation-state functionality.

## Handoff note

Treat backup and rollback ordering as correctness, not UX polish. The implementation is not complete when it can write a valid config; it is complete when every failure point has a deterministic pre-write refusal or verified restoration path.
