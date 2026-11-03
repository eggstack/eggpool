# Tool Integration M005 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/tool-integration/005-canonical-tool-identity-correction.md`

Source subsystem roadmap:

- `plans/subsystems/tool-integration-roadmap.md#7-milestones`

Repository baseline reviewed: `3fcbd712` (M002 closure)

Implementation commits or pull requests:

- `c115fdbc` — use one canonical Eggsearch tool identity list for readiness and authorization.

## 1. Executive finding

The policy allowlist now exactly matches the ten tools advertised by the
qualified Eggsearch 0.4.3 service. Readiness and grant validation both consume
`rust/src/tool_policy.rs::KNOWN_TOOLS`. M003's hard corrective and live MCP
contract gates are satisfied, so M003 is promoted to ready. This correction
adds no route and does not alter previously persisted grants because M002 had
not exposed invocation publicly.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| One source of canonical names | `rust/src/tool_policy.rs`; `rust/src/operations/tools.rs` reads `KNOWN_TOOLS` | pass | The duplicate readiness constant was removed. |
| Match the live pinned catalog | Eggsearch 0.4.3 `/healthz`, MCP `initialize`, `tools/list`, and `tools/call` loopback session; rebuilt `eggpool tools doctor --json` returned `ready`, version `0.4.3`, count `10` | pass | The live `provider_status` call returned a valid MCP result on the initialized session. |
| Exact names and rejection of noncanonical identities | `tool_policy` exact-list unit assertion and grant tests | pass | Case-folded, Unicode-lookalike, wildcard, and unknown grants remain rejected. |
| Readiness and grants agree | `tool_backend_config` live catalog fixture and `tool_grants` policy suite | pass | Both consumers use the shared list. |
| No migration or stored-grant rewrite | No schema or database changes; M002 grants were not publicly invokable | pass | Existing policy rows remain valid; no renaming is needed. |

Canonical Eggsearch names, in sorted order:
`batch_fetch`, `build_evidence_bundle`, `provider_status`, `repo_fetch`,
`repo_map`, `repo_search`, `research_search`, `security_search`,
`web_fetch`, `web_search`.

## 3. Production implementation evidence

`KNOWN_TOOLS` now contains the exact M001/Eggsearch 0.4.3 catalog. The explicit
readiness doctor checks the live catalog against that list, and authorization,
administrative policy mutation, and grant resolution continue using the same
authority. A regression assertion freezes the canonical ten-name list.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all
rtk cargo test --manifest-path rust/Cargo.toml --test tool_backend_config -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test tool_grants -- --test-threads=1
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
EGGPOOL_CONFIG=/tmp/eggpool-m005-tools.toml rtk cargo run --manifest-path rust/Cargo.toml -- tools doctor --json
```

### Results

- Formatting completed; `git diff --check` passed.
- Focused backend suite: 4 passed; grants suite: 4 passed.
- Database compatibility: 12 passed; CLI contract: 7 passed.
- Strict Clippy: default and no-default both reported no issues.
- Full default workspace: 1,227 passed, 1 ignored across 92 suites.
- Full no-default workspace: 1,228 passed, 1 ignored across 92 suites.
- Rebuilt CLI doctor against live Eggsearch 0.4.3 returned `{"schema":"eggpool.tools.doctor/v1","status":"ready","service_version":"0.4.3","tool_count":10}`. A live Streamable HTTP session completed initialize, list, and `provider_status` call.

Commands ran locally in this workspace; no hosted CI result is claimed.

## 5. Invariant review

- Tool IDs remain exact, case-sensitive and finite. Regression coverage rejects
  unknown, wildcard, case-folded and Unicode-lookalike names.
- Backend identity remains the fixed `eggsearch` value.
- Readiness still performs only the explicit bounded operator probe; this
  change adds no background task, retained client or default probe.
- Unknown grants and calls continue to fail closed.
- No credentials, arguments, source text or result content are persisted or
  added to diagnostics.

## 6. Failure and recovery review

No new asynchronous execution, persistence, cancellation, restart or contention
path was added. Catalog mismatch continues to produce `IncompatibleCatalog`;
unreachable or malformed responses keep their existing bounded diagnostic
class. The shared identity list makes catalog drift fail closed before M003
can expose a partial catalog.

## 7. Migration and compatibility review

No migration, configuration change, protocol change or public endpoint was
introduced. Existing M002 policy records remain unchanged. Default and
no-default workspace suites pass.

## 8. Security review

The correction removes acceptance of eight ghost names and permits grants for
the eight actual names that were previously absent from the policy authority.
All names remain individually subject to the existing user/profile/instance
precedence and per-credential scopes. The live qualification used a local
loopback service and made no downstream credentials available to Eggsearch.

## 9. Documentation and operations

The M005 plan and this closure record capture the exact Eggsearch 0.4.3 names,
protocol qualification, tests and dependency disposition. The existing
`eggpool tools doctor` command remains the explicit operator readiness check.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No M005 findings remain. | none | none |

## 11. Roadmap disposition

M005 is closed. The canonical identity and real Eggsearch session/list/call
contract gates for M003 are satisfied. M003 is promoted from blocked to ready
for implementation. M004 remains blocked on M003 closure and coordinator/IR
qualification. E001 remains blocked on a qualified live embedding provider.
R001 remains blocked on M003 and backend-enforced document ACL/delegation
evidence.

## 12. Registry updates

`plans/registry.md` removes M005 from active work, records it under recently
closed, promotes M003 to dependency-ready, and retains the remaining blockers.
The tool-integration roadmap records M005 closed and M003 ready.
