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
└── accepted_finalization.py  # Reload invariant tracking
```

## Key Components

### Control Server (`server.py`)

Unix-domain socket server (~617 lines) implementing a single-shot newline-delimited JSON protocol (v1) for `eggpool rehash`.

**Wire format:**
```json
// Request (one JSON object per line)
{
  "protocol_version": 1,
  "request_id": "<uuid>",
  "command": "reload_config",
  "validated_digest": "<sha-256>"
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
- Socket created with mode `0o600` (owner-only read/write)
- Socket cleaned up on server stop and at startup if stale
- Path: `~/.local/state/eggpool/eggpool.sock`

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

### Accepted Finalization (`accepted_finalization.py`)

Tracks reload invariants during the staged reload process. Ensures that:
- Only one reload is in progress at a time
- Concurrent rejections return `reload_in_progress`
- The reload transaction completes atomically

## Reload Transaction (`reload_transaction.py`)

A monotonic state machine with atomic commit semantics across SQLite and runtime publication. States progress through:
- `STAGED` → `COMMITTED` → `RETIREMENT_FINALIZED`
- On failure: `STAGED` → `ROLLED_BACK`

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
