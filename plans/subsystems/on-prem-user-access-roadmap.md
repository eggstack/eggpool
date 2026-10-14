# On-Prem User Access Roadmap

Status: active

Long-term references:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Related ADRs:

- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

## 1. Purpose and ownership boundary

Provide optional accountable use of **one** on-prem EggPool instance by
small teams while preserving its one-user SBC/LAN workflow. `rust/src/server/`
owns HTTP credential extraction, browser sessions/CSRF and route-role gates;
new `rust/src/identity/` and `rust/src/user_policy/` (suggested boundaries)
own account and effective authorization policy services; `rust/src/db/` owns
user tables and transactional reservation state; `rust/src/coordinator/`
owns authenticated-request propagation, durable publication and settlement;
`rust/src/operations/` + `runtime.rs` own local admin CLI/onboarding;
`rust/src/server/dashboard/` owns thin operator/member projections; existing
`routing/`, `quota/`, `providers/`, and TOML `Config` continue owning
upstream accounts and routing. Do not create a provider-secret web editor.

## 2. Work classification

### Invariants

- Single-instance on-prem; no SaaS organizations, public signup or upstream
  credentials in user-owned SQLite tables.
- Every admitted request retains a trustworthy principal; denial precedes
  upstream dispatch; aliases/selectors/cross-protocol requests cannot bypass
  model restrictions.
- User budgets and upstream provider quotas remain independent.
- Preserve generation leases, one SQLite connection/gate/worker and
  WAL/NORMAL, request publication/finalization ownership and redaction.
- No shared default admin password or exposed unauthenticated control path;
  managed access never inherits public-dashboard/loopback shortcuts.
- One-user default has no mandatory external IAM or added server process.

### Capabilities

- First-run admin CLI bootstrap + local recovery; password login; revocable
  per-user agent tokens; admin-created users; reusable model-policy profiles;
  user/model budgets; members' own catalogs and usage; administration UI.
- Self-hosted provider endpoints configured in TOML remain first-class, with
  usage counted even if no USD price exists.

### Infrastructure

- Identity/session/credential migrations; typed principal and immutable policy
  snapshot; cached credential resolution; durable budget holds/settlement;
  user-specific profile revisions and audit records.

### Polish

- Embedded dashboard UX, migration diagnostics, docs, single-user and
  1–49-user footprint/latency measurement on representative target classes.

## 3. Non-goals

No billing/chargeback, hosted control plane, tenant/organization segregation,
federated login/SSO, invite/email reset, native reverse-proxy/VPN/TLS manager,
remote account creation, public internet deployment guide, cloud model catalog
as authority, custom upstream per-user provider secrets, or a published
50-simultaneous-inference capacity promise.

## 4. Current state

M001–M007 were implemented on PR #7 from baseline
`c18dcc2051ae4987a2cb95a7cd046b8aff2095ff` and
each has a retained closure record. M002 and M004 are closed.
M001, M003, M005, M006 and M007 are **conditionally closed**,
not equivalent to release qualification.

The current Rust implementation contains:
- Append-only SQLite identity/session/request-attribution/policy/budget/admin
  migrations **0055–0060**; `rust/src/identity.rs`, `web_session.rs`,
  `model_policy.rs`, `user_budget.rs`.
- `rust/src/server/middleware.rs` / `auth.rs`: personal agent tokens,
  opaque browser sessions, CSRF and managed-route role fencing.
- `rust/src/coordinator/endpoints.rs`, `publication.rs`,
  `finalization.rs`: per-request principal, concrete-model authorization
  and downstream-user budget holds/usage settlement.
- `rust/src/server/dashboard/admin.rs` and `render/admin.rs`:
  admin/member user, profile, budget, usage and audit views.
- `rust/tests/on_prem_*.rs` and successful local/hosted default CI;
  the M007 synthetic provider artifact exercises seven loopback requests.

Remaining verified gaps (see immutable closure records M001/M003/M005–M007):
- M005 token/USD hard-limit semantics are not proven from estimated holds;
  unknown-price models can receive generic USD estimates; **global instance
  ceilings are absent**.
- Budget-specific process-crash and actual schema-54 restored-backup
  qualification, plus ordinary-host SQLite-gate overhead measurements,
  were not performed.
- M006 new admin/member pages lack browser screenshot/interaction
  qualification. The Python oracle applies to pre-existing pages only.
- Physical Pi/Le Potato Argon2/SQLite/WAL/TTFT and 1/5/10/25/49 **named**
  user workloads are unmeasured. No concurrency/capacity claim is authorized.
- M003 records local `operations::update` failures; the latest hosted
  branch CI is green, but reproducible platform classification is not yet
  documented.

Correctives M008–M012 were created by explicit user direction;
the predecessor closures remain immutable. This roadmap is **active**
until required corrective closure evidence exists.

## 5. Target architecture

One initial admin, more users only through admin-controlled creation. Local
CLI bootstraps/recover users. Browser password sessions and per-user API tokens
both resolve to an identity; credentials are not interchangeable. Layer
instance security > explicit user override > named profile > safe default
for effective policy, with hard deny/ceiling intersection. Expose
`/v1/models` and integration profile filtered by that identity. Freeze policy
for each authorized request and preserve stable user ID across retries and
terminalization; invalidate authorization for **new** requests immediately on
admin change, with documented in-flight behavior. Use a separate user-budget
reservation state machine over SQLite, durable compensations, and bounded user
analytics; never change provider score-only quota into user hard-cap logic.
Web UI uses existing embedded Rust HTML/CSS/JS; provider TOML remains local.

## 6. Dependency graph

- M001 (hard: accepted ADR only) -> M002 (hard: credential and session schema).
- M002 -> M003 (hard: principal/scope API stable).
- M003 -> M004 (hard: concrete-model attribution and principal propagation).
- M004 -> M005 (hard: effective policy semantics + atomic denial established).
- M005 -> M006 (hard: storage and quota API; soft: UI skeleton may be prepared).
- M006 -> M007 (hard: usable onboarding, admin/member web and client flow).
- Operational: physical SBC and normal-host evidence needed for M007 closure.
- Interface: existing published provider topology, generation leases, budget
  finalization, scoped `eggpool-client-config` integration shape throughout.

## 7. Milestones

### M001 — Secure admin bootstrap and client credentials

Class: infrastructure. No hard dependencies. Deliverable: schema, username+
password first-run CLI/TTY or stdin, one admin, non-recoverable token storage,
legacy-compatible staged migration, local recovery; no browser login or
multi-user claim. Exit: no admin/admin, no remote bootstrap, old installations
still start, isolated credential tests and backups pass. Deferred: cookies,
principal routing, admin web.

### M002 — Browser sessions and route authorization

Class: capability. Hard: M001. Deliverable: login/logout/change-password,
opaque sessions, CSRF, authorized route classification and managed
public-dashboard/loopback restrictions. Exit: end-to-end browser auth, role
denials and unauthenticated/no-CSRF negative tests. Deferred: user model
policies, usage.

### M003 — Request principal and durable user attribution

Class: infrastructure. Hard: M002. Deliverable: typed principal through all
client surfaces, compact, selector plumbing, publication/finalization; user ID
on new requests, legacy-unattributed history explicit; user-filtered
read-only discovery plumbing. Exit: retry/restart/cancel/stream stable user
attribution; no client header identity spoofing. Deferred: grants/budgets.

### M004 — Profile-based model policy

Class: capability. Hard: M003. Deliverable: admin-assignable profiles,
per-model access and preferences, user overrides, consistent concrete,
qualified/virtual/semantic/compact admission; user-discovery filtered.
Exit: no bypass through selectors or variants; disabled users fail closed
and new policy version visible without process restart. Deferred: quotas/UI.

### M005 — Transactional user and model budgets

Class: capability. Hard: M004. Deliverable: per-user and per-model request,
token, cost and concurrency caps where enforceable; atomic holds, settlement,
exactly-once recovery, unknown-price behavior, bounded rollups. Exit:
simultaneous requests cannot over-admit within defined reservation bounds;
stream/retry/failure recovery and local-model token metering pass. Deferred: UI.

### M006 — Administration and personal client integration

Class: capability. Hard: M005. Deliverable: embedded admin users/profiles/
budget/audit screens; member self-service password/tokens/usage; filtered
profile and `configremote`/`eggpool-connect` workflows, no member access to
provider config. Exit: end-to-end two-user clients with distinct catalogs,
limits and revocation; parity and no credential exposure. Deferred:
physical target qualification.

### M007 — Small-team SBC qualification and compatibility closure

Class: polish. Hard: M006. Deliverable: fresh/legacy/headless installation,
backup/restore, connection/profile compatibility and physical hardware
qualification, docs, truthful service sizing. Exit: 1,5,10,25,49 named users
with explicit concurrent load levels; single-user deltas; safe failure modes
and no unjustified performance/capacity claim. Deferred: SaaS/enterprise.

### M008 — Enforceable output-token and price-bound admission corrective

Class: capability. Ready (M005 published reservation contract exists).
Corrects M005's estimated-output/unknown-price hard-cap overclaim.
Deliverable: typed hard-versus-estimated enforcement, conservative
proof-or-reject across model/wire/selector/retries and explicit local-price
authority in operator TOML; preservation of existing estimated rules.
Exit: no unbounded/unpriced hard request reaches upstream; strict request/
concurrency behavior unchanged. Source:
`plans/implementation/on-prem-user-access/008-hard-token-cost-admission-corrective.md`.

### M009 — Instance-global budget and scope intersection corrective

Class: capability. Hard: M008 closure. Adds one-instance global and
optional model ceilings intersected with user/model rules, with
atomic cross-user last-slot holds, no tenant hierarchy or second writer.
Exit: two separate users cannot over-admit a shared global remaining
slot; one-admin/no-global-rule compatibility remains. Source:
`plans/implementation/on-prem-user-access/009-global-budget-intersection-corrective.md`.

### M010 — Durable crash recovery and schema-54 restore corrective

Class: invariant. Hard: M008 and M009 closures. Independent
schema-54 backup->migration and modern backup/restore checks,
restart/finalization/cancellation fault matrix, paired ordinary-host
SQLite gate observations. Reclassifies M003's local updater-test issue
against the latest hosted-green baseline without altering updater
code by default. Exit: every recoverable budget hold converges
correctly; no silent historical identity/backfill or restore gap. Source:
`plans/implementation/on-prem-user-access/010-budget-crash-recovery-and-schema54-restore.md`.

### M011 — Admin/member browser qualification corrective

Class: polish. Ready independently of M008–M010. Frozen Python oracle
continues verifying *historical dashboard pages*; new admin/member
pages get separate Rust-owned reference screenshots and interactive
CSRF/accessibility/role tests. Operational browser/oracle artifacts
must exist for full closure. Exit: no unreviewed existing-page
regression; new pages have reproducible desktop/mobile browser
evidence and no token leakage. Source:
`plans/implementation/on-prem-user-access/011-admin-dashboard-browser-parity-corrective.md`.

### M012 — Physical SBC small-team qualification and final disposition

Class: polish. Hard: M008–M011 closures. Operational: actual Pi/Le Potato
plus representative ordinary hardware. Measure 1/5/10/25/49 **named**
user counts separately from simultaneous request levels, login
hashing, RSS, SQLite/WAL and tail latency against a paired solo baseline.
Exit: evidence supports precisely stated deployment envelope; no
50-concurrent-request marketing claim and no unresolved release-
blocking budget/security/restore defects. Source:
`plans/implementation/on-prem-user-access/012-physical-sbc-small-team-qualification.md`.

### Corrective dependency graph and promotion

- Hard: M008 -> M009 -> M010 -> M012.
- Parallel: M011 may run alongside M008–M010, but M012 needs M011 closure.
- Operational: browser/oracle reference for full M011 closure; provisioned
  physical SBC and ordinary Linux host for M012. Unavailable evidence
  must be reported as blocker, never fabricated or replaced by hosted CI.
- No predecessor conditional closure is retroactively altered; an additive
  M008–M012 closure record provides the corrective proof. On each
  milestone closure, audit the blocked registry and promote only
  genuinely ready successors.

## 8. Cross-cutting requirements

- Storage: append immutable numbered migrations and checksums; preserve DB
  ownership and backups; no separate writer. Historical request `user_id`
  nullable and identified as legacy/unattributed.
- Protocol: OpenAI/Anthropic inputs and SSE semantics unchanged; agent
  `Bearer`/`x-api-key` compatibility with personal tokens; login cookies
  restricted to web, not inference.
- Security: Argon2id with SBC-calibrated memory/CPU and throttled parallelism,
  cryptographically strong tokens/digests, session CSRF/rotation, no password
  in argv, operator-supplied LAN TLS and robust role gates.
- Concurrency: no database held during provider I/O; idempotent publication/
  finalization/claim compensation; policy cache invalidation bounded and
  fail closed; reload/generation behavior explicit.
- Observability: per-user counters only under authorization; no prompt,
  credentials, hashes or sensitive metadata in ordinary diagnostics.
- Performance: low idle RSS and minimal hot-path overhead in solo profile;
  physical target evidence before claiming multi-user load support.
- Docs: status diagrams and CLI change descriptions accompany code, not plan.

## 9. Verification strategy

Use `rust/tests/server_transport.rs`, `cli_contract.rs`,
`database_compatibility.rs`, `coordinator_publication.rs`,
`coordinator_finalization.rs`, `coordinator_c008.rs`,
`coordinator_c009.rs`, `coordinator_c011.rs`, `coordinator_boundaries.rs`,
`model_router.rs`, `quota.rs`, `operations_o005.rs`, and new named
user-access tests. Include adversarial authorization, exact-surface model
bypass, credential race, multi-request budget, cancel/restart, backup,
migrations and current dashboard parity. Run Rust tests serial using
`--test-threads=1`, including no-default compile/Clippy. Add physical
loopback tests to `scripts/qualification_sbc.py` without touching user's
live keys or provider services.

## 10. Risks and decision points

- Password-bearing HTTP over LAN: require explicit safe transport posture
  and bound/label any insecure-lab override; do not mistake LAN for TLS.
- Existing `dashboard.public`, keyless loopback, and `getkey/newkey`
  compatibility cannot produce managed-mode privilege bypass.
- Unknown provider pricing and private-LLM cost are not zero by default.
- Selector calls and virtual aliases may spend hidden tokens: charge/authorize
  them or explicitly deny, never silently hide bypass.
- Single DB gate may bound concurrent request settlement; optimize only with
  measured gate-pressure and no WAL safety regression.
- Subscription/account sharing must follow each upstream's terms; no built-in
  claim of consumer-seat pooling rights.

## 11. Completion definition

M001–M007 historical closure records remain linked and truthful, with their
open conditions addressed by accepted M008–M012 corrective closure records.
The subsystem closes only when hard-limit rules are provable or fail closed,
instance-global budgets intersect correctly, crash/restore and browser
qualification pass, and physical SBC small-team evidence supports the
documented target envelope. Conditional closure is not release closure.
A ready plan is not an implemented capability.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| M001 | conditionally closed | `plans/implementation/on-prem-user-access/001-admin-bootstrap-and-credentials.md` | `plans/closure/on-prem-user-access/001-status.md` | Physical Le Potato/Pi Argon2 latency/RSS qualification carried to M007. |
| M002 | closed | `plans/implementation/on-prem-user-access/002-browser-sessions-and-route-auth.md` | `plans/closure/on-prem-user-access/002-status.md` | Browser sessions, managed route authorization and transport posture accepted; SBC hash qualification remains M007 evidence. |
| M003 | conditionally closed | `plans/implementation/on-prem-user-access/003-principal-propagation-and-attribution.md` | `plans/closure/on-prem-user-access/003-status.md` | Stable principal accepted; unrelated update unit-test failure and physical resource evidence are explicit conditions. |
| M004 | closed | `plans/implementation/on-prem-user-access/004-policy-profiles-and-model-gates.md` | `plans/closure/on-prem-user-access/004-status.md` | Profile rules, concrete-model gates and revision invalidation verified; M005 unblocked. |
| M005 | conditionally closed | `plans/implementation/on-prem-user-access/005-durable-user-budgets.md` | `plans/closure/on-prem-user-access/005-status.md` | Durable user holds and usage API verified; exact output-token and USD caps, global scope, crash/restore tests, and overhead measurements remain named conditions. |
| M006 | conditionally closed | `plans/implementation/on-prem-user-access/006-web-admin-and-personal-integrations.md` | `plans/closure/on-prem-user-access/006-status.md` | Browser parity captures remain outstanding; M005 estimate conditions and physical qualification carry into M007. |
| M007 | conditionally closed | `plans/implementation/on-prem-user-access/007-small-team-qualification-and-migration.md` | `plans/closure/on-prem-user-access/007-status.md` | Physical SBC capacity, exact schema-54 restore, dashboard parity, and inherited M005 conditions remain unqualified; no capacity claim. |
| M008 | closed | `plans/implementation/on-prem-user-access/008-hard-token-cost-admission-corrective.md` | `plans/closure/on-prem-user-access/008-status.md` | Hard/estimated enforcement, proof-or-reject admission, operator tariff trust boundary verified. |
| M009 | closed | `plans/implementation/on-prem-user-access/009-global-budget-intersection-corrective.md` | `plans/closure/on-prem-user-access/009-status.md` | Instance-global intersection, atomic cross-user holds, user-independent history verified. |
| M010 | closed | `plans/implementation/on-prem-user-access/010-budget-crash-recovery-and-schema54-restore.md` | `plans/closure/on-prem-user-access/010-status.md` | Reconciliation releases crash-orphaned budget holds without folding usage; frozen v54 image migrates 55–62 with legacy history intact; ordinary-host gate observations recorded (not SBC evidence). |
| M011 | ready | `plans/implementation/on-prem-user-access/011-admin-dashboard-browser-parity-corrective.md` | — | Independent implementation; operational browser/oracle artifacts required for full closure. |
| M012 | blocked | `plans/implementation/on-prem-user-access/012-physical-sbc-small-team-qualification.md` | — | Hard: M008–M011 closures; operational physical hardware evidence. |
| M013 | closed | `plans/implementation/on-prem-user-access/013-transcode-eligibility-corrective.md` | `plans/closure/on-prem-user-access/013-status.md` | One-line transcode-protocol restoration plus admission bridge guard; Q007 green. |
