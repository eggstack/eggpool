# On-Prem User Access Milestone 004 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/004-policy-profiles-and-model-gates.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m004--profile-based-model-policy`

Repository baseline reviewed: `1a4ae78` (M004 activated after M003 closure).

Implementation commits:

- `f537df4` — add per-user model policy profiles and enforce model gates.
- `c53bf4d` — test concurrent policy edit and snapshot refresh.

## 1. Executive finding

M004 delivers persisted, reusable model-policy profiles, exact model rules,
per-user overrides, instance-wide denials, preferred-model metadata, live
policy revisioning, concrete-dispatch checks, and principal-filtered model
discovery. The same coordinator admission path covers finite, streaming,
compact, protocol aliases and selector resolution. A request keeps its
immutable authorization snapshot after admission; new requests see a committed
policy or user-state change without a process restart. M004 is closed and M005
is ready.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Persist profiles, exact rules, assignments, overrides, denials and revision | Migration 0058; migration checksum and runtime manifest; `database_compatibility.rs`, `build_manifest.rs` | pass | Tables and indexes are append-only; mutation and user-state triggers advance the revision. |
| Apply deterministic precedence and reject broad model keys | `model_policy::ModelPolicySnapshot`; `on_prem_model_policy.rs` | pass | Instance denial wins, then user override, profile rule and safe role default; model keys are exact and provider-qualified syntax is tested. |
| Authorize requested and concrete models before dispatch | `coordinator/endpoints.rs`; `on_prem_attribution.rs`; `server_transport.rs` | pass | The shared endpoint path validates requested model, selector model and resolved concrete candidate; denied requests return 403. |
| Preserve immutable admitted policy and refresh new requests | policy snapshot immutability, disabled-user invalidation, revision refresh and concurrent edit/read fixtures in `on_prem_model_policy.rs` | pass | Database transactions serialize mutations; snapshots remain fixed while later requests observe the newer revision. |
| Filter model list and integration profile, including aliases | `server/health.rs`; `on_prem_attribution.rs`; `dashboard_filters.rs`; `status_command.rs` | pass | Denied model and ineligible alias disappear; policy revision is included in the integration ETag. |
| Provide local setup surface and safe preferred model validation | `eggpool users policy` commands; `cli_contract.rs`; policy service tests | pass | Actor must be an active admin; preferred model must currently be allowed. Preference is stored metadata, not an implicit rewrite of explicit requests. |
| Keep provider configuration and quotas separate | `model_policy.rs`, migration 0058 and architecture/database documentation | pass | No upstream credential, account routing or provider quota ownership moved into user policy. |

## 3. Production implementation evidence

Migration 0058 adds `policy_state`, `policy_profiles`,
`profile_model_rules`, `user_profile_assignments`,
`user_policy_overrides`, and `instance_model_denials`. The checksum inventory
has 58 files; the migration checksum is
`dc98951394abed29a02f48d75d2b72554da74d98de49563fa87c2548dd757fe2`.

`rust/src/model_policy.rs` owns the typed policy snapshot and storage service.
Its bounded cache is keyed by user and checked against the persisted monotonic
revision. Mutations are transactional, user state/role/auth-epoch triggers
invalidate snapshots, and an admitted snapshot is immutable. Exact instance
denials cannot be overridden. Members with no profile default-deny; an
unassigned admin retains the single-owner allow behavior.

`InferenceState` receives the policy service in production. The coordinator
checks requested, selector and resolved concrete model IDs before dispatch.
The health projection filters `/v1/models` and the integration profile by the
authenticated principal, omits virtual aliases with no allowed route, and
adds the policy revision to the integration ETag. Local CLI commands support
profile creation, exact rule changes, assignment and preferred model setup.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --workspace --tests -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_model_policy -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test on_prem_attribution -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test build_manifest -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test dashboard_filters -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test model_router -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
rtk git diff --check
```

### Results

- Formatting, strict default Clippy, no-default check and strict no-default
  Clippy passed.
- The initial full integration run passed **956 tests, 1 ignored across 76
  suites**. After adding the concurrent policy fixture, the full
  `--workspace --all-targets` run passed **957 tests, 1 ignored across 76
  suites** in 117.94 seconds.
- Focused policy tests passed 3; attribution 3; build manifest 4; database
  compatibility 9; CLI contract 5; status command fixture 1; server transport
  14; dashboard filters 2; model router 4; compact compatibility 13; and
  operations O005 14.
- `git diff --check` passed. No dependencies or Cargo features changed, so
  dependency audit and release build were not required.

## 5. Invariant review

- Provider topology, credentials, account selection and provider quotas remain
  operator-owned and separate from downstream user policy.
- Authorization precedes upstream dispatch and is repeated for the resolved
  concrete candidate; selector aliases and provider-qualified IDs do not
  bypass it.
- Policy snapshots are immutable per admitted request. New requests check the
  current durable revision and disabled-user state.
- SQLite remains one connection/gate/worker. No transaction spans provider
  I/O; policy storage and cache are bounded.
- Errors and discovery expose no credentials or other users' policy details.

## 6. Failure and recovery review

Policy reads and mutation errors fail closed. Profile/rule/assignment changes
commit atomically; revision triggers cover direct user state, role and
auth-epoch edits as well as policy table mutations. A stale admitted request
continues with its captured snapshot; later requests observe the new revision.
Service restart begins with an empty cache and loads current durable state.
There is no separate selector, retry or compact authorization implementation
that can silently omit the shared coordinator gate.

## 7. Migration and compatibility review

Migration 0058 is append-only and checksum-validated. Existing schema history
and upstream `accounts` are unchanged. Existing databases receive empty policy
tables and preserve unassigned-admin access; unassigned members default-deny.
The migration and manifest counts are 58. No public inference request shape or
provider TOML format changed.

## 8. Security review

The CLI policy mutation commands require an active administrator actor.
Exact model identifiers reject wildcards and noncanonical whitespace. A user
override cannot clear an instance denial. Disabled users fail policy snapshot
resolution. Model discovery is a convenience projection only; runtime checks
remain authoritative. Policy revision is not a credential or secret and is
the only new cache invalidation fact exposed in the ETag.

## 9. Documentation and operations

Updated the database, routing, request lifecycle and integration deep dives,
plus model routing, agent configuration, API and CLI reference docs. These
describe role defaults, exact policy precedence, no-restart invalidation,
filtered catalogs and preferred-model behavior.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Physical SBC resource/performance measurements for the full user-access line are not available in this M004 environment. | No physical target capacity or latency claim is made. | M007 owns target hardware qualification and carries the earlier M001/M003 measurement conditions. |

No M004 correctness or security findings remain open.

## 11. Roadmap disposition

M004 is closed. Its profile, policy revision and concrete-model authorization
contracts satisfy the hard predecessor for M005. Physical footprint evidence
remains assigned to M007; it does not block budget implementation.

## 12. Registry updates

The on-prem user-access M004 row moves to recently closed. M005 is promoted
from blocked to dependency-ready because M004's policy authorization is now
closed. M006 remains blocked on M005; M007 remains blocked on M006 and
operational SBC/ordinary-host evidence. The audit found no other registered
plan newly eligible under this dependency chain.
