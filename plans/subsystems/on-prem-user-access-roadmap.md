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

PR #7 landed on `main` as merge commit `88f58bbf2e9095cf46437945edf83e580270bdb6`
on 2026-10-15. M001–M016 closure records remain immutable historical
evidence; M016's pre-merge review hold no longer describes the live branch.
The current runtime includes SQLite identity/session/policy/budget/audit
migrations 0055–0062, browser and personal-token auth, user/model/instance
budgets, CSRF-fenced administration and scoped discovery. Physical Pi 5
qualification measured solo/lightweight footprint and microSD saturation,
not ordinary-server small-team capacity; see M012 closure and separate
on-prem concurrency M001.

A post-merge integration audit found independent correctness gaps not covered
by the M016 pre-merge checks: `configsetup`/`configremote`/`getkey`
still rely on the retired shared server key; `status` and
`runtime-status` authenticate via that key and fail after bootstrap;
remote plaintext `GET /login` renders a password form that cannot safely
submit; first-member web transition and CLI `enable-managed` disagree
about config prerequisites; headless member CRUD and safe default provisioning
are incomplete. Hard-cost admission is already proof-or-reject (M008/M015),
but coding-agent compatibility needs bounded qualification.

Product/hardware scope clarified 2026-10-15: **SBC/microSD is primarily
the solo-owner/lightweight reference**. Small teams are expected primarily
on ordinary on-prem Linux servers with durable SSD/NVMe and operator-managed
HTTPS for browsers. None of the new plans assumes 49 concurrent requests or
relaxes the single-process/SQLite/WAL architecture.

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

### M014 — Rustls dependency security-advisory corrective

Class: invariant. Closed. PR #7 updates the shared resolved Rustls
family from 0.23.45 to 0.23.46 with no provider-family or feature
changes. The exact implementation SHA passed hosted cargo-deny, CI,
Linux/macOS installer qualification, and local default/no-default,
transport, budget, and tooling checks. `main` retains 0.23.45 until
the PR is merged; this remains a merge prerequisite, not a separate
production change. Source:
`plans/implementation/on-prem-user-access/014-rustls-advisory-dependency-corrective.md`.

### M015 — Independent authentication and authorization threat review

Class: invariant. Closed on PR #7 candidate `d8fd9c2a`. Independent
review found and corrected hard-cost proof under-reservation and two
browser-session middleware rejection paths. Regression tests and full
default/no-default local suites pass; exact-SHA hosted CI, installer and
dependency audit pass. The security scan and independent reviewer found no
remaining M015 finding. Closure evidence:
`plans/closure/on-prem-user-access/015-status.md`. Source:
`plans/implementation/on-prem-user-access/015-authz-threat-review-corrective.md`.

### M016 — Final integration and merge-readiness reconciliation

Class: polish. Closed as a **historical pre-merge reconciliation**
on exact head `3d68c715`. Local, browser, release-doc, Q007 and exact-head
hosted CI/installer/**cargo-deny** evidence passed. The M016 closure
recommended holding for maintainer review at that point; the PR was
subsequently merged on 2026-10-15 as `88f58bbf`. This current roadmap
tracks new M017–M023 post-merge work, not the old merge hold. No 49-concurrent
capacity claim was made. Closure:
`plans/closure/on-prem-user-access/016-status.md`. Source:
`plans/implementation/on-prem-user-access/016-final-pr7-merge-reconciliation.md`.

### Post-M012 security/merge dependency graph

- Hard: M014 closure + M015 closure -> M016; both are accepted.
- Operational for M016 closure: hosted CI, installer shells and
  security audit must pass on the exact final candidate commit.
- Separate nonblocking performance roadmap:
  `plans/subsystems/on-prem-concurrency-performance-roadmap.md`,
  M001 Pi 5/MMC tail attribution, does **not** block M014–M016
  or revise M012's closed physical evidence. Any new storage
  topology needs a separate accepted ADR.

### Post-merge M017–M023 corrective and team-utility milestones

#### M017 — Managed client credential reconciliation

Class: capability. Ready independently. Repair `onboard`,
`getkey/newkey`, `configsetup`, `configremote` and `eggpool-connect`
to use valid personal keys after bootstrap, preserve `EGGPOOL_API_KEY`
and secret-free `epc1`, and test real end-to-end agent requests.
Source: `plans/implementation/on-prem-user-access/017-managed-client-credential-reconciliation.md`.

#### M018 — Local operator status corrective

Class: capability. Ready independently. Restore `eggpool status` and
`runtime-status` for OS-authorized local operators without granting agent
tokens browser/admin rights. Prefer existing owner-protected local control
channel; new privilege decisions require ADR review.
Source: `plans/implementation/on-prem-user-access/018-local-operator-status-corrective.md`.

#### M019 — Login transport and solo LAN operability

Class: invariant. Ready independently. Prevent remote plaintext
`GET /login` rendering credential fields, verify proxy/CSRF invariants,
and document a clear SSH/loopback solo SBC browser path plus ordinary-server
TLS-proxy team path. No native TLS server or insecure LAN default.
Source: `plans/implementation/on-prem-user-access/019-login-transport-solo-lan-corrective.md`.

#### M020 — Managed transition and headless administration

Class: capability. Blocked on hard M017/M019 closure. Unify
`enable-managed` with member-create transition requirements and add local
CLI create/disable/enable/show member workflows; preserve audit, CSRF,
password-stdin, last-admin and atomic creation guarantees.
Source: `plans/implementation/on-prem-user-access/020-managed-transition-headless-admin.md`.

#### M021 — Default member policy and provisioning UX

Class: capability. Blocked on hard M020 closure. Add explicit reusable
member default profile (and optional budget template) with safe fallback,
transactional assignment, admin/CLI consistency and understandable
catalog-empty/deny feedback. Never grant unaudited model access.
Source: `plans/implementation/on-prem-user-access/021-default-member-policy-provisioning.md`.

#### M022 — Hard-budget agent interoperability

Class: polish. Blocked on hard M017 closure. Measure whether client/surface
requests carry sufficient output-token proof; improve diagnostics and
introduce output-cap insertion only where upstream and wire evidence prove a
hard bound. Do not regress M008/M015 hard-cost safety.
Source: `plans/implementation/on-prem-user-access/022-hard-budget-agent-compatibility.md`.

#### M023 — Short-window team fairness controls

Class: capability. Blocked on hard M022 closure. Optional per-principal
short-window admission throttle and stable 429/Retry-After, bounded in-memory
state, no unbounded queues or new SQLite write pressure; qualify on ordinary
server hardware while preserving solo no-rule behavior.
Source: `plans/implementation/on-prem-user-access/023-short-window-fairness-controls.md`.

### Post-merge dependency and hardware policy

- Parallel ready: M017, M018 and M019. None depends on a Pi/Le Potato run.
- Hard: M017 + M019 -> M020 -> M021; M017 -> M022 -> M023.
- Interface: M018's privileged local transport must not broaden HTTP rights;
  any new auth authority stops for ADR review. M022 hard-cost proof contract
  is fixed by M008/M015.
- Operational: ordinary-server M002 performance qualification in
  `plans/subsystems/on-prem-concurrency-performance-roadmap.md` is independent
  and nonblocking to M017–M022; it supplies evidence for M023 and future
  team-capacity claims. Pi microSD tail investigation is a solo benchmark,
  not a team-server release gate.
- Deferred, not silently bundled: optional personal-token expiry/rotation,
  secondary administrators (requires admin uniqueness migration and security
  review), notifications and SSO; take them as separate bounded plans only
  after core operability closure.

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

M001–M007 historical closure records remain linked and truthful;
M008–M013 provide the accepted additive corrections, including a
physically measured **restricted** Pi 5/MMC envelope. The prior
M012 roadmap closure is preserved as historical evidence. New
RUSTSEC advisories and missing integrated pre-merge security
review require M014/M015 closure and M016 final same-SHA
integration evidence before this roadmap may close **again**.
Physical MMC tail attribution is separately nonblocking; no
unsupported concurrency/service capacity claim is authorized.
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
| M011 | closed | `plans/implementation/on-prem-user-access/011-admin-dashboard-browser-parity-corrective.md` | `plans/closure/on-prem-user-access/011-status.md` | Frozen Python oracle rebuilt and run with no new historical parity gaps; on-prem admin/member pages qualified against a Rust-owned approved reference. Browser sign-in and responsive/navigation defects corrected. |
| M012 | closed | `plans/implementation/on-prem-user-access/012-physical-sbc-small-team-qualification.md` | `plans/closure/on-prem-user-access/012-status.md` (closed) | Pi 5 managed envelope measured (functional + 3× benchmark, Argon2, 49 named users, 1/2/4/8 concurrency); 8-way saturates, Le Potato/reference-host/16-way recorded as low-severity uncovered dimensions. |
| M013 | closed | `plans/implementation/on-prem-user-access/013-transcode-eligibility-corrective.md` | `plans/closure/on-prem-user-access/013-status.md` | One-line transcode-protocol restoration plus admission bridge guard; Q007 green. |
| M014 | closed | `plans/implementation/on-prem-user-access/014-rustls-advisory-dependency-corrective.md` | `plans/closure/on-prem-user-access/014-status.md` | Rustls 0.23.46; exact-SHA hosted cargo-deny, CI, and installer jobs pass. Main receives the fix when PR #7 merges. |
| M015 | closed | `plans/implementation/on-prem-user-access/015-authz-threat-review-corrective.md` | `plans/closure/on-prem-user-access/015-status.md` | Exact candidate `d8fd9c2a`; findings fixed and independently re-reviewed; exact-SHA hosted gates pass. |
| M016 | closed | `plans/implementation/on-prem-user-access/016-final-pr7-merge-reconciliation.md` | `plans/closure/on-prem-user-access/016-status.md` | Exact candidate checks pass; merge held pending maintainer PR review. PR #7 subsequently merged at `88f58bbf`; historical M016 closure remains unchanged. |
| M017 | ready | `plans/implementation/on-prem-user-access/017-managed-client-credential-reconciliation.md` | — | Independent; full onboarding/desktop client replay needed. |
| M018 | ready | `plans/implementation/on-prem-user-access/018-local-operator-status-corrective.md` | — | Independent; inspect local control authority/ADR threshold. |
| M019 | ready | `plans/implementation/on-prem-user-access/019-login-transport-solo-lan-corrective.md` | — | Independent; insecure remote GET password form. |
| M020 | blocked | `plans/implementation/on-prem-user-access/020-managed-transition-headless-admin.md` | — | Hard: M017 and M019 closure. |
| M021 | blocked | `plans/implementation/on-prem-user-access/021-default-member-policy-provisioning.md` | — | Hard: M020 closure. |
| M022 | blocked | `plans/implementation/on-prem-user-access/022-hard-budget-agent-compatibility.md` | — | Hard: M017 closure. |
| M023 | blocked | `plans/implementation/on-prem-user-access/023-short-window-fairness-controls.md` | — | Hard: M022 closure; ordinary-server evidence operational. |
