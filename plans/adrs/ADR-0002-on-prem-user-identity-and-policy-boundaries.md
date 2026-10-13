# ADR-0002: On-prem user identity, policy, and ownership boundaries

Status: accepted

Date: 2026-10-13

Decision owners: project maintainers (explicit product direction, 2026-10-13)

Related specification sections:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Affected subsystem roadmaps:

- `plans/subsystems/on-prem-user-access-roadmap.md`

## Context

EggPool is a native Rust LAN provider router, not hosted SaaS. The canonical
operator is one owner on SBC/local hardware, with several TOML-configured
upstream provider accounts and optional operator-hosted private LLMs. The
desired extension is a low-friction single-instance installation supporting a
small team (roughly fewer than 50 named humans, **not** 50 simultaneous
inferences without evidence). Administrator-created users need browser login,
revocable per-person agent tokens, model access rules, reusable named profiles,
and per-user/per-model consumption limits.

Today `rust/src/server/middleware.rs` verifies one `server.api_key` or
`x-api-key`, `DashboardConfig.public` defaults true, `/v1/models` and
`/api/integrations/v1/profile` project global models, `accounts` represent
upstream credentials, and `quota/ + routing/` score provider-account capacity.
`coordinator/publication.rs` and `finalization.rs` persist and settle
provider-bound requests without downstream user identity. A login page and
dashboard-side usage counters would not establish tenant isolation or hard
usage limits.

## Decision drivers

- Keep the one-user SBC/LAN onboarding and inference path canonical and small.
- Avoid public registration, multi-organization infrastructure, cloud
  dependencies, enterprise IAM, built-in VPN, and billing.
- Respect local operators' provider choices, including their own privacy-
  sensitive self-hosted LLMs; do not require catalog cloud calls for identity.
- Keep upstream provider accounts/keys/topology and routing configuration in
  TOML/environment; keep human/user state, policies, sessions, audit and usage
  in the SQLite database.
- Preserve client wire, provider health/fairness, generation leases, SQLite
  single gate/worker, streaming, compensation, and existing deployments.
- Treat licensing/terms of upstream consumer subscriptions as operator
  responsibility; no claim that personal plans permit team sharing.

## Considered options

### A — Separate team-mode daemon or external identity service

Extra deployable components and distributed configuration add maintenance,
upgrade, credential handling, and SBC overhead. Rejected.

### B — Keep one global ingress key, add usernames only to the dashboard

A stolen/shared key has no accountable caller identity. Aliases, retries,
model discovery, and budgets cannot be enforced per person. Rejected.

### C — One instance, mandatory initial local administrator, optional users

Use one identity/authorization engine: one admin is the simple base case,
additional users and profiles enable managed small-team behavior. Separate
human browser authentication from per-user agent/API tokens. Selected.

## Decision

1. **Administrative domain and storage.** One instance/database and one
   operator-owned TOML provider/routing authority. The database owns local
   `users`, password hashes, browser sessions, client credential digests,
   profiles, overrides, user budget reservations, usage and audit. Never
   repurpose `accounts` or `account_models` for downstream people. No
   per-organization `tenant_id`, service mesh, hosted control plane or
   separate database per person.
2. **Bootstrap.** New interactive onboarding creates one administrator from a
   username and non-default password via local CLI/TTY; unattended setup uses
   protected stdin/FD rather than argv/environment. There is **no**
   `admin/admin`, network bootstrap, silent fallback, or unauthenticated admin
   endpoint. Initialization is atomic and protected by local filesystem
   ownership. The CLI provides controlled local recovery.
3. **Compatibility.** Legacy installations and pre-existing server keys must
   have an explicit upgrade path; never silently lock out unattended systemd
   users. New auth cutover may associate a legacy key with the initialized
   admin for inference-only compatibility, but may not grant web-admin access
   or attribute multi-user traffic under that shared key. Before adding a
   second user, retire or disable the shared legacy key. Managed mode never
   accepts unauthenticated loopback inference or `dashboard.public`
   exemptions for private/admin data.
4. **Two credential classes.** Local password+opaque, revocable server-side
   browser session is for human UI. Per-user, revocable high-entropy bearer/
   `x-api-key` tokens are for inference/model discovery/profile, with explicit
   scopes. Human password and cookie are not inference credentials; agent
   credentials are never administrative browser sessions. Existing OpenAI
   compatible wire and integration env-key conventions continue.
5. **Authentication and authorization.** Ingress resolves a typed principal
   from verified credentials; never accept a caller-supplied user ID as
   authority. All private/admin endpoints require role/scope checks and web
   mutation CSRF protection. Effective model policies intersect instance hard
   restrictions with a profile and optional admin-set per-user overrides.
   Explicit requested and eventual concrete provider-qualified targets,
   virtual routes, selector calls, compact and every inference surface are
   covered. Unauthorized models are not exposed in per-user discovery.
6. **Budgets.** Provider capacity/quota scoring remains provider-owned. A
   separate user/model budget authority has atomic pre-dispatch admission,
   durable reservations, exactly-once settlement/compensation semantics and
   request attribution across retries/streaming/recovery. Local model use can
   be metered in tokens/requests independently of USD. Missing pricing
   cannot silently count as zero under a hard USD limit.
7. **Administration.** Only admins create/disable users, modify profiles,
   grants and budgets. A member may manage their password and their own
   scoped tokens and view only authorized catalogs/own usage. Provider
   credentials and TOML routing remain local operator functions, not mutable
   member web UI state. Admin UI extends embedded Rust dashboard assets;
   no required SPA, JavaScript framework, external auth/DB server.
8. **Network boundary.** No native internet access stack. Loopback is the
   lowest-friction local case. Multi-user LAN browser credentials should use
   HTTPS through an operator-managed reverse proxy or VPN; plain HTTP LAN
   credentials are risky and require an explicit, documented security posture.
   No implicit trust of spoofable forwarding/identity headers.
9. **Performance target.** Design/test for approximately 1–49 named users
   on reasonable local hardware, keeping the single-user path fast. Bound
   caches, hash verification concurrency, DB write amplification, idle RSS
   and login cost on SBCs. Publish no concurrency capacity claim without
   measured representative traffic on the actual target class.

## Consequences

### Positive

- Minimal deployment unit; no external authentication or database dependency.
- Accountable per-user access and differentiated frontier-model consumption.
- Existing on-prem provider routing, including private LLMs, remains intact.
- Shared admin workflow without provider credentials crossing into the user DB.

### Negative

- Security-critical ingress, dashboard and lifecycle changes require coordinated
  migrations, backup semantics, failure recovery and extensive negative tests.
- Strong user budget enforcement increases admission/settlement SQLite work.
- HTTPS on LAN requires operator reverse-proxy configuration unless separate
  local-only access is chosen; plaintext LAN use is not silently safe.

### Neutral or deferred

- No SSO, invite/email workflow, cloud discovery, chargeback/billing, complex
  nested roles, multi-instance synchronization or native TLS frontend.
- Fine-grained organization hierarchies and cluster leases are out of scope.
- Named users are a capacity target, not a limit enforced by fiat.

## Compatibility and migration

Append checksum-verified SQLite migrations, preserve source-schema fixtures and
historical rows, and explicitly distinguish legacy-unattributed usage from
new authenticated requests. Preserve existing `[server].api_key` behavior
until an operator-controlled upgrade cutover. Separate the ability to migrate
old daemon installs unattended from permission to add users. Do not move
`providers.*.accounts` or upstream credentials into the database.
`configsetup`, `configremote`, `eggpool-connect`, `newkey/getkey`, and
`/api/integrations/v1/profile` require scoped compatibility tests and a
deprecation path for globally shared keys.

## Security and reliability implications

Require Argon2id password hashes calibrated for constrained SBCs, strict
bounds on simultaneous memory-hard authentication work, high-entropy random
tokens with non-recoverable at-rest digests, session rotation, CSRF protection,
constant-time comparisons, rate limiting, SameSite/HttpOnly/Secure cookies as
applicable, restrictive DB/backup modes and redacted observability. Auth
failure is closed, not the existing unauthenticated-loopback success path.
Explicitly define principal/policy snapshot semantics across reload, account
disable and mid-stream revocation. Reserve and compensate user budgets in
the coordinator's durable ownership lifecycle; never hold the DB gate during
provider I/O. Avoid adding separate producer DB writers or a per-request
external identity dependency.

## Verification

- Fresh admin bootstrap, one-user local use, headless stdin, and legacy
  systemd/backup upgrade paths; no default/remote bootstrap credential.
- Browser sessions, cookies, CSRF, role and cross-user endpoint attacks.
- Shared-key retirement and revocation race, token hashing/no exfiltration.
- Unauthorized concrete/provider-qualified/virtual/semantic/compact models
  cannot be dispatched; all client surface and discovery projections match.
- Concurrent user/model hard caps, retries, cancellation, streaming terminal,
  double finalization, crash/restart and stale-policy behavior.
- Pi/Le Potato-class and ordinary-hardware measured single-user overhead,
  1/5/10/25/49 named-account workloads, and bounded concurrent requests.
- Full default and no-default serial test/lint matrix, migrations/checksums,
  backup/restore, restart/rehash and docs/ops qualification.

## Supersession

None.
