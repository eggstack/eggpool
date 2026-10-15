# On-Prem User Access M015 — Final on-prem authentication and authorization threat review

Status: ready

Repository baseline: `3c7def1e35871209e68563b2ee88f36033aff76e` on `plans/on-prem-small-team-user-access` (PR #7, open; 134-file implementation, schema 62).

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m015--independent-authentication-and-authorization-threat-review`

Long-term requirements:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted; no new ADR needed unless an auth or lease boundary must change).

Primary class: invariant

## 1. Objective

Perform a targeted adversarial security review of the on-prem identity,
session, model policy, provider routing and budget boundaries newly
introduced by PR #7; strengthen tests or issue **separate bounded
correctives** for actual defects. This is a release-gating review
with explicit negative evidence, not a claim that green integration
tests constitute a complete security audit.

## 2. Why this milestone is ready

M001–M013 implementation and additive closure evidence exist at
`3c7def1e35871209e68563b2ee88f36033aff76e`. There is no hard dependency on M014's unrelated
TLS dependency patch for the **review** itself. Run this work in
parallel with M014. M016 final merge-readiness requires both their
closure records. An independent reviewer should inspect the
actual diff, not merely accept earlier closure prose.

## 3. Current implementation evidence and blind spots

PR #7 introduces identity/session tables (migrations 0055–0057),
`rust/src/identity.rs`, `web_session.rs`, `model_policy.rs`,
`user_budget.rs`, `instance_budget.rs`; ingress
`rust/src/server/middleware.rs`, `auth.rs`, coordinator
`endpoints.rs`, `publication.rs`, `finalization.rs`,
`reconciliation.rs`, and `dashboard/admin.rs`. M008/M009 hard/
estimated rules, M010 phantom-hold crash fix, M011 sign-in/CSRF
fix, M012 physical testing and M013 transcode-bridge fix have
separate closure records. However, those tests are largely
implementation-owned and there has not yet been an **independent
diff-wide, adversarial security acceptance** of the entire
134-file on-prem feature integration. Earlier browser tests found
an unusable sign-in flow; admission changes broke Messages
transcode despite green Rust tests. These show why a coherent
boundary-level negative review remains useful after milestone
closure.

## 4. Invariants that must not regress

- EggPool remains a **single on-prem instance** for an owner or small team,
  with provider topology, upstream credentials, TLS trust, private LLM
  endpoints and routing configuration controlled by operator TOML/environment;
  user identities, profiles, sessions, audit and downstream quotas stay in
  SQLite. No SaaS, org/tenant control plane, embedded reverse proxy, external
  IAM, secret-bearing HTML or browser provider editor.
- Preserve generation leases, provider health/eligibility, request identity,
  retry and streaming finalization, single SQLite gate/worker and WAL/NORMAL,
  live/restart config authority, all supported Chat/Responses/compact/Messages
  client and cross-protocol routes, and the canonical solo SBC installation.
- Authorization and budget failure must not become provider unhealthiness
  or anonymous fallback. No unauthenticated web mutation, shared
  `admin/admin` credential, bypass of profile/global/model denies, or
  moving operator secrets to SQLite.
- All qualification uses fake provider tokens, temporary roots and redacted
  artifacts; never production data or real user passwords.

## 5. Scope

### In scope

- External attack surface, route/method authorization and browser
  provenance/transport trust on direct LAN and trusted proxy.
- User lifecycle, initial bootstrap, legacy-key cutover, personal
  credential/scope, sessions, last-admin invariant, reset/revoke.
- Requested/virtual/concrete/qualified/selector model authorization,
  metadata discovery/profile and cross-surface transcode.
- Atomic hard-vs-estimated user and global budget admission,
  retry/selector cost holds, crash/reconcile/restore and account removal.
- Cross-user read isolation, audit/redaction and resource abuse
  on constrained hardware; explicit privacy posture for local models.
- Minimal code/test changes for proven gaps; severity rubric and
  explicit closure recommendation.

### Explicitly out of scope

No hosted SaaS, SSO, IAM enterprise RBAC, frontend rewrite, TLS dependency
upgrade (M014), provider TOML web editor, unmeasured Pi MMC tuning,
storage architecture redesign, or unlimited exploratory pen-testing.
No claim of formal certification or absolute absence of vulnerabilities.

## 6. Required production/test changes

A. Construct a full **route-method-credential matrix** from
`server/mod.rs`, `middleware.rs` and dashboard/API routers,
covering anonymous, legacy shared token, admin browser, member browser,
admin/member agent tokens, disabled/revoked user, invalid CSRF, and
path/method variants including normalized/encoded paths. Add negative
tests in `rust/tests/server_transport.rs`,
`on_prem_sessions.rs`, `on_prem_admin_ui.rs` for any uncovered
privilege surface. Unknown `/api/*` must never inherit public
dashboard access.

B. Threat-model the boundary of `trusted_proxy_ips` and
`x-forwarded-proto`, duplicate `Host`/`Origin`/`Referer`
headers, real browser same-origin submissions, Secure/SameSite/
HttpOnly cookies, login throttling and Argon2 worker memory cap
(19 MiB/two workers). Test HTTP LAN password rejection by default,
loopback allowed posture, and spoofed/proxied TLS acceptance.
Do not conflate LAN with authenticated confidentiality.

C. Verify stable `user_id` and credential fencing across
provider account selection, fallback, alias, semantic selector,
compact/Responses/Messages/transcode, configuration reload,
revocation, password reset, request-ID collision and finalization.
A user must not obtain another member's usage/model list or
ever force a forbidden concrete model through routing. Include
`runtime-status`, `/v1/models`, `/api/user-budget`,
`/api/integrations/v1/profile` and admin password reset/token
reveal/usage/audit.

D. Independently trace **one logical request** through all
budget scopes on concurrent last-slot, multi-user retry,
selector child, hard USD tariff provenance, estimated-mode
fallback, output-token bound mismatch, cancellation, process
death and restart. Assert no provider submission on unprovable
strict rule, no false hard cost from unknown self-hosted price,
no stranded budget hold, and no policy metadata leaks in 429/403.
The M010 recovery fix should fail under a deliberately reverted
two-line change and pass with it restored, if safe and supported
by the test harness; otherwise document the proof limitation.

E. Secret and abuse review: password/token plaintext lifetime,
one-time display, database hashed storage, audit/session backups,
query-parameter and HTML/cache leakage, `Debug` logs, browser
snapshots, request body admission before password validation,
token selector brute-force, request/login rate limiting and
SQLite gate amplification. Non-redacted immutable artifacts are
a blocking finding.

## 7. Ordered work packages

1. Freeze PR diff/commit, inventory every HTTP endpoint and
   principal/owner authority, enumerate high-value threat cases.
2. Run focused adversarial negative tests; record gaps with
   severity and direct reproduction, not speculative assertions.
3. Fix only bounded in-scope defects with code+regression test; if
   root cause crosses auth ADR or persistent trust boundary, stop
   and write a new architecture/corrective plan.
4. Produce independent threat/evidence review, verify default/
   no-default and full tooling gates (Q007 as standard), preserve
   historical closures and promote release integration M016 only
   when both M014 and M015 are accepted.

## 8. Failure, cancellation, restart and contention semantics

Evaluate credential/user disable between middleware resolution and
publication; a **new** request must not start with stale privileges.
Already admitted streams may complete under their immutable policy
snapshot, with budget ownership retained. A contested last global
slot cannot admit two members. Forced process exit during publish
must recover without freeing actually incurred upstream spend.
Failed auth/database read rejects rather than falling back to
legacy/loopback. Hash throttling and session lookup cannot hold
the SQLite gate while Argon2 runs or create unbounded attacker-
controlled memory.

## 9. Compatibility and migration

No new auth contract is assumed; preserve historical one-user
`server.api_key` upgrade behavior only in explicit legacy state,
personal `EGGPOOL_API_KEY` tokens, browser-only sessions,
and source-owned provider TOML. If a security fix changes default
network posture, legacy key rights, CSRF behavior, token privileges
or API error shapes, stop for an ADR or scoped compatibility
decision before implementation. Schema changes require append-only
checksum-backed migration (>62) and an independent restored-schema
compatibility test; absent a finding, no migration is expected.

## 10. Required tests

Cross protocol/credential negatives:
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_attribution -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_model_policy -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_global_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
uv run pytest tests/tooling/test_qualification_live_provider.py -q
```
Review M011 real-browser artifact/manifest; re-run browser gate if
mutated HTML/session middleware changes, not merely DOM assertions.
Run at least one synthetic nonloopback reverse-proxy scenario and
a complete direct-local no-proxy scenario. Cover failed credential
brute-force and duplicate provenance headers with bounded synthetic
inputs.

## 11. Required verification commands

Record commands/results **actually executed** at the implementation SHA:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv sync --frozen
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```
Use `cargo deny check` with the repository's root `deny.toml`
and verify the command/working directory that the GitHub audit workflow
actually invokes. Run `cargo build --manifest-path rust/Cargo.toml --locked
--release` for production-affecting Cargo changes. The hosted CI,
installer-shell and dependency-audit jobs must be green on the final
head SHA; no earlier green check is a substitute.

## 12. Documentation updates

New security review closure, `architecture/deep-dive-security.md`
and `docs/deployment.md` only for actual changed behavior or threat
model gaps; release docs validator if touched. Preserve public claims
about Pi 5 storage tails from M012 and no remote access support.

## 13. Acceptance criteria

A reproducible route/principal matrix covers all existing public
and private entry points and no cross-role, cross-user, cross-model
or budget bypass remains. The negative/failure corpus demonstrates
managed-mode key separation, CSRF and trusted proxy checks,
authorization at concrete dispatch and stable user-budget settlement.
No high/critical findings, and no unaccepted medium **security
correctness** findings. Every defect is either fixed with regression
tests or has a **separate blocked merge** disposition; negative
coverage is independent of the implementation's previously chosen
happy paths. Full Rust/tooling gates green on the review head.

## 14. Stop conditions

Stop when an actual violation would require a new authorization ADR,
provider credential ownership shift, unsafely permissive default,
cross-transaction write, or architectural rewrite. Mark M015
`corrective pass required` and open the narrow follow-up rather
than glossing over an unresolved security finding.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/015-status.md`: source SHA,
reviewer-boundary matrix, threat assumptions, attack input/test
identity, reproducible fail/pass fixtures, source edits, exact serial
commands, local vs hosted CI results, leaked-secret scan,
severity-ranked open risks and M016 dependency promotion audit.
Do not back-edit M001–M013 closure records.

## 16. Handoff notes

This review may run **in parallel** with the M014 TLS patch.
Do not merge PR #7 based solely on green integration tests;
M016 owns the final SHA/merge gate once security and audit close.
