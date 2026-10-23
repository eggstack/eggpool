# Operations CLI Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/operations-cli/001-operator-status-projection-fidelity.md`

Source subsystem roadmap:

- `plans/subsystems/operations-cli-roadmap.md#milestone-001--operator-status-projection-fidelity`

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- Not committed. The implementation is in the working tree on `main` at the
  baseline above; no commit was created.

## 1. Executive finding

M001 is complete and **closed**. The CLI's status projection no longer has
two independent shapes:

- the offline fallback is now built from the same typed
  `ProxyStatusSnapshot` / `ProxyHealthSummary` / `ProviderHealthSummary` /
  `RuntimeHealthSummary` structs the server publishes, so a field added to a
  typed struct can no longer be silently absent from the fallback;
- the status projection now routes through a real redaction projection on
  both the online and the offline path, instead of a passthrough;
- a structural guard asserts the one-schema property.

The published status schema is unchanged: no field was added, renamed, or
removed on a published struct, the CLI command tree and argument surface are
unchanged, and the CLI's human-readable output wording is unchanged.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| The offline fallback's field set equals the server projection's field set | `rust/tests/status_projection.rs::offline_fallback_carries_the_published_status_schema` and `..._end_to_end` | pass | The top-level key set, the per-provider key set, and the `proxy` and `runtime` key sets are compared |
| Every provider entry carries the typed provider summary's full field set | The same two tests compare each offline row's key set to `ProviderHealthSummary` | pass | — |
| The fallback still reports the server as unavailable | `rust/tests/status_projection.rs::offline_fallback_reports_unavailable_instead_of_observed_health` and `..._end_to_end` | pass | `status` unavailable, `ready` and `available` false, provider health unobserved, runtime facts unknown |
| Providers are ordered and bounded | `rust/tests/status_projection.rs::offline_fallback_orders_and_bounds_provider_rows` and `..._end_to_end` | pass | Sorted by provider id, truncated to `MAX_STATUS_PROVIDERS`, provider ids bounded to `MAX_PROVIDER_ID_CHARS` |
| Secret-shaped material does not appear in the projection | `rust/tests/status_projection.rs::redaction_removes_secret_shaped_material` and `..._end_to_end` | pass | A member named `api_key`, a member named `authorization`, a `Bearer <token>` value, and an `sk-` prefixed value are all removed |
| Non-sensitive fields survive redaction | `rust/tests/status_projection.rs::redaction_preserves_non_sensitive_fields` | pass | `provider_id`, `status`, and `enabled_accounts` survive unchanged |
| The redaction is safe on unusual input | `rust/tests/status_projection.rs::redaction_is_safe_on_unusual_input` | pass | A very large string, a deeply nested object, an empty object, an array, a non-string scalar, and a control character are all handled without panicking |
| The CLI's human-readable output wording is unchanged | `rust/tests/status_command.rs::status_cli_human_output_labels_are_unchanged` | pass | The known labels are asserted as they are |
| The published status schema is unchanged | `rust/src/operations/status.rs` diff adds `offline_snapshot`, `OfflineProviderFacts`, `redact_status_projection`, `is_secret_bearing_key`, and `REDACTED_MARKER`; no published struct field was added, renamed, or removed | pass | The additions are new API, not schema changes |
| The CLI command tree and argument surface are unchanged | `rust/src/cli.rs` untouched | pass | — |
| Strict Clippy with `-D warnings` | `cargo clippy --workspace --all-targets -- -D warnings` | pass | Zero findings |
| Default and no-default workspace suites pass | The affected targets green (§4) | partial | The full serial workspace suite and the `--no-default-features` build were **not** run in this pass, by instruction to keep verification targeted. Outstanding (§10) |

## 3. Production implementation evidence

- `rust/src/operations/status.rs`: new `OfflineProviderFacts` (provider id,
  total accounts, enabled accounts), new `offline_snapshot`, which builds
  the CLI's offline fallback from the same typed structs the server
  publishes and serializes through the same `Serialize` path, and new
  `REDACTED_MARKER`, `is_secret_bearing_key`, and `redact_status_projection`,
  a recursive projection that replaces secret-bearing member names and
  secret-shaped string values with the marker while preserving the
  structure.
- `rust/src/runtime.rs`: `sanitize_status_value` now delegates to
  `redact_status_projection` instead of returning its input unchanged, and
  `offline_status_snapshot` builds the typed snapshot and serializes it
  through `serde_json::to_value` before routing it through the redaction.
  The online path routes through the same redaction.
- `architecture/overview.md`: a new review-index entry for the operations
  CLI surface.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test status_projection -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml \
  --test routing_fail_closed --test quota --test routing_domain \
  --test routing_domain_d008 --test routing_claims \
  --test routing_selection_efficiency --test health \
  --test durable_health_effects --test terminal_detail_redaction \
  --test coordinator_failure --test server_auth \
  --test coordinator_finalization --test coordinator_c007 \
  --test coordinator_c008 --test coordinator_c009 --test coordinator_c010 \
  --test coordinator_c011 --test coordinator_publication \
  --test database_compatibility --test on_prem_budgets \
  --test on_prem_global_budgets --test status_projection \
  --test status_command --test dashboard_filters \
  --test dashboard_ttft_qualification --test stats_transcoding \
  -- --test-threads=1
```

### Results

- `cargo fmt --all` — clean.
- `cargo clippy --workspace --all-targets -- -D warnings` — **pass**.
- `status_projection` — 10 passed, 0 failed.
- `status_command` — 15 passed, 0 failed.
- The 25-target sweep — 25 targets, 0 failures, 1 pre-existing ignored
  test.

### Regression-guard evidence

Three reverts were applied in an isolated worktree at the baseline commit
and the corresponding tests re-run, then the production code was restored
and confirmed byte-identical by checksum:

| Revert | Result |
|---|---|
| `sanitize_status_value` back to `value.clone()` | 2 failures — the CLI published `api_key: "sk-cli-status-secret-0001"` and `authorization: "Bearer CliStatusBearerToken0001"` |
| `offline_status_snapshot` back to the hand-written `json!` literal | 1 failure — the provider id was not bounded (105 against the published 96) |
| The hand-written literal plus a dropped typed field (`digest_prefix`) | 2 failures — the CLI's offline runtime key set drifted from the published schema |

The unavailable-semantics and no-panic-safety tests pass against the
pre-change behavior by construction: the old literal already carried the
same unavailable semantics, and the pre-change code had no recursive
projection that could panic. They are preservation and safety guards, not
drift detectors, and are recorded as such.

## 5. Invariant review

| Invariant | Evidence |
|---|---|
| The published dashboard and status schemas keep their existing field names and shapes | No published struct field was added, renamed, or removed; the offline fallback is built from those structs |
| A degraded or empty data source keeps producing the current degraded response | The fallback reports the server as unavailable rather than claiming a live server |
| The CLI's human-readable output wording is unchanged | `status_cli_human_output_labels_are_unchanged` |
| Credentials, prompts, raw bodies, and cache keys stay out of an operator projection | The redaction runs on both the online and the offline path |
| No new dependency, config key, or migration | `git diff --name-only` shows none |
| `rust/src/runtime.rs` adapts the CLI to operations and owns no state machine | The change is the fallback construction, the redaction helper, and the tests |

## 6. Failure and recovery review

- **Duplicate delivery** — the fallback is a pure function of the
  configuration and the provider facts; repeated invocation converges.
- **Cancellation races** — not applicable; the CLI path performs no
  cancellable work.
- **Restart** — not applicable; no new persisted state.
- **Partial persistence failure** — not applicable.
- **Malformed input** — the redaction is exercised on unusual values in
  `redaction_is_safe_on_unusual_input`.
- **Degraded data source** — the server being unreachable is the ordinary
  offline case, and it is covered end to end.

## 7. Migration and compatibility review

No schema migration, no config change, no protocol change. The CLI and the
server agree on the schema by construction rather than by agreement between
two hand-maintained lists.

## 8. Security review

- **Secret handling** — the status projection is a redaction projection, not
  a passthrough, and it runs on both the online and the offline path.
- **Known over-redaction** — a member whose name is secret-bearing, or a
  string value shaped like a credential, is replaced with `[redacted]`. A
  published field whose name happens to be secret-bearing would be redacted;
  no current published status field is.
- **Privilege bounds** — unchanged; no route or command was added.
- **DoS bounds** — the redaction is a bounded recursive walk over the
  projection; the provider count and provider id length are bounded by the
  existing constants.
- **Audit behavior** — the removed material is not logged.

## 9. Documentation and operations

- `architecture/overview.md`: a new review-index entry for the operations
  CLI surface, recording that `runtime.rs` dispatches and renders and
  `operations/` owns the local services.
- No new operator command and no operator action is required.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | The full serial workspace suite and the `--no-default-features` build were not run in this pass, by instruction to keep verification targeted at this stage | The no-default-features parity required by the source plan is unverified | Run `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` and `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings`, then the full serial suite, before the change is pushed |
| low | The redaction is applied to the status projection only; other operator surfaces are not covered by this milestone | A secret-bearing value in another operator projection is still possible | Out of scope for this milestone, which owns the status projection. A later operations-cli milestone should sweep the remaining operator surfaces |
| low | The shared build directory grew during the parallel work and the disk filled once | A build failed with `No space left on device` mid-session | `cargo clean -p eggpool` reclaimed 8.6 GiB of this package's own artifacts; dependency artifacts were left in place. Watch disk usage on the build host |

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. Milestone 002 (local
process lifecycle safety) does not depend on this milestone.

## 12. Registry updates

- `plans/registry.md`: the operations-cli milestone 001 row moves from
  `ready` to `closed`, with the closure record linked.
- `plans/subsystems/operations-cli-roadmap.md`: the milestone 001 row in the
  milestone status table moves to `closed`.
- `plans/implementation/operations-cli/001-operator-status-projection-fidelity.md`:
  the `Status` line moves from `ready` to `implemented`.
