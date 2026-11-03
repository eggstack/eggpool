# Tool Integration M002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/tool-integration/002-tool-grants-and-credential-scopes.md`

Source subsystem roadmap:

- `plans/subsystems/tool-integration-roadmap.md#7-milestones`

Repository baseline reviewed: `cf385e49` (M002 activation)

Implementation commits or pull requests:

- `df2f6bb0` — additive policy migration, live authorization API, explicit token scopes, CLI, docs and compatibility guards.
- `cff31bde` — file-backed backup/restore grant persistence test.
- `da0450b9` — administrator-default-deny, legacy-principal denial assertions.

## 1. Executive finding

M002 is complete. Exact Eggsearch tool grants are independently default-deny
for all users, including administrators and existing inference credentials.
New credentials receive discovery and invocation scopes only when an operator
explicitly requests them. An internal authorization check reads current user,
credential, profile, override and instance-denial state before every decision.
No public tool route or invocation endpoint was added.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Default-deny additive schema | Migration `0065_tool_grants.sql`; migration inventory/checksum tests | pass | Empty grants for all legacy users and profiles. |
| Exact canonical backend/tool identities | `tool_policy.rs::KNOWN_TOOLS`; wildcard, case and Unicode collision tests | pass | Backend is fixed to `eggsearch`; tool names are an exact allowlist. |
| Policy precedence | `tool_grants` two-user/two-profile test | pass | Instance denial > user override > assigned profile > deny. |
| Independent credential scopes | `issue_api_token_with_tool_scopes`; `tool_grants` scope matrix | pass | Existing/default credentials gain no tool scope; discovery does not imply invocation. |
| Current credential and user state | `authorize_tool`; disabled-user and post-revocation denial tests | pass | New decisions re-read active user, credential expiry/revocation and auth epoch. |
| Admin mutations and audit | CLI contract; content-free audit rows in `tool_grants` | pass | Active admin required; audits retain action and canonical target only. |
| Profile edit conflict | Profile version compare in the SQLite write transaction; stale-version test | pass | Stale updates fail without replacing current policy. |
| Backup/restore and historical upgrade | `tool_grants_survive_backup_and_restore`; `database_compatibility` | pass | Grant remains effective after archive recovery; historical migrations remain additive. |
| No route or ordinary inference change | `server/*` unchanged; complete workspace suites | pass | No listener, runtime task, provider health effect or payload persistence. |

## 3. Production implementation evidence

Migration v65 adds profile rules, user overrides and instance-wide denials;
SQLite triggers advance the existing policy revision and profile version.
`rust/src/tool_policy.rs` is the sole EggPool authority for the fixed backend,
exact tool identifiers, explicit action scopes, fail-closed authorization,
admin mutations and the CLI grant projection. Credential issuance keeps the
existing inference/discovery/integration scopes and appends the two tool
scopes only when requested. Existing tokens are not rewritten.

The `eggpool users tool-policy` commands set/remove exact profile or user rules,
set/remove instance denials, and inspect effective grants with assigned profile
version. User/profile writes and audit entries commit in one existing SQLite
transaction. Authorization has no process cache and stores no tool arguments
or results.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
CARGO_PROFILE_TEST_DEBUG=0 CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
CARGO_PROFILE_TEST_DEBUG=0 CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test tool_grants -- --test-threads=1
CARGO_PROFILE_TEST_DEBUG=0 CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --no-default-features --test tool_grants -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_identity --test on_prem_model_policy -- --test-threads=1
```

### Results

- Formatting, default and no-default strict Clippy/checks passed.
- The complete serial default and no-default workspace suites passed. The
  dashboard TTFT physical/query-plan qualification remains the suite's existing
  ignored manual test.
- Final focused `tool_grants` passed in both feature modes (4 tests each),
  including backup/restore, administrator denial, cross-user isolation,
  disabled/revoked credentials, stale profile version, audit redaction and
  independent token scopes. CLI contract (7), database compatibility (12),
  on-prem identity (5) and model policy (6) passed.
- Test builds used one Cargo job and no test debug symbols after the full
  debug-symbol build exhausted the filesystem. Cargo artifacts were cleaned
  and the focused no-default test was rerun successfully from a clean target.

## 5. Invariant review

- Default policy is deny for members, administrators, unprofiled users and
  legacy credentials. Tool grants do not affect model authorization.
- Discovery and invocation require separate exact scopes and each operation
  checks its own required scope.
- Instance denials override all user/profile rules; user overrides override
  profile rules; missing policy denies.
- Every decision validates the current database credential, user state and
  auth epoch. A committed revoke or disable denies subsequent checks.
- No endpoint, worker, extra listener, backend transport, tool body storage or
  second SQLite gate was introduced.

## 6. Failure and recovery review

Mutations use the serialized database transaction and fail closed when the
database or active administrator is unavailable. Stale profile versions fail
without applying a rule. New authorization checks observe revocation; already
admitted in-flight tool work is deliberately not promised because M002 adds no
tool dispatch. Backup/restore preserves the exact grant rows and revision.

## 7. Migration and compatibility review

Migration v65 is additive and leaves v1–v64 immutable. It does not backfill
grants or add scopes to stored credentials. Historical schema fixtures, the
current database backup/recovery path, runtime manifest hash, and serial
startup migration checks pass. Existing inference tokens and routes retain
their behavior.

## 8. Security review

Only the fixed `eggsearch` backend and the ten exact bundled tool names are
accepted. Wildcards, case aliases, Unicode lookalikes, legacy shared keys,
browser sessions, stale credentials, disabled users, missing grants and
missing action scopes deny. Tool arguments, documents, URLs and results are not
stored or audited. Audit targets include only canonical profile/user/backend
and tool identifiers.

## 9. Documentation and operations

Updated the CLI reference, API scope caveat, on-prem identity ownership deep
dive, architecture overview, migration count/checksum and CLI contract matrix.
The CLI clearly distinguishes effective grants from credential scopes and
states that M002 adds no tool HTTP route.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| — | None within M002 scope. | M003 still requires protocol and live Eggsearch qualification before it can be promoted. | Continue at M003's existing blocked gate. |

## 11. Roadmap disposition

M002 is closed. M003's M002 dependency is satisfied, but M003 stays blocked
until the supported Eggsearch MCP transport/session and `tools/call` contract
is qualified against the pinned local service. M004, E001 and R001 remain
blocked on their independent declared dependencies.

## 12. Registry updates

This closure commit moves M002 from active to closed, removes it from the ready
and active tables, adds this record to recently closed, and audits the
dependents. No successor is promoted until its remaining interface or
operational evidence gate is satisfied.
