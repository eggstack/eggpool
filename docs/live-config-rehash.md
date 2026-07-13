# Live Configuration Rehash

## Overview

`eggpool rehash` applies supported configuration changes to a running
EggPool server without a restart. The command validates the config
locally, contacts the running server's control socket, and the server
atomically swaps the active configuration generation when safe.

## Quick start

```bash
# Edit config, then apply live changes
$EDITOR ~/.config/eggpool/config.toml
eggpool rehash

# Disruptive changes (host, port, database path) require a restart
eggpool restart
```

## Complete reload flow

The reload follows a strict transactional pipeline:

1. **Local validation** — CLI validates the config file via
   `validate_config_file()`. Invalid configs are rejected immediately
   (fail-closed); the running config is never touched.
2. **Control socket** — CLI connects to the Unix-domain socket and
   sends the validated `content_digest` (SHA-256 of the config bytes).
3. **Server-side re-validation** — The server independently re-validates
   the config to guard against TOCTOU drift.
4. **Diff** — Server computes a `ConfigDiff` against the active
   generation's config.
5. **Restart-required check** — Any field with `RESTART_REQUIRED`
   disposition causes the entire operation to be rejected. The server
   lists the offending fields in the response.
6. **Candidate generation** — A new `RuntimeGeneration` is built
   (router, DB connections, app state) without touching the active one.
7. **Persistence reconciliation** — Database state is reconciled in a
   transaction; failures trigger rollback.
8. **Atomic publication** — The new generation is installed via
   `RuntimeManager`; new requests immediately use it.
9. **Old generation retirement** — Active streams continue on their
   original generation. Old resources close only after all leases
   drain (timeout: 300s default).

## Control socket

| Property | Value |
|----------|-------|
| Path | `~/.local/state/eggpool/eggpool.sock` |
| Permissions | `0o600` (owner-only read/write) |
| Protocol | Newline-delimited JSON v1 |
| Max request size | 64 KB |
| Command timeout | 30s |

The socket is created on server startup and cleaned up on stop. Stale
socket files are automatically removed before binding.

### Wire format (protocol v1)

Request (one JSON object per line):

```json
{
  "protocol_version": 1,
  "request_id": "<uuid>",
  "command": "reload_config",
  "validated_digest": "<sha-256-hex>"
}
```

Response (one JSON object per line):

```json
{
  "protocol_version": 1,
  "request_id": "<uuid>",
  "ok": true,
  "stage": "commit",
  "generation": 3,
  "changed_sections": ["routing", "accounts"],
  "warnings": [],
  "restart_required": [],
  "retirement_pending": false,
  "message": "rehash applied"
}
```

## `eggpool rehash` command behavior

The CLI performs these steps:

1. **Preflight validation** — Runs `validate_config_file()` against
   the config file. On failure, prints a clear error and exits
   non-zero. The running configuration is never touched.
2. **Connect to control socket** — Sends the validated
   `content_digest` to the running server. If the server is not
   running, prints "Control socket unavailable. Is the server running?"
   and exits non-zero.
3. **Render result** — On success, prints changed sections and the
   new generation number. On failure, lists restart-required fields.

### Error messages

| Message | Meaning |
|---------|---------|
| "Live configuration is unchanged. Refusing to apply an invalid config…" | Local validation failed; config was not sent to the server |
| "Control socket unavailable. Is the server running?" | Server is not running; use `eggpool restart` |
| "Control socket request timed out." | Server did not respond within 30s |
| "A reload transaction is already in progress" | Concurrent `rehash` rejected; only one reload at a time |
| "Restart-required changes: …" | One or more fields require a restart; use `eggpool restart` |

## Digests and fingerprints

The validation result carries two distinct hashes:

| Hash | Purpose |
|------|---------|
| `content_digest` | SHA-256 of the exact config file bytes. Guards against time-of-check / time-of-use drift. The CLI sends this to the server so it re-validates the same bytes. |
| `runtime_fingerprint` | Deterministic, secret-safe canonical hash. Secret fields (API keys, tokens) are redacted to `"<redacted>"` before hashing. Used for no-op detection ("is the running config identical to the new one?") and diagnostics. |

## Reload policy

Every `AppConfig` field is classified in the `_FIELD_DISPOSITION` map
in `config_reload_policy.py`:

| Disposition | Meaning |
|-------------|---------|
| `LIVE` | Can be hot-swapped without a restart (none currently) |
| `RESTART_REQUIRED` | Changing the field requires a service restart |
| `IGNORED` | Field is ignored for reload purposes |

**All fields are currently `RESTART_REQUIRED`.** This is fail-closed —
any field not explicitly classified requires a restart. When a future
milestone adds a live-reload path for a field, the corresponding entry
moves to `LIVE` in the same diff.

Currently, `eggpool rehash` will reject the entire operation for any
config change because all fields are `RESTART_REQUIRED`. Use
`eggpool restart` for any configuration change.

## Transaction safety

- **Serialized** — One reload at a time. Concurrent `rehash` commands
  are rejected with `reload_in_progress`.
- **Content digest** — Prevents TOCTOU races: the server re-validates
  the exact bytes the CLI validated.
- **Fail-closed** — All failures before publication are rolled back.
  The old generation remains active.
- **No secrets in logs** — Secret fields render as `<changed>` in
  diagnostic output.

## Old generation retirement

After publication, active streams continue on their original
generation. New requests immediately use the new generation. The old
generation is retired after all leases drain (default timeout: 300s).
During retirement, `retirement_pending: true` appears in the response.

## Troubleshooting

### Server is not running

```
Control socket unavailable. Is the server running?
Use `eggpool restart` for a disruptive configuration reload.
```

The server must be running for `eggpool rehash` to work. Start it
with `eggpool serve` (daemon mode) or via systemd.

### Concurrent reload in progress

```
A reload transaction is already in progress
```

Only one reload can execute at a time. Wait for the current reload to
complete, then retry.

### Restart-required changes

```
Restart-required changes:
  - server.host
  - server.port
```

These fields cannot be changed without a process restart. Use
`eggpool restart` (or `systemctl restart eggpool`).

### Socket permissions error

The socket is created with `0o600` (owner-only). If you see permission
errors, ensure you are running `eggpool rehash` as the same user that
owns the running server process.

### Stale socket file

Stale socket files are automatically cleaned up on server start. If
you see connection errors after a crash, try `eggpool restart` to
ensure a clean socket state.

## See also

- `architecture/README.md` § Live Configuration Rehash — validation
  contract, diff shape, wire types, and runtime generations
- `src/eggpool/config_validation.py` — reusable validation contract
- `src/eggpool/config_reload_policy.py` — typed diff and reload policy
- `src/eggpool/control/server.py` — control socket server
- `src/eggpool/control/client.py` — control socket client
- `src/eggpool/control/reload_manager.py` — transactional reload manager
