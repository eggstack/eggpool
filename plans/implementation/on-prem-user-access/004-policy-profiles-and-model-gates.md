# On-Prem User Access Milestone 004 — Profile-based model authorization

Status: blocked

Repository baseline: `main` at `9a0a903c9944b69a6c1ae9fa969bb8603d5c65d9` (accepted new architecture on `plans/on-prem-small-team-user-access`).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md`.

Long-term requirements:
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted).

Primary class: capability

## 1. Objective

Create reusable named local profiles and per-user overrides controlling model
access and default/recommended model selection, with authority evaluated at
the concrete-dispatch boundary. Explicit model denials and global hard limits
cannot be bypassed via provider-qualified names, virtual selectors, compact,
or protocol conversion. **Hard dependency:** accepted M003 closure establishing
stable principal propagation and persistent user identity. No model-access
browser editor or consumption budget is delivered here.

## 2. Why this milestone is blocked

M001–M003 are not closed; profile assignment must not bypass an unauthenticated
or unattributed path. Ready only after their security and lifecycle contracts
are accepted. `plans/subsystems/on-prem-user-access-roadmap.md` defines
scope and typed dependency chain.

## 3. Current implementation evidence

`rust/src/coordinator/endpoints.rs` calls `resolve_concrete` and
`resolve_virtual` before routing; `rust/src/model_router.rs` and
`rust/src/coordinator/semantic.rs` implement affinity/selector selection;
`rust/src/request/admission.rs` creates canonical request facts,
`rust/src/routing/eligibility.rs` filters upstream candidates, and
`rust/src/server/health.rs` exposes a global `/v1/models`.
`rust/src/config.rs` defines TOML provider/model overrides; it must remain
the operator policy authority and never be reinterpreted as per-user grants.

## 4. Non-regressing invariants

- This is one LAN/on-prem instance, not hosted SaaS. The first-admin SBC flow remains canonical; no mandatory external service.
- TOML/environment retain provider topology, provider credentials, outbound routing, model overrides and self-hosted LLM connection details. SQLite holds downstream users, policies, budgets, sessions and usage. Never make the web dashboard a general provider configuration editor.
- Existing Chat Completions/Responses/Messages/compact wire, streaming termination, auth-header compatibility, metadata redaction, recovery, provider scoring and health remain intact.
- Single SQLite gate/worker (WAL/NORMAL), `GenerationLease` and immutable provider topology remain authoritative. No secret/body logging, second retry/finalization owner or unbounded in-memory cache.
- A denied user cannot transform a permitted virtual model or provider qualifier into an unauthorized underlying dispatch.

## 5. In-scope production changes

### A — SQLite profile and assignment state

Add versioned, normalized `policy_profiles`,
`profile_model_rules`, `user_profile_assignments` (or one profile FK
on `users`), and `user_policy_overrides` with creation/update identity,
auditable versions and strict constraints. Profiles control model permissions
(allow/deny), a preferred default model and a reference to quota policy
to be added in M005. Start with **one assigned profile per user**; no
nested profiles, organization trees, groups or role inheritance. Admin
profile preserves historical unrestricted single-admin access unless
configured otherwise. Restricted profiles default-deny unknown models.
A user's explicit allow cannot override global hard deny or inactive
provider. Keep instance/provider configuration in TOML only.

### B — Effective policy compiler

Build a typed, bounded effective policy from instance restrictions, user
overrides and assigned profile, with deterministic precedence:
instance hard deny/ceiling always wins; explicit per-user restriction/
allowance within instance constraints; then profile; then default.
Use immutable cached policy with monotonic version/epoch and atomic
invalidation for **new** requests when admin edits or revokes privileges.
Changes must not require a process restart or TOML rehash; security
cache invalidation must not mistakenly resurrect stale permissions across
generation swap. Model identifiers use canonical IDs plus optional exact
provider-qualified rules. Avoid display-name policy keys and open-ended
wildcards unless a carefully bounded/fail-closed grammar is necessary.
Do not treat catalog absence as permission.

### C — Authorize all model choices

At ingress authorize explicitly requested model ID (including virtual
alias), then authorize every resolved *concrete* candidate before upstream
dispatch. A permitted virtual alias cannot gain access to denied concrete
models through stickiness, semantic selection, fallback/repair, or
provider-qualified syntax. Selector's own model requires permission (or an
explicit tightly scoped internal exception with attributable usage and
separate operator approval); reject rather than silently spending denied
models. Retain a single request policy snapshot so concurrent edits do not
cause inconsistent partial decisions inside one request. Native
`/v1/responses/compact` and all Chat/Responses/Messages surfaces share
identical checks; no alternate path around endpoint admission.
Return an unambiguous authorization denial without exposing secret
provider topology, credentials or other users' policies.

### D — User-specific discovery and integrations

Filter `GET /v1/models` and
`GET /api/integrations/v1/profile` to principal-allowed models and
aliases. Apply the same conservative capability/limit projection as
current `eggpool-client-config`, not a new client catalog codec.
Include the effective-policy version in profile revision/ETag to invalidate
desktop caches on permission change. A model absent from discovery must
also fail at runtime even when a client sends it manually. Clarify that
a preferred model is a client default/recommendation, never a silent
rewrite of an explicit request.

## 6. Out of scope

No token/USD budgets (M005), admin profile editor (M006), upstream credential
policies, per-user local LLM startup, nested groups, model-rewriting by a
policy engine, changes to provider health/fairness, browser auth work.

## 7. Ordered work packages

1. Append checksum-verified schema and profile repository APIs. Define
   exact precedence with pure property tests.
2. Add effective-policy caching, version invalidation and negative
   authorization tests, including disabled-user updates.
3. Insert model gates into the single endpoint/selector lifecycle, retaining
   current parsed-body preservation and generation lease.
4. Apply filtered model discovery/integration profile revision, then
   add CLI-only operator profile fixture/setup path until M006 adds the UI.

## 8. Failure, cancellation, restart and contention

DB/policy compilation failure fails closed for restricted users. Admin
policy edit is atomic: no partial profile visibility; new requests observe
the new policy version once committed. Already-admitted requests retain the
captured version and may complete; document this and ensure a disabled user
cannot start any new work. Selector/retry paths never shed their version.
No SQLite transaction may span a model inference, selector call or stream.
Bound cache count by known local users and active generations; invalidate
on delete/revoke and avoid resurrecting stale grants after restart.

## 9. Compatibility and migration

No provider TOML rewrite, no upstream secret move, no public model rename,
no old dashboard analytics regression. Preserve the historical default
single-owner all-model access with explicit admin/default policy.
Historical user records gain one default profile through a deterministic
migration. Expose limited context and capability metadata only after
authorization, including `model/provider` and aliases.

## 10. Required tests

Add `rust/tests/on_prem_model_policy.rs` with allowed/denied concrete,
provider-qualified, virtual aliases, semantic selector/repair, sticky
affinity, retry failover, native compact, Messages/Chat/Responses,
model-list/ETag projection and concurrent update/revoke cases. Extend
`rust/tests/model_router.rs`, `routing_domain.rs`,
`codex_responses_compat.rs`, `codex_compaction_compat.rs`,
`operations_o005.rs` and `server_transport.rs`.
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_model_policy -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test model_router -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
```
## 11. Required verification commands

Run the following after focused tests; preserve exact results for closure:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
git diff --check
```
If new dependencies/feature flags: `cargo deny --manifest-path rust/Cargo.toml check`,
`cargo tree --manifest-path rust/Cargo.toml -e features`,
`cargo build --manifest-path rust/Cargo.toml --locked --release`.
No test may be declared passed unless actually run.

## 12. Documentation

Update `architecture/deep-dive-routing.md`,
`architecture/deep-dive-request-lifecycle.md`,
`architecture/deep-dive-integrations.md`,
`docs/model-routing.md`, `docs/agent-configuration.md` and
`docs/api-reference.md` with exact authorization/discovery semantics.

## 13. Acceptance criteria

A restricted member cannot dispatch, discover, or request a forbidden
concrete model through **any** supported API, qualifier, alias, selector
or retry, while an authorized admin retains normal routing and data.
Profile edits apply to new requests without restart and test fixtures prove
immutable request policy ownership and redacted errors.

## 14. Stop conditions

Stop if public request/wire contracts would break, model resolution occurs
on an unguarded path, or a policy update would bypass versioning/fencing.
Do not implement budgets or UI to conceal missing enforcement.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/004-status.md`: commit IDs,
spec-to-test matrix, all alternate-model bypass negatives, concurrency and
restart observations, discovery/ETag fixtures, migrations and commands
actually run, known residual risks and registry unblock audit.

## 16. Handoff

One implementation agent owns the policy/compiler and admission seam.
Keep provider-account health/quotas entirely separate; no generic SaaS
tenant abstraction. Confirm current branch/source before modification.
