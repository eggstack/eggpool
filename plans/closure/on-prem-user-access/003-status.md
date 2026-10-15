# On-Prem User Access Milestone 003 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/003-principal-propagation-and-attribution.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m003--request-principal-and-durable-user-attribution`

Implementation commits:

- `9af8787a` — propagate user identity through inference.
- `98f90b8f` — move M003 into closure.

## 1. Executive finding

M003 gives authenticated inference requests one typed, server-resolved
principal, propagates its stable owner through finite, streaming, compact, and
semantic-selector publication, and persists attribution atomically with the
request. Duplicate proxy request IDs require matching owner facts; publication
rechecks account/session state and auth epoch before dispatch. Terminalization
checks the same owner snapshot. Legacy shared-key requests remain explicitly
unattributed. M004 is ready to build model policy on this principal contract.

M003 is conditionally closed. The complete integration target set and the
required compile/lint gates pass. The full all-targets run reaches unrelated
`operations::update` unit tests and fails there on this Darwin host. One of
those tests reproduces in isolation. M003 does not modify update code. Runtime
overhead and physical SBC resource measurements remain unqualified and are
assigned to M007; no sizing claim is made here.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Typed principal from trusted auth middleware | `identity::AuthenticatedPrincipal`; middleware resolves agent credentials and browser sessions from SQLite; legacy shared key is explicitly typed | pass | Client identity headers do not set principal fields. |
| Stable identity across finite, streaming, compact, retries, and selector dispatch | Attribution is carried through endpoint decision, finite coordinator, streaming coordinator, and semantic selection; `rust/tests/on_prem_attribution.rs`; `coordinator_c011.rs`; `codex_compaction_compat.rs` | pass | Selector publication is an internal child and retains the original user and credential. |
| Durable attribution and legacy history | Migration 0057, checksum/runtime manifest updates, `database_compatibility.rs`, and HTTP inference spoof-header fixture | pass | Existing rows remain null/unattributed; user attribution intentionally has no FK so deletion cannot erase history. |
| Cross-user replay and finalization owner equality | `coordinator_publication.rs`, `coordinator_finalization.rs`, coordinator boundary suites | pass | Mismatched duplicate attribution fails closed. |
| Revocation/auth-epoch check before publication | `on_prem_attribution.rs`, `coordinator_publication.rs` | pass | Password reset increments the epoch and revokes API credentials and browser sessions. |
| Bounded content-free user request history | `identity::recent_user_requests`; attribution test | pass | Query limit is clamped to 1–500; internal selector rows are excluded. |
| No public request-shape regression | Existing coordinator public request structures remain unchanged; coordinator boundary and compact compatibility tests | pass | Attribution uses additive private input and publication APIs. |
| Architecture and protocol documentation | Request lifecycle, database, observability, and API reference docs | pass | User IDs and credentials remain out of routine diagnostics and provider rollups. |

## 3. Migration and implementation evidence

Migration 0057 adds `users.auth_epoch` and nullable request owner,
credential/session, principal-kind, request-kind, and selector-parent fields.
The `(user_id, started_at DESC, id DESC)` index supports bounded history.
Historical request rows remain queryable with null ownership; user removal
does not cascade into request history. The embedded checksum inventory and
runtime manifest now include version 57.

Publication validates current active user, auth epoch, and credential/session
state inside its transaction. Request owner facts are persisted in that same
transaction and copied into `FinalizationIdentity`. Duplicate publication and
terminalization compare owner facts before observing or settling an existing
row. Internal selector attempts use `request_kind='internal_selector'` and a
parent proxy request ID; logical history omits them.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --tests -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_attribution -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --lib operations::update::tests::package_failure_after_mutation_restores_the_previous_exact_release -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --lib operations::update::tests::pip_transition_updates_metadata_and_runs_target_without_raw_replacement -- --test-threads=1
rtk git diff --check
```

### Results

- Formatting, strict default Clippy, no-default-features check, and strict
  no-default-features Clippy passed.
- The full integration target set passed: **953 passed, 1 ignored across 75
  suites**. Focused attribution passed 3 tests; publication passed 10;
  coordinator boundaries 6; `coordinator_c011` 18; finalization 11; compact
  compatibility 13; server transport 14.
- The full `--workspace --all-targets` command did not pass. Its library unit
  suite had 162 passed and 2 failed: `package_failure_after_mutation_restores_the_previous_exact_release`
  and `pip_transition_updates_metadata_and_runs_target_without_raw_replacement`.
  The first failed again in isolation with
  `RollbackFailed { reason: "ownership_changed_unexpectedly" }`; the second
  passed in isolation. Both belong to `operations::update`, outside M003.
- `git diff --check` passed before closure metadata edits.
- No dependency changes were made; release build, dependency audit, and
  dependency feature-tree checks were not required.
- No request latency, memory, or physical SBC benchmark was run. Existing
  M001 Argon2 and new attribution resource observations remain assigned to
  M007. This closure makes no performance or capacity claim.

## 5. Invariant and failure review

- Middleware resolves identities from stored credentials/session state;
  spoofed downstream-user headers do not affect attribution.
- Credential revocation and password reset are checked before publication.
  Already published requests retain immutable owner facts through retries and
  terminal settlement.
- DB errors fail closed; no path degrades managed inference to anonymous
  ownership. Legacy shared-key use stays separately marked and userless.
- SQLite remains the single publication authority. No database transaction
  spans provider I/O. Request bodies, prompts, bearer secrets, and cache keys
  are not added to attribution or user-history results.
- Semantic selector executions remain attributable children and do not count
  as additional logical user requests.

## 6. Residual conditions

| Severity | Condition | Impact | Required evidence |
|---|---|---|---|
| medium | Two unrelated `operations::update` library tests fail in the all-targets run on this host; one reproduces alone. | The repository-wide all-targets gate is not green in this environment. M003 integration targets and compile/lint gates are green. | Diagnose and close under the update/deployment owner before claiming a green all-targets workspace run. |
| low | Request-attribution overhead and physical target resource use are not measured. | No latency/RSS sizing assertion is supported. | M007 paired baseline and post-change measurements on named hardware. |

## 7. Roadmap disposition

M003 is conditionally closed: the principal and durable-ownership contracts
are stable, and M004 may proceed. M004 is promoted to ready. M005 and M006
remain blocked on their hard predecessors. M007 remains blocked on M006 and
must carry M001 and M003 physical qualification evidence.

The update unit-test failures are recorded as a repository gate condition;
they do not indicate an attribution failure or change M004's policy boundary.
