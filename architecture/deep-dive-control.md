# Deep Dive: Control Plane

Back to [Overview](overview.md)

## Purpose

The control plane provides live configuration reload (rehash) via a Unix-domain socket, enabling zero-downtime config changes without process restart.

## Module Structure

```
src/eggpool/control/
├── __init__.py
├── server.py               # UDS server (~617 lines)
├── client.py               # CLI client for rehash commands
├── reload_manager.py       # Staged reload orchestration
└── accepted_finalization.py  # Post-acceptance finalization lifecycle
```

## Key Components

### Control Server (`server.py`)

Unix-domain socket server (~617 lines) implementing a single-shot newline-delimited JSON protocol (v1) for `eggpool rehash`.

**Wire format:**
```json
// Request (one JSON object per connection)
{
  "protocol_version": 1,
  "request_id": "<uuid>",
  "command": "reload_config",
  "validated_digest": "<sha-256>",
  "params": {}
}

// Response (one JSON object per line)
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

**Security model:**
- Runtime directory is created and verified as an owner-only (`0o700`)
  non-symlink directory owned by the effective UID.
- Socket is created with mode `0o600` (owner-only read/write), and startup
  fails closed if ownership or mode verification cannot complete.
- Stale cleanup probes a real socket, requires current-UID ownership, and
  removes only the inode observed by the probe; symlinks, regular files,
  active sockets, and foreign-owned sockets are left untouched.
- Linux peer credentials are checked when `SO_PEERCRED` is available.
- Path: `<runtime_dir>/eggpool.sock` (`$EGGPOOL_RUNTIME_DIR` → a suitable
  `$XDG_RUNTIME_DIR/eggpool` → private state/runtime fallback → UID-scoped
  `/tmp` fallback).

The request envelope is bounded to 64 KiB, accepts only protocol version 1,
requires a bounded safe request ID and a known command, and rejects non-object
JSON, non-object `params`, invalid digests, invalid UTF-8, and multiple frames
on one connection before dispatch. Reads and writes have bounded timeouts.

**Connection model:** One request per connection, structured response, then close. Designed for short-lived CLI interactions.

### Control Client (`client.py`)

CLI-side client that connects to the UDS, sends a reload command, and reads the structured response. Used by `eggpool rehash` command.

### Reload Manager (`reload_manager.py`)

Orchestrates the staged reload sequence:
1. **Validate** — parse and validate new config
2. **Stage** — prepare new `RuntimeGeneration` candidate
3. **Diff** — classify fields via `config_reload_policy.py` (LIVE vs RESTART_REQUIRED)
4. **Commit** — atomic swap if all changes are LIVE-compatible
5. **Rollback** — restore previous generation on failure
6. **Finalize retirement** — mark old generation for drain

The reload manager coordinates with `RuntimeManager`, `ReloadTransaction`, and `RequestFinalizationSupervisor` to ensure zero-downtime transitions.

Model-router affinity is process-owned, so an unrelated live configuration
change preserves existing sticky decisions when the newly compiled router has
the same semantic fingerprint. Changing a router's selector, default, target
set, descriptions, or other fingerprint input makes old entries unreachable
without a cache rewrite. Removing a router makes its old entries unreachable
because the new generation no longer resolves the alias. A candidate that
fails validation or construction is never published and cannot disturb the
active generation or its affinity cache.

### Accepted Finalization (`accepted_finalization.py`)

Tracks post-acceptance finalization lifecycle for committed reloads. For each accepted reload, a process-owned `AcceptedReloadFinalizationJob` executes idempotent steps (ownership transfer, mirror update, transition finalization, observer reporting, retirement scheduling, transaction completion) in order. Completed steps are not repeated on retry; failure leaves the job registered at the exact failed step; cancellation preserves the job so retry can resume.

## Reload Transaction (`reload_transaction.py`)

A monotonic state machine with atomic commit semantics across SQLite and runtime publication. States progress through:
- `RUNTIME_STAGED` → `RUNTIME_SWAP_COMMITTED` → `COMPLETED`
- On failure: `ABORTING` → `ABORTED` (or `COMPENSATION_FAILED`)

The transaction ensures that config changes and runtime generation swaps are atomic — either both succeed or both roll back.

## Configuration

The control plane has no separate config section. It is activated by:
- `eggpool rehash` CLI command (client)
- Server starts automatically with the Granian worker
- Socket path derived from `runtime_paths.py`

## Key Invariants

- Only one reload can be in progress at a time — concurrent attempts rejected with `reload_in_progress`
- Reload transaction is atomic — partial apply never occurs
- Old generation drains in-flight requests before retirement
- `RuntimeManagerLeaseExhaustedError` → HTTP 503 if lease pool is exhausted during swap
- Socket permissions prevent unprivileged access on shared hosts

## Related

- [deep-dive-runtime.md](deep-dive-runtime.md) — Runtime generations and the generation swap mechanism
- [deep-dive-core.md](deep-dive-core.md) — Config reload policy (LIVE vs RESTART_REQUIRED fields)
- [deep-dive-deployment.md](deep-dive-deployment.md) — Operational use of `eggpool rehash`
