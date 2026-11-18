# Security Hardening Roadmap

Status: active

Long-term references:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  items 8 (secret-free diagnostics), 11 (single-instance local authority), and
  12 (identity-bound policy and accounting)
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/001-terminology-and-domain-model.md` — principal, credential,
  session, and account terminology
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Related ADRs:

- `plans/adrs/ADR-0001-binary-first-quick-install-authority.md` — installer
  dependency and verification authority; M006 constrains itself to this
  decision
- `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`
  — session cookies, CSRF, and sign-in throttling stay unchanged
- `plans/adrs/ADR-0010-personal-upstream-account-custody.md` — personal
  upstream secrets and bindings stay unchanged
- `plans/adrs/ADR-0011-configuration-secret-wrapper-rust-api.md` — M009's
  user-approved source-level Rust config field type change; TOML and runtime
  contracts stay unchanged

M006 still requires a signature-authority ADR before implementation. M008
requires an ADR only if vendoring or a no-SSH release policy is chosen.

## 1. Purpose and ownership boundary

This roadmap owns the remediation of the repository security review performed
at baseline `dcbd820e9b978d47bd43d237eded1f488d3449ee`, and the guards that
keep each correction from regressing.

It owns:

- the request-boundary credential-header policy in
  `rust/src/server/inference.rs` and `rust/src/coordinator/attempt.rs`;
- the truthful `[security]`/`[server]` configuration contract in
  `rust/src/config.rs`, its examples, and its operator documentation;
- the authentication/authorization observation record in
  `rust/src/server/middleware.rs`, `rust/src/server/auth.rs`,
  `rust/src/web_session.rs`, `rust/src/identity.rs`, and the admin audit
  surface;
- the scope contract attached to `AuthenticatedPrincipal` in
  `rust/src/identity.rs`;
- the workflow hygiene rule for `.github/workflows/`;
- the service-unit hardening surface in `deploy/eggpool.service` and the
  renderer in `rust/src/operations/deploy.rs`;
- baseline HTML response headers in `rust/src/server/dashboard/response.rs`
  and `rust/src/server/auth.rs`;
- the dependency-advisory disposition for the Eggress/`russh` stack in
  `rust/Cargo.toml`, `rust/Cargo.lock`, and `deny.toml`.

It consumes, but does not own:

- session issuance, CSRF delivery, and sign-in throttling (ADR-0004 and the
  On-prem user access roadmap);
- personal-upstream custody and routing isolation (ADR-0010 and the On-prem
  member access roadmap);
- provider transport topology and Eggress pinning policy (Provider transport
  roadmap);
- release/installer ownership classes and publication (Deployment and
  Packaging roadmap and ADR-0001);
- dashboard content, projection, and presentation (Dashboard roadmap).

Completed milestones in those roadmaps are not reopened. A milestone here may
change an owned surface, but it must preserve every cited invariant and must
not silently revise another roadmap's contract.

## 2. Work classification

### Invariants

- No inbound client credential or browser-state header reaches a model
  provider.
- No configuration key silently claims an unenforced security control.
- A stored credential scope governs the endpoint that credential may use.
- The dependency advisory gate passes without a new advisory exception.
- Every HTML response carries the baseline response-header set.
- The production service unit is never weaker than its reviewed hardening set.
- No security event record contains a credential, token, prompt, request
  body, raw IP address, or username.

### Capabilities

- An administrator can read a bounded, durable record of authentication and
  authorization outcomes.
- A release artifact can be verified independently of the HTTPS origin that
  served it (blocked on the signature-authority ADR).

### Infrastructure

- A bounded, non-blocking security-event writer on the existing task
  supervisor.
- A workflow-hygiene guard wired into CI.
- A shared response-header helper for every HTML-producing path.

### Polish

- Secret memory hygiene for configuration-owned credentials.
- A vulnerability-disclosure policy and corrected operator guidance.

## 3. Non-goals

- Changing authentication, session, CSRF, throttling, or personal-upstream
  semantics. Those require their own ADR and roadmap.
- Implementing a general CORS layer, a Host allowlist, or a browser-facing
  security capability that EggPool does not currently provide. M002 makes the
  absence explicit instead of inventing the feature.
- A strict `script-src` Content-Security-Policy. The dashboard uses inline
  scripts and inline event handlers; a script policy needs nonce plumbing and
  is deferred beyond M007.
- Zeroizing operating-system environment variables, CLI arguments, request
  bodies, database rows, or every transient `HeaderValue` copy.
- Penetration testing, fuzzing campaigns, or external network scanning.
- Dependency upgrades unrelated to the audited advisories.
- Reopening a closed milestone in another subsystem.

## 4. Current state

At the baseline:

- `filtered_incoming_headers` blocks ten headers but not `cookie`
  (`rust/src/server/inference.rs`); `add_forwarded_headers` has the same gap
  (`rust/src/coordinator/attempt.rs`). The response path strips `set-cookie`
  and `cookie` (`rust/src/coordinator/finite.rs`), so the omission is
  request-only.
- `SecurityConfig` parses `allowed_hosts`, `cors_origins`, `redact_headers`,
  and `persist_redacted_error_detail`; `ServerConfig` parses `access_log`.
  Only `trusted_proxies` and `allow_insecure_web_auth` are consumed by the
  runtime. `rust/src/config_reload_policy.rs` still classifies the inert keys
  as restart-required, and `architecture/deep-dive-security.md` describes
  `redact_headers` as if it drove log redaction.
- Authentication failures are process-local counters only
  (`rust/src/web_session.rs`). `user_admin_audit` and
  `user_budget_rule_audit` record administrative mutations, not sign-in,
  token, or authorization outcomes, and `server.access_log` is inert.
- `AuthenticatedPrincipal.scopes` is populated from
  `user_credentials.scopes` (`rust/src/identity.rs`) but no request path
  reads it. Every issuance path currently writes
  `inference,discovery,integrations`.
- `ci.yml` pins actions by moving tag and has no top-level `permissions:`;
  `dependency-audit.yml` and two qualification workflows also use tag pins.
  The release and public-release workflows are already SHA-pinned and carry
  explicit permissions.
- `operations/update.rs` verifies a SHA-256 digest resolved from the GitHub
  release API; `scripts/install.sh` verifies a checksum sidecar. Neither
  verifies an independent signature.
- `deploy/eggpool.service` hardening omits a capability set, `UMask`,
  `PrivateDevices`, `ProtectClock`, `ProtectHostname`, and `ProtectProc`; the
  unit rendered by `rust/src/operations/deploy.rs` differs from the tracked
  unit.
- Dashboard and sign-in HTML responses set `Cache-Control: no-store` on
  authenticated paths but not `X-Content-Type-Options`, `Referrer-Policy`, or
  a framing policy. Only the one-time token page adds two of them.
- `cargo deny check advisories` exits non-zero on RUSTSEC-2026-0492 and
  RUSTSEC-2026-0495 (`russh 0.62.7`) and on the documented RUSTSEC-2023-0071
  `rsa` exception; `eggress-transport-ssh 1.0.12` requires `russh ^0.62.6`.
- Configuration-owned API keys are plain `String` values with redacted
  `Debug` implementations; no zeroization wrapper exists.

## 5. Target architecture

- One credential-header exclusion set is applied on both the server pre-filter
  and the attempt-forwarding filter, covered by an observed-upstream test for
  finite, streaming, and compact paths.
- Security configuration either has a consumer or reports itself as reserved
  at startup; no documentation claims an unenforced control.
- One bounded security-event writer aggregates authentication and
  authorization outcomes into a durable, content-free table and an
  administrator-only view. The writer never blocks an auth decision and never
  stores a secret, IP, prompt, body, or username.
- The stored scope contract is enforced for agent tokens, with legacy shared
  keys and browser sessions explicitly outside the new gate and unchanged.
- Every workflow action is SHA-pinned and every workflow declares top-level
  permissions; a guard fails on reintroduction.
- Release artifacts carry an independent signature whose authority and key
  custody are defined by an accepted ADR (M006).
- The tracked and rendered service units share one reviewed hardening set,
  enforced by a comparison guard; every HTML response passes through one
  header helper.
- The advisory gate is green with no new exception, or the affected feature is
  removed by an explicit policy decision (M008).
- Configuration-owned secrets are zeroized on drop behind a redacted wrapper.

## 6. Dependency graph

- M001 — no dependencies.
- M002 — no dependencies; interface with the configuration contract and the
  examples. Must not change reload/restart classification.
- M003 — no hard dependencies; soft on M004 for the `scope_denied` event
  vocabulary. Introduces one migration.
- M004 — interface on the scope vocabulary already persisted by
  `rust/src/identity.rs`; soft on M003 for denial evidence.
- M005 — no dependencies; interface on the existing
  `scripts/validate_release_workflow.py` guard style.
- M006 — hard on an accepted signature-authority ADR; operational on signing
  key custody and release-environment configuration.
- M007 — no dependencies; interface on the tracked unit and the existing
  response helpers.
- M008 — hard on an Eggress release that permits `russh >=0.65`, or on an
  accepted ADR for vendoring or for a no-SSH release policy.
- M009 — interface on a direct `zeroize` dependency already present in the
  resolved graph; no hard dependency.

## 7. Milestones

### Milestone 1 — Provider request credential-header exclusion

Class: invariant

Objective: stop forwarding inbound `Cookie`/`Cookie2` headers to any provider.

Dependencies: none.

Deliverable boundary: the two header filters, their tests, and the one
architecture sentence that counts the blocked headers.

User or operator value: a browser or cookie-jar client can no longer leak an
EggPool session or CSRF cookie to a third-party provider.

Exit conditions: finite, streaming, and compact observed-upstream tests show
no cookie header; the existing authorization, host, content-length, and
route-session exclusions still hold; default and no-default suites pass.

Deferred work: an allowlist-style header policy; provider surfaces that need
cookies (none exist).

### Milestone 2 — Security configuration truthfulness

Class: invariant

Objective: every security setting either takes effect or reports itself as
reserved; secret-bearing configuration files are not silently world-readable.

Dependencies: none.

Deliverable boundary: `rust/src/config.rs` annotation and accessor, startup
warnings, example/documentation correction, a vulnerability-disclosure file,
and a startup permission warning for inline secrets.

User or operator value: an operator is never told that a CORS, Host, log
redaction, or access-log control is active when it is not.

Exit conditions: non-default inert values produce a typed warning; default
configs stay silent; the example no longer advertises them; the architecture
and deployment documents match the runtime; a 0600 config is silent and a
0644 inline-secret config warns without printing a value.

Deferred work: implementing Host validation, CORS, or an access log as real
features under their own plans.

### Milestone 3 — Authentication event audit

Class: infrastructure

Objective: a bounded, durable, content-free record of authentication and
authorization outcomes, written off the request path.

Dependencies: none; soft on M004 for the denial event vocabulary.

Deliverable boundary: one migration, one coalescing writer owned by the task
supervisor, emission points at the existing refusal branches, and an
administrator-only view.

User or operator value: after a suspected brute-force or token-stuffing
attempt, an administrator can see what happened and how often.

Exit conditions: events aggregate into minute windows; the table is bounded
and pruned; a flood cannot block or fail authentication; no row contains a
credential, token, prompt, body, IP address, or username; the view is
administrator-only; restart preserves rows.

Deferred work: extending the content-free JSON audit export with a security
section; a full access log.

### Milestone 4 — Personal credential scope enforcement

Class: invariant

Objective: make the persisted `scopes` value govern which endpoints an agent
token may use.

Dependencies: interface on the existing scope vocabulary; soft on M003.

Deliverable boundary: one middleware gate, typed denial, tests with narrowed
credentials, and documentation of the scope-to-path mapping.

User or operator value: a credential minted for one purpose cannot silently
use another.

Exit conditions: an agent token without the required scope receives a typed
403; every currently issued credential keeps its exact access; legacy shared
keys and browser sessions are unchanged; no scope grammar or issuance UI is
added.

Deferred work: selectable scopes at issuance; per-scope expiry.

### Milestone 5 — CI workflow pinning and least privilege

Class: infrastructure

Objective: every GitHub Actions reference is immutable and every workflow
declares least-privilege permissions.

Dependencies: none.

Deliverable boundary: SHA pins for the four uncovered workflows, a
`permissions: contents: read` default for `ci.yml`, a workflow-hygiene
validator, a tooling test, and a CI invocation.

User or operator value: a moving tag cannot change code executed in the
project's CI without a reviewed commit.

Exit conditions: the guard fails on a tag pin or a missing permissions block;
all workflows pass it; the existing release-workflow validator is unchanged;
the guard is exercised against a deliberate revert.

Deferred work: pinning runner images or third-party container digests.

### Milestone 6 — Release artifact signature authority

Class: capability

Objective: verify a release artifact independently of the origin that served
it, without weakening ADR-0001's dependency-light installer.

Dependencies: hard on an accepted ADR defining the signature mechanism, key
custody, rotation, and installer fallback. Status is blocked until then.

Deliverable boundary: an ADR, release-workflow signing, updater verification
before staging, installer verification, and failure-path tests.

User or operator value: a compromised release origin or TLS chain alone is
not enough to install a different binary.

Exit conditions: an artifact modified after publication is refused by both
the installer and `eggpool update`; a valid signature installs; key rotation
is documented; no unsigned fallback exists for new releases unless the ADR
explicitly defines a transition.

Deferred work: Sigstore transparency-log verification; Windows helper
signing.

### Milestone 7 — Service-unit and HTTP response hardening

Class: invariant

Objective: strengthen the production unit and give every HTML response the
baseline browser protections.

Dependencies: none.

Deliverable boundary: tracked and rendered unit hardening plus a drift guard;
one shared response-header helper; framing, MIME-sniffing, and referrer
headers on dashboard, sign-in, account, and token pages.

User or operator value: a compromised dashboard page is harder to frame or
reinterpret, and the service runs with a narrower kernel surface.

Exit conditions: the unit comparison guard passes and names every intentional
difference; the headers appear on every HTML path and never on JSON; existing
`no-store` behavior is preserved; a disposable-host systemd start is
qualified.

Deferred work: a script/style CSP with nonces; sandboxing beyond systemd's
documented directives.

### Milestone 8 — russh advisory resolution

Class: invariant

Objective: return the advisory gate to green without a new exception.

Dependencies: hard on an Eggress release that permits `russh >=0.65`, or on
an accepted ADR for vendoring or a no-SSH release policy. Status is blocked.

Deliverable boundary: a dependency update with requalification, or a
documented policy change with its ADR.

User or operator value: the shipped binary no longer carries known client
channel-handling and agent-confirmation advisories.

Exit conditions: `cargo deny check advisories` passes with no new ignore;
`cargo tree -i russh` shows a patched version or no `russh`; SSH proxy
behavior is either qualified or explicitly unsupported; the RSA exception is
re-reviewed and removed if its reachability changed.

Deferred work: unrelated dependency refreshes.

### Milestone 9 — Credential memory hygiene

Class: polish

Objective: shorten the lifetime of plaintext configuration-owned secrets in
process memory.

Dependencies: interface on the existing `zeroize` crate already resolved in
the graph.

Deliverable boundary: a redacted zeroizing wrapper for `server.api_key`,
account `api_key`, and provider static-header `value`, plus tests.

User or operator value: a core dump or accidental format of a config value is
less likely to contain a usable provider key.

Exit conditions: the three fields use the wrapper; `Debug` stays redacted;
TOML round-tripping is unchanged; `zeroize` is a direct dependency with a
compatible license; no CLI, HTTP, or runtime behavior changes. The public Rust
field-type change is explicitly authorized by ADR-0011.

Deferred work: OS environment, CLI arguments, request bodies, header copies,
and database rows.

## 8. Cross-cutting requirements

- Storage/migration: M003 adds one migration after `0066`; it must be
  append-only, checksum-embedded by the existing build script, and compatible
  with an upgraded database. No other milestone changes schema.
- Protocol/compat: no public inference or integration-profile shape changes.
  M002 removes advertised keys from the example but keeps parsing them for
  compatibility. M004 grants no new authority and removes none from any
  currently issueable credential.
- Security/auth: no milestone logs or persists a credential, token, prompt,
  request body, raw IP address, or username. Refusal responses keep their
  existing status codes unless a milestone explicitly states otherwise.
- Concurrency/cancellation/recovery: M003's writer is bounded, drop-tolerant,
  owned by the existing task supervisor, flushed on shutdown with a timeout,
  and never awaited by an authentication decision.
- Observability: M003 is the only new durable observability surface; it is
  metadata-only and administrator-only. M002 warnings are secret-free.
- Performance: no synchronous database write, allocation of request bodies, or
  new lock on the authentication hot path. M001 is a constant-list membership
  check.
- Docs/ops: every milestone updates `architecture/` and the operator docs it
  invalidates, and adds a guard where a prose claim could drift again.

## 9. Verification strategy

- Focused Rust targets: `server_auth`, `server_transport`,
  `on_prem_sessions`, `on_prem_admin_ui`, `coordinator_c013`,
  `provider_transport`, `database_compatibility`, `operations_o009`,
  `gateway_audit`, and the terminal-detail sanitizer target where a denial
  message is added.
- Tooling: `tests/tooling/test_workflow_hygiene.py` (new),
  `test_plan_status_consistency.py`, `test_release_supply_chain.py`, and the
  release-docs validator for documentation edits.
- Guards: the workflow-hygiene guard, the unit-drift guard, the header test,
  the scope test, the cookie observed-upstream test, and the inert-setting
  warning test must each be shown failing on the pre-fix state.
- Full gate before handoff: default and no-default check/clippy, the serial
  workspace suite, and the locked release build only when dependencies
  change (M008, M009).

## 10. Risks and decision points

- M002 chooses truthful deprecation over implementing Host validation or
  CORS. If the project wants those controls, a separate plan and (for Host
  policy) an ADR are required.
- M003 adds durable personal-adjacent data (an internal user identifier). It
  stores no username, IP, or credential; if review considers the identifier
  too identifying, the plan falls back to event counts only.
- M004 may be judged an auth-semantics change. If reviewers require an ADR,
  stop and write one before implementation.
- M006's mechanism and key custody are unresolved and block the plan. A
  checksum-only fallback must not be presented as equivalent.
- M008 has no local fix under the current Eggress pin. Vendoring or dropping
  the default `ssh` feature are architecture decisions, not implementation
  details.
- M009 is intentionally partial; zeroizing every copy would touch the wire and
  transport types and is not a bounded pass.
- Physical/systemd evidence for M007 needs a disposable Linux host; a macOS
  unit-text test is not service-start evidence.

## 11. Completion definition

The roadmap closes when every milestone has a closure record with
requirement-to-evidence mapping; the advisory gate passes without a new
exception; no inert security key remains; every HTML path carries the header
set; the workflow guard and the unit-drift guard are active in CI; the
security-event table is bounded and administrator-only; and no milestone has
weakened an invariant owned by another roadmap.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 1 | active | [001](../implementation/security-hardening/001-provider-request-credential-header-exclusion.md) | — | — |
| 2 | active | [002](../implementation/security-hardening/002-security-configuration-truthfulness.md) | — | — |
| 3 | active | [003](../implementation/security-hardening/003-authentication-event-audit.md) | — | — |
| 4 | active | [004](../implementation/security-hardening/004-personal-credential-scope-enforcement.md) | — | — |
| 5 | active | [005](../implementation/security-hardening/005-ci-workflow-pinning-and-least-privilege.md) | — | — |
| 6 | blocked | [006](../implementation/security-hardening/006-release-artifact-signature-authority.md) | — | Signature-authority ADR acceptance and signing-key custody |
| 7 | active | [007](../implementation/security-hardening/007-service-unit-and-http-response-hardening.md) | — | — |
| 8 | blocked | [008](../implementation/security-hardening/008-russh-advisory-resolution.md) | — | Eggress release permitting `russh >=0.65`, or an accepted vendoring/no-SSH ADR |
| 9 | active | [009](../implementation/security-hardening/009-credential-memory-hygiene.md) | — | — |
