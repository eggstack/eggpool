# On-Prem User Access M017 — Managed client credentials and integration reconciliation

Status: active

Repository baseline: `88f58bbf2e9095cf46437945edf83e580270bdb6` (`main`, PR #7 merged).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md#m017--managed-client-credential-reconciliation`

Long-term requirements: `plans/000-long-term-specification.md#1-product-definition`, `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`.

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted).

Primary class: capability

## 1. Objective

Restore a complete fresh-install and managed client-configuration path without relying on a retired shared server key. A solo developer's first inference and a small-team member's remote desktop must use valid individual credentials. This is a post-merge follow-up to M006/M007 and M015/M016; preserve those closure records.

## 2. Readiness and dependencies

Hard: none; M001–M016 code is on `main`. Interface: existing `epk1` personal tokens, secret-free `epc1` profiles, `EGGPOOL_API_KEY`, integration-profile endpoint, and `eggpool-client-config` projection are stable. Operational: run a synthetic end-to-end CLI-to-agent smoke test before closure.

## 3. Current evidence

`rust/src/runtime.rs::onboard` generates `[server].api_key` then bootstraps an admin personal token. `rust/src/operations/integrations.rs::{build_context,build_endpoint_context}` calls `config_mutation::resolve_server_key`, which manufactures a shared key when absent. `build_remote_context_from_config` rejects a keyless managed instance. `runtime.rs::getkey`, README First Request and client hints still direct operators to `eggpool getkey`. `rust/src/server/middleware.rs` rejects the shared key after local identity initialization. The M006/M007 qualification did not traverse this full post-bootstrap path.

## 4. Invariants

Provider accounts, upstream secrets and routing stay in TOML/env; human state stays in SQLite. No personal token becomes a web-admin session, no credentials in secret-free desktop profiles, no implicit shared-key fallback in managed mode. Keep legacy authentication for `legacy_unmigrated`, Chat/Responses/compact/Messages, user-filtered discovery, generation leases, single SQLite gate/WAL/NORMAL and budget/finalization ownership.

## 5. Scope

In: `onboard`, `getkey/newkey` compatibility, all `configsetup` targets, `configremote`, `eggpool-connect`, onboarding and README instructions. Out: storing recoverable personal token plaintext, putting member credentials in connection tokens, new token protocol, IAM/SSO, provider routing changes.

## 6. Production changes

Make integration credential resolution aware of access state. In managed/admin-initialized installations, require an explicit personal credential or a safe local operator-issued one-time token, rather than `resolve_server_key`. Shared-key CLI commands must clearly report the distinction between legacy credentials and personal tokens. Export of remote configuration must not depend on the legacy key's presence and must remain read-only.

## 7. Ordered work packages

### A — Freeze authentication matrix

For legacy-unmigrated, fresh-uninitialized, admin-initialized and managed states, define which commands can generate, show and use which credential. Select a non-mutating mode authority. Reject stale server-key use without silently changing the credential.

Acceptance: contract tests for every state and a zero-secret export.

### B — Reconcile adapters

Change `operations/integrations.rs` and `runtime.rs` clients; preserve `EGGPOOL_API_KEY` interpolation and existing Codex/OpenCode output shapes. Cover Claude Code and other supported generated configurations. Make `configremote` function without a global server key and keep the profile token credential-free.

Acceptance: remote desktop bootstrap, personal-token discovery, profile revision and revocation all work.

### C — Test full user journeys

Isolated HOME/TOML/SQLite/fake upstream: fresh onboard → initial personal token → configsetup → successful model discovery/inference; separate member credential → configremote → desktop helper → authorized models; revoke token and deny next request. Compare legacy pre-bootstrap flow.

Acceptance: no post-bootstrap 401 from following documented first-request steps.

## 8. Failure, cancellation, restart and contention

No export mutates TOML or regenerates the retired shared key. On missing/revoked token, fail with actionable error; no anonymous or admin fallback. A changed user policy revision invalidates discovery; concurrent token issuance cannot leak or cross-attribute a credential. No DB gate held during external client setup.

## 9. Compatibility and migration

No database migration anticipated. Preserve `epc1`, `EGGPOOL_API_KEY`, personal bearer/`x-api-key` wire, client config ownership/drift semantics and legacy installations. If changing auth authority beyond ADR-0002, stop and propose an ADR.

## 10. Tests

Extend `rust/tests/cli_contract.rs`, `rust/tests/on_prem_identity.rs`, `rust/tests/on_prem_sessions.rs`, `rust/tests/on_prem_model_policy.rs`, and client-config/connect crate contract suites. Add a synthetic cross-component CLI/inference/remote fixture. Cover keyless managed, stale configured server key, legacy key, two principals, profile filtering, token revocation and redacted stdout.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
git diff --check
```

## 12. Documentation

Update `README.md`, `docs/agent-configuration.md`, `docs/cli-reference.md`, `docs/deployment.md`, `architecture/deep-dive-integrations.md` and embedded paste hints; add a regression guard for obsolete post-bootstrap getkey instructions.

## 13. Acceptance criteria

Fresh solo onboarding produces a usable agent credential; all local/remote client configuration survives the managed transition without regenerating or requiring a legacy shared key; two members remain isolated; legacy single-key upgrades remain functional until operator cutover.

## 14. Stop conditions

Stop on cross-user secret disclosure, implicit key resurrection, broadened browser/admin privileges, or an incompatible client-profile schema change without an accepted decision.

## 15. Closure evidence

Create `plans/closure/on-prem-user-access/017-status.md` with exact implementation SHAs, a requirement-to-evidence matrix, executed test/CI evidence, negative auth/revocation cases, compatibility and docs review, severity-tagged residuals and unblock audit. No closure on unit tests alone.

## 16. Handoff

P1 integration corrective. Ensure the single-user first request is tested as a user journey, not inferred from separately passing auth and CLI tests.
