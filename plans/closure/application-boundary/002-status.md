# Application Boundary Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/application-boundary/002-content-free-gateway-audit.md`

Source subsystem roadmap:

- `plans/subsystems/application-boundary-roadmap.md#m002--content-free-gateway-audit-export`

Repository baseline reviewed: `e049025274a397d7799c3ae21ed106947c2a1572`

Implementation commits:

- Uncommitted working tree on that baseline. No commit has been created.

## 1. Executive finding

M002 is complete. An active administrator can export content-free gateway
facts as JSON lines from `eggpool audit export` and from
`GET /api/admin/audit/export`. Members and personal API tokens cannot.
Each of the administration and inference sections is capped independently
at the requested limit. A short page sets `truncated` rather than omitting
the flag. Schema version is 1. No migration was added.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Allowlisted keys only | `GATEWAY_AUDIT_KEYS` and `assert_allowlisted` in `rust/tests/gateway_audit.rs` | pass | The fixture is the test, not a separate JSON file. |
| A prompt stored beside the row does not appear | Sentinel `PROMPT-SENTINEL do not export this body` written into `error_detail` and `error_message` | pass | Those columns are not selected. An `error_class` that is not `^[A-Za-z0-9_]{1,64}$` is dropped. |
| Cap and truncation | `gateway_audit_export(..., 1)` with two logical rows and one admin row | pass | `truncated` is true, each section length is 1, limit is 1. `0` and `2001` are rejected. |
| Administrator HTTP success | Browser session, `allow_insecure_web_auth` because oneshot has no connection info | pass | 200, `kind` `export`, includes `issue_token`. Password is absent. |
| Member denial | Member browser session | pass | 403. |
| Agent-token denial | Administrator and member personal tokens | pass | Status is 401 or 403, never 200. The path is not a personal-credential path. |
| Selector children excluded | `request_kind = 'internal_selector'` row | pass | Query filters `request_kind = 'logical'`. |
| No migration | No file under `rust/assets/db/migrations` changed | pass | Budget reason is a projection of stored `error_class`. |
| Docs name fields and caps | `docs/cli-reference.md`, `docs/api-reference.md` | pass | Default 500, maximum 2000, schema version 1. |

## 3. Production implementation evidence

`identity::gateway_audit_export` is the only export shape. It takes one
SQLite gate call. The administration query unions `user_admin_audit` with
`user_budget_rule_audit`, newest first, `LIMIT n+1`. The inference query
reads logical request rows only: proxy request id, user id, credential id,
model id, protocol, status, input and output tokens, rounded latency,
`error_class` filtered by `bounded_reason_code`, and `started_at`. It does
not select `error_detail`, `error_message`, thinking traces, or raw usage.

`GatewayAuditExport::to_json_lines` writes administration lines, inference
lines, then a trailer `kind: "export"` with `truncated` and `limit`, and a
trailing newline. `truncated` is true when either section returned `n+1`.

The CLI command is `eggpool audit export` with `--limit` default 500 and
`--actor` default `admin`. The actor must be an active administrator via
`policy_actor_id`. The database file must already exist. The HTTP handler
requires a browser session whose role is `admin`. Members are refused by
the existing HTML 403 before the handler. The handler also refuses a
principal that is not an admin browser session. A limit outside 1..=2000
is HTTP 400. A read failure is HTTP 503 JSON. Success is
`application/x-ndjson; charset=utf-8`. The dashboard audit page links the
export and does not gain prompt columns.

Budget denials that roll back before insert are not request rows. A
persisted `error_class` is projected as `reason_code` only when it matches
`^[A-Za-z0-9_]{1,64}$`. No column was added to distinguish user-budget
denial from instance-budget denial, because publication does not store
that distinction on a durable denied request row.

## 4. Verification executed

### Commands run

Local, offline, not CI. `on_prem_attribution`, `on_prem_admin_ui`,
`cli_contract`, and `gateway_audit` were not invoked as separate commands.
They passed inside both full serial suites. `database_compatibility` was
not added to the focused list because no migration was introduced. It ran
inside both suites as part of `--workspace --all-targets`.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --offline -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --offline -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features --offline -- --test-threads=1
```

### Results

- `cargo fmt --check`: exit 0.
- `cargo clippy` with `-D warnings`: exit 0.
- Default serial suite: exit 0, elapsed 346s. `gateway_audit` 2 passed. `on_prem_admin_ui` 13 passed. `on_prem_attribution` 3 passed. `cli_contract` 7 passed.
- No-default serial suite: exit 0, elapsed 415s.

## 5. Invariant review

The export selects no prompt, raw body, cache key, tool argument, tool
result, token secret, or provider credential. The test plants a prompt
sentinel in unused columns and asserts it is absent. The read is one
`Database::call`. Members do not receive the export. Selector children
stay out of the inference section. Publication and finalization stay in
`rust/src/coordinator/`.

## 6. Failure and recovery review

A concurrent finalization can omit the newest row until the next export.
Cancellation closes the HTTP response. Restart does not drop durable rows.
The read is not on the inference admission path. A truncated result is
flagged. An out-of-range limit fails before the query. A missing database
file fails the CLI through the existing open path.

## 7. Migration and compatibility review

No schema change. Export schema version is 1. Older databases export the
administration and attribution columns they already have. The dashboard
administration page remains.

## 8. Security review

The route is in `requires_browser_session` and `requires_auth`, and it is
not in `personal_credential_allowed`. The handler checks
`PrincipalKind::BrowserSession` and `role == Some("admin")`. The CLI actor
must be an active administrator. The HTTP test sets
`allow_insecure_web_auth` only because the oneshot fixture has no
connection info. Production loopback HTTPS checks are unchanged. The
serialized password from bootstrap does not appear in the body. Row caps
are 500 by default and 2000 at most, per section.

## 9. Documentation and operations

Updated: `docs/cli-reference.md`, `docs/api-reference.md`,
`docs/deployment.md`, `architecture/deep-dive-on-prem-user-access.md`,
`architecture/deep-dive-observability.md`. The observability note says the
export is a projection, not a new telemetry pipeline. EggUI transcripts and
CodeGG sessions stay in those applications.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | User-budget and instance-budget denials that roll back before insert have no request row | The export cannot show a denial that was never stored | None. A migration was refused for that reason. |
| low | Working tree is uncommitted | Closure SHAs are not yet a commit | Commit when requested. |

## 11. Roadmap disposition

Milestone closed. No successor is registered. The export does not authorize
a transcript log or a network sink.

## 12. Registry updates

Application-boundary M002 leaves the dependency-ready table. The roadmap
row points at this record. Nothing else was blocked on M002.
