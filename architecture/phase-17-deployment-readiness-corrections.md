# Phase 17: Deployment Readiness Corrections

## Purpose

Phase 16 finalized the release-polish architecture, but a final
review identified several deployment blockers and operational
mismatches that must be corrected before GoRouter runs against
real OpenCode Go subscription keys. This phase is narrowly scoped:
fix the remaining blockers, close the missing regression
coverage, and make operational tooling accurately reflect runtime
behavior.

## Summary of changes

### 1. Enforced explicit database write transactions

Every DML write must now occur inside an `async with
db.transaction():` block owned by the current task. The
`Database` class enforces this through a new
`_require_transaction_owner()` guard called at the start of
`execute_write`, `execute_insert`, `execute_returning`, and
`_execute_cursor`. Outside a transaction these helpers raise
`DatabaseError` instead of opening an implicit transaction.

A new `execute_pragma()` helper holds the connection lock for the
full execute-and-fetch cycle and only accepts SQL beginning with
`PRAGMA `. This replaces the previous raw-cursor use for WAL
checkpointing.

`AccountRepository.sync_from_config()` is wrapped in a single
outer transaction so the "update existing / insert new / disable
removed" sequence commits atomically. The previous per-account
transaction boundary left implicit transactions in place if a
later step failed.

`RequestCoordinator._handle_exhausted()` now wraps the
"update an existing pending request without a selected attempt"
branch in a transaction so the update is atomic and does not
leave a half-applied error row if the request had already been
finalized by another path.

The legacy `db.execute()` public wrapper is removed; tests and
seed helpers use the explicit `execute_write` / `execute_insert`
/ `execute_returning` / `fetch_one` / `fetch_all` helpers inside
transactions.

### 2. Prevented local credential forwarding upstream

`filter_request_headers()` now strips an explicit
`LOCAL_CREDENTIAL_HEADERS` set (`authorization`, `x-api-key`,
`proxy-authorization`) regardless of casing before forwarding
the request. The selected account's credential is then injected
by a single `build_upstream_auth_headers()` helper that returns
exactly one `Authorization: Bearer` header for both OpenAI- and
Anthropic-compatible payloads. This prevents duplicate
`Authorization` fields and ensures the local key never reaches
the upstream gateway.

### 3. Repaired CI dependency

`pytest-cov` is now part of the dev extra in `pyproject.toml` so
the coverage job in `.github/workflows/ci.yml` runs against a
clean frozen checkout. The CI test job gains a small smoke step
that runs `pytest --help | grep -- --cov` and fails early if the
plugin disappears from the lockfile.

### 4. Restored migration immutability

`0005_price_microdollars.sql` is restored to its exact historical
contents (`source TEXT NOT NULL DEFAULT 'config'`). No new
forward migration is added because production insert paths
always provide an explicit `source` value.

A migration manifest at `src/go_aggregator/db/schema/checksums.json`
records the SHA-256 of every applied migration file. A future
edit to an existing migration will fail
`tests/integration/test_migration_compatibility.py` until the
checksum is updated, which requires an explicit review decision.
New migrations append new entries without touching existing ones.

### 5. Enforced raw cursor API contract

`_execute_cursor()` now requires transaction ownership: the
caller must hold the connection lock through
`async with db.transaction():`. Outside a transaction the
connection lock would be released when the method returns, so any
subsequent use of the cursor would race with other concurrent
tasks. The dedicated `execute_pragma()` helper exists for the
WAL checkpoint case so the connection lock is held for the full
execute-and-fetch cycle.

### 6. Added real coordinator-level 402 lifecycle test

`tests/integration/test_phase17_release_validation.py` exercises
the full quota-exhaustion behavior through the coordinator:

- Two accounts are configured; the router picks the first
  non-deterministically. The first request returns 402 for one
  account and 200 for the other.
- The 402 account is placed in `quota_exhausted` cooldown by
  both the `HealthManager` and the `AccountRuntimeState`.
- A subsequent independent request does not attempt the
  exhausted account while the cooldown is active.
- After advancing the mocked cooldown clock, the formerly
  exhausted account becomes eligible again and is attempted.

The test asserts that all attempts are terminal, no active
reservations remain, no request is still pending, no active
request count remains for either account, the 402 attempt
affects health exactly once, and no raw provider body is
persisted to the database. Cooldown expiration is simulated by
rewinding `cooldown_until` directly so the test does not depend
on `time.sleep()`.

### 7. Validated incremental streaming in smoke tests

`scripts/smoke_test.py` now uses `httpx.Client.stream()` for the
streaming OpenAI and Anthropic requests. It records the timing
of request start, response headers, first nonempty chunk, and
stream completion. It requires the proxy request ID and
attempt-count headers, verifies at least one known SSE marker
(`data:` for OpenAI, `event:` for Anthropic), and surfaces any
transport error.

All model environment variables (`GOROUTER_OPENAI_MODEL` and
`GOROUTER_ANTHROPIC_MODEL`) are now required at runtime so stale
generic IDs cannot produce misleading deployment failures.
`GOROUTER_TEST_STREAM_CANCEL=1` closes the response after the
first nonempty chunk to exercise the client cancellation path.
`GOROUTER_SKIP_LIVE=1` is supported for the unit test harness.

The script never logs or echoes request bodies, response bodies,
or secrets; it only reports endpoint status codes, timing, and
structural SSE markers.

### 8. Removed unsupported systemd reload action

`ops/gorouter.service` and `deploy/gorouter.service` no longer
advertise `ExecReload=/bin/kill -HUP $MAINPID`. GoRouter has no
tested configuration-reload implementation, and a silent
`kill -HUP` against a Python process that does not handle SIGHUP
is worse than an explicit restart-only policy.

`docs/deployment.md` and `README.md` now document the restart
workflow:

```bash
sudo systemctl restart gorouter
sudo systemctl status gorouter
sudo journalctl -u gorouter -n 100 --no-pager
```

`architecture/phase-9.md` and the Workstream H section of
`architecture/phase-10-integration-hardening.md` are updated to
reflect the restart-only policy.

### 9. Hardened persisted error-detail privacy

`security.redaction.redact_error_detail()` is strengthened to
cover common JSON credential forms. The new logic also accepts a
JSON payload and recursively sanitizes it (replacing values for
sensitive keys with `[REDACTED]`, retaining safe keys like
`type`, `code`, and bounded `message` only after string
redaction, and limiting depth, item count, and total serialized
size).

`RequestFinalizer` and `AttemptFinalizer` use the strengthened
redactor. A new `persist_redacted_error_detail: bool = False`
configuration default keeps the field set to `None` so no
arbitrary provider error detail is persisted by default; only
`error_class`, `status_code`, `upstream_request_id`, and a safe
event type are stored.

### 10. Tightened operational scripts

`scripts/check_database.py` is now a genuine read-only
diagnostic. The `Database` constructor accepts a `read_only=True`
flag; the checker opens the file via a `file:...?mode=ro` URI so
the invariant checks cannot change journal mode, create WAL
files, apply migrations, write health-probe rows, or mutate
PRAGMAs beyond safe read-only settings.

The checker inspects `_migrations` first and returns exit code 2
with a clear message if the schema is older or newer than the
checker expects, instead of crashing with a raw
`no such column` exception.

The documented exit codes are:

- `0` = all invariants pass
- `1` = invariant violation
- `2` = configuration or database access error

The smoke test now requires explicit `GOROUTER_OPENAI_MODEL` and
`GOROUTER_ANTHROPIC_MODEL` values; generic placeholder IDs
cannot produce misleading deployment failures.

## Key Files

| File | Changes |
|------|---------|
| `src/go_aggregator/db/connection.py` | `_require_transaction_owner`, `execute_pragma` |
| `src/go_aggregator/db/repositories.py` | Atomic `sync_from_config` |
| `src/go_aggregator/request/coordinator.py` | Transaction-owned writes in `_handle_exhausted` |
| `src/go_aggregator/request/finalizer.py` | Strengthened redaction |
| `src/go_aggregator/request/attempt_finalizer.py` | Strengthened redaction |
| `src/go_aggregator/security/redaction.py` | JSON key/value redaction |
| `src/go_aggregator/proxy/client.py` | `LOCAL_CREDENTIAL_HEADERS`, `build_upstream_auth_headers` |
| `src/go_aggregator/background/cleanup.py` | `execute_pragma` for WAL checkpoint |
| `src/go_aggregator/app.py` | Use `execute_pragma` |
| `ops/gorouter.service` | `ExecReload` removed |
| `deploy/gorouter.service` | `ExecReload` removed |
| `docs/deployment.md` | Restart workflow |
| `scripts/smoke_test.py` | Incremental streaming, required model IDs |
| `scripts/check_database.py` | Read-only, exit codes, schema check |
| `src/go_aggregator/db/schema/checksums.json` | Migration manifest |
| `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml` | `pytest-cov`, cov smoke |
| `tests/integration/test_database_transaction_contract.py` | Write-helper contract |
| `tests/integration/test_application_startup.py` | Fresh-startup integration |
| `tests/integration/test_migration_compatibility.py` | Migration equivalence and checksum |
| `tests/integration/test_upstream_credential_boundary.py` | Credential boundary |
| `tests/integration/test_phase17_release_validation.py` | Real 402 lifecycle |
| `tests/unit/test_header_filtering.py` | Header filtering |
| `tests/unit/test_smoke_test.py` | Smoke test logic |

## Phase exit outcomes

Phase 17 is complete when:

1. No standalone database write can leave an implicit SQLite
   transaction open.
2. Fresh application startup with configured accounts and a
   file-backed database succeeds.
3. Local proxy credentials are never forwarded upstream in any
   header form.
4. CI has all required dependencies and visibly passes on the
   head commit.
5. Migration history is deterministic across old and fresh
   installations.
6. Raw cursor access is enforced as transaction-owner-only.
7. A real coordinator-level 402 test verifies failover,
   cross-request cooldown, and recovery.
8. The smoke test verifies incremental streaming rather than
   only buffered success.
9. The systemd unit exposes only supported lifecycle operations.
10. Persisted error-detail redaction covers common JSON
    credential forms or error detail is disabled by default.
11. Operational scripts are safe and accurately documented.
12. The final release-validation matrix passes locally and in
    CI. (Raspberry Pi deployment validation is documented in
    `docs/raspberry-pi.md` and is performed by the operator.)
