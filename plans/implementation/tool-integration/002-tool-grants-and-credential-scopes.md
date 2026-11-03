# Tool Integration M002 — Effective tool grants and credential scopes

Status: active

Repository baseline: `0e021c1ffe55803aff5f325033e65313dca9f1dc` (planning, 2026-11-03)

Source roadmap: plans/subsystems/tool-integration-roadmap.md (M002)

Long-term requirements: plans/000-long-term-specification.md §§1–3 and `6; plans/001-terminology-and-domain-model.md; plans/002-long-term-roadmap.md `5; plans/003-planning-process.md

Applicable ADR: plans/adrs/ADR-0007-optional-tool-authority-and-hosted-execution-boundary.md (accepted)

Primary class: infrastructure

## 1. Objective

Add a durable, default-deny per-user/per-profile tool permission and personal-token scope contract that can safely authorize a future MCP gateway. Produce an internal effective authorization function plus administrator CLI to grant/revoke known tools. No tool invocation HTTP endpoint yet.

## 2. Readiness/dependencies

**Hard blockers:** M001 backend configuration/identity closed and ADR-0007 accepted. **Interface:** current effective model profile/version and principal revocation machinery, not new auth framework. This plan must be promoted to ready in the registry only after those conditions are satisfied; writing it is not permission to begin implementation.

## 3. Current implementation evidence

rust/src/identity.rs defines AuthenticatedPrincipal and credential scopes. rust/src/db/, rust/src/model_policy.rs, rust/src/server/dashboard/ and rust/src/server/auth.rs implement identity, model profiles/overrides, browser-session administration, and revocation. docs/api-reference.md describes per-user model discovery and hard-versus-estimated budgets. Existing migration chain and backup/restore must be inspected before choosing table design. Do not assume current scopes already include tools or that admin browser sessions are equivalent to personal MCP tokens.

## 4. Invariants

Inference-only tokens must never grant tools. Disabled/revoked credentials deny immediately for new operations. Effective permissions are evaluated against a canonical backend identity and exact tool name after validation; filtering discovery alone is never sufficient. Tool policy is separate from model policy and never changes provider eligibility or budgets. Operator config (backend endpoints/credentials) must not become user-editable. Single SQLite gate and existing idempotent migration/recovery rules remain.

## 5. Scope

**In:** additive migrations for tool grants and token scopes, exact backend/tool identity representation and versioned policy, effective grants by admin/profile/user overrides, audited operator mutations, safe read-only self-service list of effective tools (or CLI projection), internal authorize_tool(principal, backend, name, action) result. Default deny even for old broad inference scopes. Ensure scopes for discovery and invocation are independently meaningful.

**Out:** network MCP service, web UI unless needed to avoid inaccessible administration, shell or filesystem tools, tool argument/result storage, tool request budgets/quotas beyond finite per-call ceilings (M003/M004), changing LLM model policy, public signup, centralized enterprise IAM.

## 6. Required production changes

Extend the existing managed-account policy schema with a minimal tool permission model. Prefer known-backend/exact-tool allowlist entries, with stable tool identifiers and effective policy revision included in cache invalidation; prevent wildcard escalation, case/normalization ambiguity, and ghost grants after backend replacement. Reuse principal identity and immutable policy snapshots for admitted requests. Introduce scoped credential issuance explicitly requested by an administrator; don't silently widen existing tokens. CLI mutation and read operations should use existing user/admin auth and audit conventions. Migrations must assign empty effective grants to all legacy and managed users and preserve existing token hash/revocation formats.

Address whether tool scope is stored per credential or fixed in a credential-policy binding. Implement schema/SQL as a single gate and do not create another caching authority. If tool access is changed while a call is being dispatched, define a small atomic authorization/admission boundary and what happens to already-admitted work; new requests must observe revocation. A disabled user never gets a catalog. No backend configured → grants inert and invisible at tool gateway.

## 7. Ordered work packages

A. **Policy shape.** Define canonical tool identity, grant action and resolution precedence (instance explicit deny > user override > assigned profile > safe default deny). Reject malformed/unknown backend and tool names at mutation; stale profile version conflicts must be detected.

B. **Storage and credential lifecycle.** Add the smallest compatible migration(s), recovery/backups and explicit scopes; include safe admin audit events without arguments/queries.

C. **Operator administration.** Add a bounded CLI command tree under existing `eggpool users` semantics and a private policy summary; preserve existing user creation, profile preference, token issuance and migration UX.

D. **Authorization API and tests.** Construct an immutable principal+policy decision that M003 can consume; attach a policy fingerprint and explain in docs which changes affect active vs new calls.

## 8. Failure/cancellation/restart/contention

Mutations are transactional, race-safe and fail closed. Simultaneous grant and revoke must produce a serializable effective view; a cached policy may not grant after a revocation has committed for newly admitted operations. Database unavailable → no authorization. No new per-request SQLite connection and no unbounded grant cache. If the credential is revoked during a tool call, new calls fail; aborting admitted in-flight work is a separate explicit policy choice and must not be silently promised.

## 9. Compatibility/migration

Preexisting inference-only API tokens and login sessions continue working for inference. No old credential gains tool invocation. New migrations preserve schema-54→current upgrade, identity restore, and serial DB startup, with no content storage. Avoid requiring a user to configure a profile for solo inference.

## 10. Required tests

Add rust/tests/tool_grants.rs or an equivalently focused target. Extend rust/tests/on_prem_identity.rs, on_prem_model_policy.rs, on_prem_sessions.rs, database_compatibility.rs and any budget tests affected. Test 2 user/2 profile isolation, old-token denial, disabled user, unknown/renamed backend, grant+revoke race, stale admin form/version, restore/migration, credential scope filtering, wildcard/Unicode collisions, lost DB and auditable content-free mutation.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Run `cargo test --manifest-path rust/Cargo.toml --test tool_grants -- --test-threads=1`, the focused on-prem identity/policy suites, and migration/backup restore fixtures.

## 12. Documentation

Update docs/api-reference.md (internal/future scope caveat), docs/cli-reference.md, docs/firewall.md if auth flow changes, architecture/overview.md and the identity authorization deep dive. Avoid claiming a working public tool gateway before M003.

## 13. Acceptance criteria

Default policy is empty for all users, including admins and existing broad-scope tokens; scoped permissions are mutable only through authorized administrator controls; correct decisions survive restart and backup/restore; every expected denial has machine-readable safe errors; no changes to model authorization or inference behavior.

## 14. Stop conditions

Stop on ADR-0007 not accepted, M001 incomplete, any inability to make revocation authoritative on new calls, any schema design that stores endpoint secrets under end-user data, or authorization requiring a second DB writer. Do not introduce permissive wildcard semantics as a shortcut.

## 15. Closure evidence

Migration forward/backward compatibility, two-user authorization matrix, contention/revocation race logs free of content, effective policy and audit tests, exact commit/test/CI results and dependency promotion in plans/registry.md. Only M003 becomes ready after M002 closes and its protocol interface is qualified.

## 16. Handoff notes

Coordinate with the on-prem user-access auth owner without reopening its closed milestones; use additive plan/closure evidence. M002 is infrastructure and not a public tool capability by itself.
