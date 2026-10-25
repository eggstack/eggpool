# EggPool Active Planning Registry

This file is the compact control surface for active interim planning.
Detailed requirements and completed history remain in source roadmaps,
implementation plans, `plans/closure/`, flat legacy plans, and Git history.
Links only — do not duplicate milestone requirements here.

Canonical direction:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`
- `plans/003-planning-process.md`

Legacy archive (pre-251, immutable, top level): `plans/001-*` through
`plans/250-*` plus `python_hotpath_dispatch_compression_optimization.md`.
Most recently closed: Request Admission and Wire M008 (remote compaction capability
contract; a compact request against an installation with no eligible provider surface
fails with a typed capability outcome and consumes no provider attempt;
`plans/closure/request-admission-wire/008-status.md`),
Operations CLI M003 (watchdog start-guard ownership
correction; a lock that names no owner is reclaimable while an undeterminable
owner is still held, the publisher confirms the path still names its own file,
and a vanished lock is free; `plans/closure/operations-cli/003-status.md`),
Routing Selection M007 (durable backoff scope and reason correction; the
model-scoped row carries the live reason and the model's own rung, and the
hydration restores a row by its scope; `plans/closure/routing-selection/007-status.md`),
Request Admission and Wire M009 (terminal detail redaction coverage
correction; the JSON spelling, the standalone scheme words, the URL userinfo
authority, and the case-insensitive vendor prefix are covered;
`plans/closure/request-admission-wire/009-status.md`),
and Deployment and Packaging M007 (release action-pin enforcement and guard
correction; the qualification workflows are SHA-pinned and held to the rule,
and the cited glob guard now reads the repository;
`plans/closure/deployment-packaging/007-status.md`).
Previously: Operations CLI M002 (local process lifecycle safety; an
undeterminable lock owner is a held lock whose file is left in place, nine
deadline sites go through one checked helper returning a typed error, and
non-finite seconds are rejected instead of coercing; `plans/closure/operations-cli/002-status.md`),
Routing Selection M006 (backoff policy adoption and health-reason
vocabulary unification; the live path now applies the existing bounded policy
and writes one typed reason vocabulary; `plans/closure/routing-selection/006-status.md`),
Deployment and Packaging M006 (release artifact version derivation and
repository artifact hygiene; the qualification workflows derive the version
and the tracked/ignored artifact conflict is reconciled; `plans/closure/deployment-packaging/006-status.md`),
Planning Governance M002 (review index, ownership, and census reconciliation;
the CLI, migration, route, and module censuses are corrected and guarded;
`plans/closure/planning-governance/002-status.md`),
and Persistence M011 (post-M010 roadmap and documentation reconciliation;
the remaining detailed milestone bodies now read closed; `plans/closure/persistence/011-status.md`).
Earlier: Dashboard M013 (observation projection derivation
reconciliation; five duplicated derivations single-sourced, no published
field name or shape changed; `plans/closure/dashboard/013-status.md`).
Previously: On-prem Concurrency Performance M001 (conditionally
closed after three paired physical Pi 5/MMC and tmpfs runs; durable
publication/SQLite/storage attribution; remaining controls in
`plans/closure/on-prem-concurrency-performance/001-status.md`).
Legacy archive latest: Plan 250
(EggServe 0.3.0 direct-Tower migration, `7879cbf9`). Plans 244–245, 215–220,
241 remain historical per their own closure passes; the `146-*` duplicate
pair is a known numbering accident.

Post-merge planning reconciliation (2026-10-15): PR #7 has merged to
`main` (`88f58bbf`). M016's closure accurately describes its historical
pre-merge hold, not the current branch. M017–M023 register independent
post-merge correctness, headless-administration and optional small-team
capability passes. Solo SBC/microSD remains the lightweight reference; ordinary
on-prem server hardware with durable storage is the expected team host.
No new runtime, schema or performance claim lands with these plans.

## Status vocabulary

- **proposed** — roadmap or plan exists but is not approved for execution.
- **ready** — dependencies and interfaces satisfied; may be handed off.
- **active** — implementation or closure work in progress.
- **blocked** — a named dependency or evidence requirement prevents progress.
- **closing** — implementation landed; closure evidence being gathered.
- **closed** — closure record accepted.
- **conditionally closed** — substantial work landed, named correctness or
  operational evidence remains (condition + risk + exact future evidence in
  the closure record).
- **superseded** — replaced by another document.
- **archived** — no longer active; retained for traceability.

## Active subsystem roadmaps

| Subsystem | Status | Roadmap | Current milestone | Dependencies or blockers |
|---|---|---|---|---|
| Provider transport | closed | `plans/subsystems/provider-transport-roadmap.md` | M006 closed — Eggress 1.0.12 dependency refresh and requalification | M001–M006 closed; no successor registered. |
| Provider profile metadata corrective | closed | `plans/subsystems/provider-profile-metadata-corrective-roadmap.md` | M001 closed — provider template endpoint/source reconciliation | M001 terminal; no successor registered. |
| Provider profile metadata planning/documentation reconciliation | closed | `plans/subsystems/provider-profile-metadata-planning-reconciliation-corrective-roadmap.md` | C001 closed — closed-roadmap/source-truth reconciliation | No successor registered. |
| Shared provider profile contract | closed | `plans/subsystems/shared-provider-profile-contract-roadmap.md` | M002 closed — OpenCode Go wire hints for the current first-party endpoint table | M001 and M002 closed; the roadmap re-opened on evidence and closed again on the same evidence. The four placed-but-unhinted models now resolve on their reviewed surfaces — `beam-free`, `minimax-m3.1-flash-free` and `step-5-preview` on Chat Completions, `claude-haiku-5-5` on Anthropic Messages. `gemini-4-argon-contributor` and the five unplaced catalog ids stay unresolved. The six entries the current table no longer places keep their hints with a recorded disposition, and a drift guard compares the wire map, the reviewed record and a dated catalog snapshot, failing closed on an unexplained delta. CodeGG consumption of M001 remains unblocked at immutable revision `9ac6a131`; CodeGG consumption of M002 is pinned to the M002 closure revision. `longcat-2.5-preview-free` stays withdrawn upstream and is not M002's to restore. |
| Routing selection | active | `plans/subsystems/routing-selection-roadmap.md` | M009 blocked — health effect ownership and terminal-withdrawal producer retirement | Reopened on evidence at baseline `e6822d8f78`: M001–M003 remain closed. M004, M005, and M006 are closed through M007. M008 is registered and is blocked only on the acceptance of `plans/adrs/ADR-0005-durable-health-state-repair-and-authentication-failure-durability.md`. M009 is registered and is blocked on the acceptance of `plans/adrs/ADR-0006-authoritative-terminal-model-withdrawal-ownership.md` and on M008 closed. |
| Runtime efficiency | closed | `plans/subsystems/runtime-efficiency-roadmap.md` | M001–M003 closed — no successor registered | No M003-dependent plan was unblocked. Persistence M007 has since completed Pi 5/MMC qualification and was rejected on WAL convergence gates; production is unchanged. |
| Persistence | active | `plans/subsystems/persistence-roadmap.md` | M011 closed — post-M010 roadmap/documentation reconciliation | M009/M010 closures accepted. Documentation/tooling-only corrective; production remains WAL/NORMAL. No technical persistence successor is registered. |
| Deployment and packaging | active | `plans/subsystems/deployment-packaging-roadmap.md` | M008 ready — CI dependency-lock discipline | Reopened on evidence at baseline `e6822d8f78`: M001–M005 remain closed. M006 is closed through M007. M008 is registered and has no dependency. |
| Dashboard | closed | `plans/subsystems/dashboard-roadmap.md` | M014 closed — operator surface presentation and accessibility corrective | Reopened on evidence at baseline `e6822d8f78`: M001–M013 remain closed. M014 is closed; the paired browser disposition needs the frozen oracle worktree and a browser, and the stale on-prem browser reference artifact has to be regenerated on hardware. |
| On-prem user access | closed | `plans/subsystems/on-prem-user-access-roadmap.md` | M024 closed — browser session CSRF delivery and member account surface corrective | Reopened on evidence at baseline `d04ad6831c6e030b7f5fc8a56501fdfecab982bd`: M001–M023 remain closed. `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md` was accepted on 2026-10-23, and M024 implements that decision; it is closed. |
| On-prem concurrency performance | active | `plans/subsystems/on-prem-concurrency-performance-roadmap.md` | M002 closed — ordinary-server synthetic baseline recorded | M001's Pi/microSD solo tail attribution stays conditionally closed; M002 authorizes no WAL/SQLite topology change. |
| Operations CLI | active | `plans/subsystems/operations-cli-roadmap.md` | M003 closed — watchdog start-guard ownership correction | New subsystem roadmap opened at baseline `e6822d8f78` for `rust/src/operations/` and `rust/src/runtime.rs`, which no other roadmap claims as a primary owner. M001 is closed and M002 is closed through M003; no successor is registered in this subsystem. |
| Request admission and wire | active | `plans/subsystems/request-admission-wire-roadmap.md` | M008 closed — remote compaction capability contract | ADR-0003 accepted; M001–M007 and M009 remain closed; M008 is closed; no successor is registered in this subsystem. |
| Planning governance | active | `plans/subsystems/planning-governance-roadmap.md` | M002 closed — review index, ownership, and census reconciliation | M001 closed; M002 closed with five census guards. The subsystem's ongoing obligation — that the review index and the agent workflow index stay aligned with the shipped runtime — is now enforced by guards rather than by review alone. No successor is registered. |

Evidence-based reopening at baseline `e6822d8f78` (2026-10-22): a repository
investigation found selection-path defects that decide the permissive outcome
on unknown or malformed input, a durable health subsystem that is implemented,
documented, and inert in production, a terminal detail sanitizer that bounds
length without removing secret material, transport input parsing that accepts
malformed values as valid, duplicated dashboard and CLI derivations that
disagree, a release version pinned as a literal in two workflows, and an
architecture review index that undercounts the CLI command tree and the
migration chain and omits the managed-user subsystem entirely. The Routing
Selection, Request Admission and Wire, Dashboard, Deployment and Packaging,
and Planning Governance roadmaps are reopened on that evidence; their closed
milestone records remain immutable. The Operations CLI roadmap is new, opened
for `rust/src/operations/` and `rust/src/runtime.rs`, which no other roadmap
claims as a primary owner. The canonical long-term documents are not touched.

`plans/adrs/ADR-0003-remote-compaction-capability-contract.md` is accepted. It
records the capability contract for the public `POST /v1/responses/compact`
endpoint, which is fully implemented, tested, and documented while no provider
surface advertises the capability, so a real request cannot reach a capable
upstream. Request Admission and Wire M008 implements that decision and is
closed; a compact request that finds no eligible provider surface now fails
with a typed capability outcome instead of an ordinary selection failure. A
bundled provider opt-in is explicitly not authorized by it.

`plans/implementation/dashboard/014-operator-surface-presentation-and-accessibility-corrective.md`
is Dashboard M014 and is closed. The dashboard stylesheet declared no custom
properties of its own, so every page shell that did not link the theme
stylesheet resolved no variable at all, and the theme stylesheet was linked
from the shared layout only. The administrative tables were emitted without
the class the table rules target. The status pills were painted with fixed
hexadecimal colours that bypassed the theme's own semantic tokens: measured
against the surface the pill text sits on, all fifty bundled themes failed
4.5:1, with a worst case of 1.42:1, and in thirteen themes every one of the
five pills failed. The chart loading path depended on Chart.js arriving within
a fixed window, and the chart surface and the refresh indicator were not
announced to a screen reader. All of that is corrected: the theme is delivered
on every shell, the table chrome is enforced by a shared opening tag, the pill
foregrounds are derived from the theme's own tokens and now clear 4.5:1 in all
fifty themes with a worst case of 4.53:1, the chart surface terminates in a
chart or an explicit failure, the accessibility surface is in place, and the
retired frontend is gone behind a guard that fails on reintroduction. The
asset manifest matches the working-tree bytes and the oracle divergence record
carries one reason per divergence. The paired browser disposition needs the
frozen oracle worktree and a browser and could not be executed here, and the
stale on-prem browser reference artifact has to be regenerated on hardware;
both are recorded against the subsystem. No published dashboard field name or
shape changed.

`plans/implementation/on-prem-user-access/024-browser-session-csrf-delivery-and-member-account-surface-corrective.md`
is On-prem user access M024 and is closed. The CSRF cookie was delivered on a
strictly narrower set of requests than the session cookie, so an operator who
reached an administrative page by a cross-site top-level navigation was
authenticated but could not act: the page rendered and every mutation was
refused. The server could not repair this for an already-signed-in browser,
because only the CSRF digest is persisted. The sign-in throttle bounded an
account-and-address pair rather than an account, and a successful sign-in did
not clear the counter. The sign-in, account, and member surfaces carried no
stylesheet, a member had no landing page and no way to reach the member
surface without knowing a URL, and the sign-in responses were cacheable. The
milestone changed auth semantics, so it required an architecture decision:
`plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`,
which the project maintainers accepted on 2026-10-23. Two passages in the ADR's
`proposed` draft were corrected before acceptance because they contradicted the
ADR's own Decision: no throttle limit is raised (the account limit stays at 10
and the per-peer limit stays at 30), and a successful sign-in clears the
account's failure bucket only, because clearing the per-peer bucket would let
any valid account reset an address's cross-account failure budget. M024
implements that decision: the CSRF cookie is `SameSite=Lax` on both its set and
clear paths, the account bucket is keyed on the normalized username alone, one
document builder renders every sign-in and member surface, every sign-in
response is `no-store`, and a member lands on `/account/landing` that links
the member surface. The server-side origin check, the double-submit token
requirement, the transport gate, and the disjointness of the member and
administrator surfaces are unchanged, and no schema, configuration,
dependency, or published API changed. The account lockout stays a hard
refusal, so a correct password does not break an active lockout; that is the
intended behavior of an account-scoped limiter and is recorded in ADR-0004.

`plans/implementation/routing-selection/008-durable-health-state-repair-and-authentication-failure-durability.md`
is registered as Routing Selection M008 at baseline
`1929abf5a725cc7faac0f866622afa7af017694a`. The durable backoff table's
unique key treats a NULL `model_id` as distinct, which SQLite's unique index
does, so the table does not deduplicate account-scoped rows; the repository has
already met that hazard once, in migration 0054, which added the same partial
unique index to the quarantine table. The durable writer has refused a
`model_unavailable` row without a model identity since D005, but the reader
treats such a row as fatal rather than skipping it, and the fatal error reaches
the startup path: the hydration fails, the generation build fails, and
`rust/src/server/mod.rs` returns an error, so EggPool does not start. The rows
are reachable from the system's own history — the Python-era reader met them in
practice and skipped them explicitly. Migration 0051 states that such rows are
hydrated as `migration_legacy` provenance, but that provenance is never
constructed anywhere and the promise is not implementable as written, because
the legacy row carries no model identity to attribute. Separately, the
`authentication_failed` durable row has a validator branch, a hydrator, a
credential-change rehash path, and a cleanup, but no writer: the reason's
policy has no timed backoff, so the live path disables the account in memory
only and the suppression is lost on a restart. The milestone changes what
durable state means across a restart and changes auth semantics, so it is
blocked on the acceptance of
`plans/adrs/ADR-0005-durable-health-state-repair-and-authentication-failure-durability.md`
and on nothing else. Accepting that ADR is a one-line status change on the ADR
plus its acceptance note.

`plans/implementation/routing-selection/009-health-effect-ownership-and-terminal-withdrawal.md`
is registered as Routing Selection M009 at baseline
`1929abf5a725cc7faac0f866622afa7af017694a`. The health subsystem carries a
parallel `HealthEffectApplier` that has never had a production caller: the
repository history contains exactly one commit that mentions it, the one that
introduced it. It diverges from the live path in ways the test suite pins — it
disables jitter where the live path enables it, so its test asserts a backoff
duration of 500.0 seconds where the live policy produces 462.5, and it writes
the account's escalation rung into a model durable row, which is the defect
M007 corrected in the production path only. The authoritative terminal model
withdrawal has the mirror problem: its consumer half is complete and its
producer does not exist. `ProviderModelPresence::AbsentAuthoritative` is never
constructed anywhere, so the terminal effect is never selected, so the in-memory
terminal set is never populated and its three eligibility checks never fire;
the only implementation of the durable terminal writer is inside the dead
applier. The Python-era runtime had a producer for it and the Rust runtime does
not. The durable terminal state itself was a deliberate exclusion, recorded in
the closed M005 record because the synchronous success path could never lift a
durable terminal row. The milestone removes three types from the library
crate's public surface and changes a capability boundary, so it is blocked on
the acceptance of
`plans/adrs/ADR-0006-authoritative-terminal-model-withdrawal-ownership.md` and
on M008 closed, because both milestones edit `rust/src/health/` and
`rust/src/routing/router.rs`. Accepting that ADR is a one-line status change on
the ADR plus its acceptance note.

`plans/implementation/deployment-packaging/008-ci-dependency-lock-discipline.md`
is registered as Deployment and Packaging M008 at baseline
`1929abf5a725cc7faac0f866622afa7af017694a`. The four CI gates that install
the Python environment use `uv sync --frozen`, which installs the versions the
lockfile records without checking that the lockfile still satisfies the
project metadata. Measured against uv 0.11.16 in an isolated project, a
deliberately drifted lockfile exits 0 under that flag, and the first `uv run`
that follows then re-resolves, rewrites `uv.lock` in the working tree, and
still exits 0 — so the job qualifies a resolution the repository never
committed, and no step reports the substitution. The same drifted tree exits 1
under `uv sync --locked` with a message naming the lockfile and the remedy.
The milestone changes the gate, not the lockfile, so it selects no dependency
and reaches no ADR threshold. The originally flagged line,
`tests/tooling/test_release_docs.py:29`, does not survive measurement as an
outlier: the repository has eighty `uv run` invocations across the workflows,
the scripts, and the tests, and none of them carries a lock flag, so the
flagged line follows the uniform convention. The milestone replaces that
subprocess with the interpreter the test suite already runs under, matching the
existing `tests/tooling/test_installer.py` precedent, and adds a guard that
keeps the workflows on the flag that detects drift. The committed `uv.lock`
and `pyproject.toml` are unchanged, and the release workflow's `--no-project`
isolation is unaffected.

## Dependency-ready implementation plans

| Subsystem | Milestone | Status | Implementation plan | Dependencies / handoff note |
|---|---|---|---|---|
| Deployment and packaging | M008 | ready | `plans/implementation/deployment-packaging/008-ci-dependency-lock-discipline.md` | none |

## Active implementation plans

| Subsystem | Milestone | Status | Implementation plan | Handoff note |
|---|---|---|---|---|


## Blocked work

| Subsystem | Milestone | Blocker |
|---|---|---|
| Routing selection | M008 | Acceptance of `plans/adrs/ADR-0005-durable-health-state-repair-and-authentication-failure-durability.md`. The plan is `plans/implementation/routing-selection/008-durable-health-state-repair-and-authentication-failure-durability.md`. No other dependency. |
| Routing selection | M009 | Acceptance of `plans/adrs/ADR-0006-authoritative-terminal-model-withdrawal-ownership.md`, and M008 closed. The plan is `plans/implementation/routing-selection/009-health-effect-ownership-and-terminal-withdrawal.md`. M008 is a hard dependency because both milestones edit `rust/src/health/` and `rust/src/routing/router.rs`. |

Unblock audit (on-prem user access reopening after M012 closure, baseline
`3c7def1e`): M001–M013 closure records remain immutable; no
historical milestone was reopened. M014 is closed after Rustls
0.23.46 passed exact-SHA hosted cargo-deny, CI, and installer jobs.
M015 is closed on `d8fd9c2a`; its independent review findings were
fixed, re-reviewed, and covered by local default/no-default suites and
exact-SHA hosted CI, installer and dependency-audit jobs. Registry audit
found M016 is the only registered successor blocked on M015, so M016 is
promoted to ready. M016 still needs final merge-readiness reconciliation.
Independent
on-prem concurrency performance M001 is ready as nonblocking
diagnostic research: the Pi 5/MMC G3 tail-latency failure is
documented, not waived or treated as a release-blocking
throughput commitment. No unrelated ready/active milestone
is promoted or blocked by this registration.

Explicit user direction opens Shared Provider Profile Contract M001 at baseline `b73eea6bc4ecdb6ce6b8974769b7821cf5e7e036`. The milestone extracts the existing secret-free provider endpoint/wire/auth/discovery/verification metadata into a sibling-consumable sans-I/O crate, keeps all account/credential/routing/runtime ownership in EggPool, and adds reviewed non-fixed OpenCode Go model-wire hints. CodeGG is an operational downstream only and must consume an immutable closure revision; no sibling repository is treated as first-party provider authority.

Shared Provider Profile Contract M002 closure audit: searched the registered Shared Provider Profile Contract plans, the subsystem dependency graph, and the ready/active/blocked registry tables. M002 was the only registered successor in this subsystem and is now closed, so no plan in this subsystem is promoted and the blocked-work table has no entry for it. Nothing else in the registry depends on M002: the Persistence M011 plan, the only other ready plan, is unrelated. The Shared Provider Profile Contract roadmap reaches its stated completion definition and is marked closed. CodeGG consumption of M001 remains unblocked at immutable revision `9ac6a131`; CodeGG consumption of M002 is pinned to the M002 closure revision. `longcat-2.5-preview-free` remains withdrawn upstream and is not EggPool's to restore, so no corrective plan is registered for it.

Persistence M008 closure audit: searched the registered Persistence plans and dependency tables; no future plan depends on M008 and the blocked-work table has no Persistence entry. Nothing was unblocked or promoted. A production storage split or further journal experiment requires a separately approved bounded plan.

Persistence M010 closure audit: searched `plans/implementation/persistence/`, the persistence roadmap dependency graph, and the ready/active/blocked registry tables. No M011 or other registered Persistence successor exists, no plan depends on M010, and the blocked-work table remains empty. Nothing is promoted; no other Persistence plan is eligible. A future split investigation requires an explicit finite history-retention and prolonged analytics-outage admission contract before new work is registered.


Historical M007 blocker assessment at baseline `7e241ad` (`plans/closure/persistence/007-status.md`) predates the scope revision that allowed implementation and local qualification. The implementation disposition is in `plans/closure/persistence/007-implementation-status.md`; current Pi 5/MMC physical closure is `plans/closure/persistence/007-pi5-qualification.md` (rejected; production unchanged).

## Recently closed

| Subsystem / plan | Disposition | Evidence |
| On-prem user access M024 — browser session CSRF delivery and member account surface corrective | closed — the CSRF cookie is delivered on exactly the requests the session cookie is delivered on, so a deep-linked operator's page is actionable; the double-submit token requirement, the server-side origin check, and the transport gate are unchanged; the sign-in throttle bounds an account rather than an (account, source address) pair, and a successful sign-in clears the account bucket only, never the per-peer bucket; one document builder renders every sign-in and member surface, every sign-in response is `no-store`, and a member lands on `/account/landing` that links the member surface; no schema, configuration, dependency, or published API change; ADR-0004 accepted 2026-10-23 with two self-contradicting passages corrected before acceptance | `plans/closure/on-prem-user-access/024-status.md`; implementation `c2ca0a6e`; ADR acceptance `7aa5d2d9` |
| Dashboard M014 — operator surface presentation and accessibility corrective | closed — every reachable shell links the theme stylesheet and a guard proves every variable the stylesheet reads is published by every bundled theme; the status pills are derived from the theme's own semantic tokens and clear 4.5:1 in all fifty bundled themes where all fifty previously failed, worst case 1.42:1 before and 4.53:1 after, and the guard rejects a deliberate regression; the chart surface terminates in a rendered chart or an explicit failure without re-fetching the payload; every chart canvas has an accessible name, the chart shell and the refresh indicator are live regions, and the skip link is the first focusable element; the retired frontend surface is removed behind a guard that fails on reintroduction; the asset manifest matches the working-tree bytes and the oracle divergence record carries one reason per divergence; the paired browser disposition remains outstanding because it needs the frozen oracle worktree and a browser | `plans/closure/dashboard/014-status.md`; implementation `2db8265a` |
| Request admission and wire M008 — remote compaction capability contract | closed — a compact request against an installation with no eligible provider surface fails with a typed capability outcome and consumes no provider attempt; the ordinary selection failure stays distinguishable; the bundled profile advertises no capability; nine new tests, each guard shown failing on a deliberate revert | `plans/closure/request-admission-wire/008-status.md` |
|---|---|---|
| Operations CLI M003 — watchdog start-guard ownership correction | closed — a lock that names no owner is reclaimable, so a publication that never completed no longer wedges the watchdog forever, while a lock whose owner cannot be determined is still held and left in place; the reclaim re-reads the moved file and restores a lock that gained an owner, and the publisher confirms the path still names its own file before it claims ownership; a lock that vanishes during the removal decision is free rather than reported as held; the file identity outside unix now distinguishes two files of the same length; 9 tests in the lock target, each guard shown failing on a deliberate revert; the lock file format, the PID file behaviour, and the CLI contract are unchanged | `plans/closure/operations-cli/003-status.md` |
| Routing Selection M007 — durable backoff scope and reason correction | closed — the model-scoped durable backoff row records the reason the live path classified instead of a fixed `model_unavailable`, so two unrelated failure classes no longer collapse onto one persisted reason, and it carries the model's own escalation rung instead of the account's, so a run of account-scoped 408 or 5xx failures no longer inflates the model-absence ladder a restart restores; the hydration restores a durable row by its scope rather than by its reason value; the model's counter gained a read-only accessor and the newly-live hydration diagnostic now honours the tracing subscriber; the backoff policy table is unchanged; three new durable-effects tests, each guard shown failing on a deliberate revert | `plans/closure/routing-selection/007-status.md` |
| Request admission and wire M009 — terminal detail redaction coverage correction | closed — the persisted terminal detail's redaction projection now covers the classes M007's closure record claimed: a labeled credential in the JSON spelling as well as the unquoted spelling, a standalone `bearer`/`basic`/`token`/`digest` scheme word, a URL authority carrying userinfo credentials consistently with the repository's other redactor, and a case-insensitive vendor token prefix; ordinary URLs, `mailto:` fragments, bare `word@word`, and compound identifiers still survive byte-identically; seven new redaction tests assert the exact redacted output and the absence of the removed secret; the durable schema, the wire contract, and the transport-robustness work are unchanged | `plans/closure/request-admission-wire/009-status.md` |
| Deployment and packaging M007 — release action-pin enforcement and guard correction | closed — every `uses:` in the release-artifact qualification workflow is pinned to the repository's canonical immutable SHA, and the release-workflow validator now enforces the pin rule on the qualification contract as well as on the release contract, so a mutable tag cannot be introduced without ordinary CI failing; the version-glob guard reads the covered workflows, the release catalog, and the Cargo version instead of comparing hardcoded strings, and the artifact-hygiene guard derives the retained evidence set from the ignore rule; the tracked artifact corpus is unchanged at 153 paths and the tracked-and-ignored overlap stays zero | `plans/closure/deployment-packaging/007-status.md` |
| Operations CLI M002 — local process lifecycle safety | closed through M003 — the record's own lock rule was reopened by review and is superseded by the corrective closure; an undeterminable lock owner is now a held lock whose file is left in place, and the stale decision is re-verified against the file's device/inode identity immediately before removal, so two concurrent starts can no longer both conclude the lock is stale; the genuine stale case still recovers; nine `Instant::now() + duration` sites now go through one checked helper returning a typed `ProcessError::Deadline`, and each caller converts it into its own existing failure category (the only new public error variant in the milestone); non-finite and out-of-range seconds are rejected through the caller's existing error path instead of a `NaN` reading as a zero delay or an infinity saturating to `u64::MAX`; the start path adds no blocking wait, no new dependency, and no new concurrency primitive; the PID file format, the `LockHeld` contract, and the server's shutdown/drain/quiesce sequence are unchanged; 14 new tests across 4 targets, each work package's guard shown failing on a deliberate revert | `plans/closure/operations-cli/002-status.md` |
| Routing selection M006 — backoff policy adoption and health-reason vocabulary unification | closed through M007 — the record's escalation statements are superseded by the corrective closure, which also closes the durable scope and reason defect; the live failure path now takes its backoff duration from the bounded policy that already existed in `rust/src/health/backoff.rs`, and the reason vocabulary is written from one total typed mapping instead of nine hand-written string literals; escalation reuses the `HealthManager` per-account and per-model counters that already existed, so no new state was introduced and no await entered the selection lock; the persisted reason strings are byte-identical to the pre-change set, so a row written before this change stays readable; two live behavior changes follow from adopting the policy as written (a quota exhaustion and a rate limit without a usable hint now produce a timed backoff) and the frozen C001 compatibility fixture still matches byte-for-byte; the durable deadline clamp now derives its ceiling from the policy cap; 18 new tests across 4 targets, each guard shown failing on a deliberate revert; no config/API/schema/dependency/map/lock change | `plans/closure/routing-selection/006-status.md` |
| Persistence M011 — post-M010 roadmap and documentation reconciliation | closed — the M008 detailed body now carries one status declaration instead of two, the M009 and M010 detailed bodies read closed rather than active and closing, and the roadmap's cross-cutting requirement and the M010 investigation record no longer pin a superseded schema version; the targeted guard grew from 3 tests to 13 and seven deliberate perturbations each failed the guard that owns them; zero production Rust/Cargo/schema/migration/API/config/artifact change; no accepted closure record or physical artifact changed; no ADR-0002 exists or is implied; the Persistence roadmap stays top-level active with no successor registered | `plans/closure/persistence/011-status.md` |
| Deployment and packaging M006 — release artifact version derivation and repository artifact hygiene | closed through M007 — the record's action-pin statements and cited glob evidence are superseded by the corrective closure; the two qualification workflows derive the release version from the repository's own version authority instead of the `0.8.0` literal (7 sites in one workflow, 2 in the other), the release-workflow validator now runs in ordinary CI and covers all three release workflows, and the qualification artifact path chose RETENTION: the 148 tracked files stay tracked, the inert ignore rule was narrowed, and the tracked∩ignored overlap went from 148 paths to 0; at least six closure records and two committed tests depend on a retained artifact, so the plan's untracking stop condition was not triggered; the parity defaults now resolve in a clean clone with the harness assertions and recorded baselines unchanged; 25 new tests across 3 files, each guard shown failing on a deliberate perturbation; zero production Rust/Cargo/manifest-schema change | `plans/closure/deployment-packaging/006-status.md` |
| Planning governance M002 — review index, ownership, and census reconciliation | closed — the CLI census was corrected from 34 to 35 (the undocumented `users` variant), the migration chain from v1–v54 to v1–v63, the route topology from 28 to all 69 registered routes, the workflow index from 21 to all 30 top-level runtime modules, and the wire ownership paragraph no longer credits the twelve root facades with kernel semantics; the managed-user subsystem gained a deep dive, a review-index entry, an ownership row, and a workflow-index entry; five read-only census guards were written first, confirmed to fail against the pre-correction tree, and each shown failing on its own deliberate perturbation; zero production Rust/scripts/docs/plans change | `plans/closure/planning-governance/002-status.md` |
| Routing selection M004 — quota, recovery-interval, and health-absence fail-closed corrections | closed — the hard-cap quota gate excludes an account with no quota record instead of admitting it unchecked; an absent `HealthManager` is no longer read as a healthy account; the missing-account recovery interval rejects a non-finite or non-positive value and both its readers now read one floored value; a non-finite quota observation timestamp is rejected before any counter moves; no exclusion reason code, scorer API, config, schema, or migration change; four of the six new guards fail against the pre-correction code and two are non-regression guards; strict workspace clippy clean and the affected targets green; the full serial suite and the `--no-default-features` parity remain outstanding | `plans/closure/routing-selection/004-status.md` |
| Routing selection M005 — durable health effect application and quarantine gating | closed through M007 — the record's model-scoped durable row is superseded by the corrective closure; the generation build path constructs the backoff and quarantine repositories, hydrates the health manager and quarantine before publication, and attaches the quarantine so the eligibility gate is live; the routing path's failure effects are mirrored into the same tables outside the selection critical section, recording the live decision rather than a recomputed one; expired rows are retired on the existing bounded maintenance schedule; a stale row naming an unknown account is skipped and does not fail the generation; a terminal withdrawal is mirrored as a bounded observation rather than a terminal row, recorded as an open finding; M006 is unblocked | `plans/closure/routing-selection/005-status.md` |
| Request admission and wire M007 — terminal detail scrubbing and transport robustness corrections | closed through M009 — the record's redaction-coverage claims are superseded by the corrective closure; the persisted terminal detail is redacted before the write on both the insert and the update path with a deterministic label-, scheme-, vendor-prefix-, and entropy-bounded rule that replaces removed material with a marker containing no part of it; an out-of-range HTTP-date clock field is no longer accepted as a retry hint and the fixed policy backoff stays in charge; the authorization scheme token is matched case-insensitively with the constant-time credential comparison and the loopback exemption unchanged; no client-visible status, route, or body change; strict workspace clippy clean and the affected targets green; the full serial suite and the `--no-default-features` parity remain outstanding | `plans/closure/request-admission-wire/007-status.md` |
| Operations CLI M001 — operator status projection fidelity | closed — the CLI's offline status fallback is built from the same typed snapshot structs the server publishes, so the two projections cannot drift; both the online and the offline status paths route through a real redaction projection instead of a passthrough; provider ordering, the provider bound, the provider-id length bound, and the unavailable semantics are preserved; the published status schema, the CLI command tree, and the human-readable output wording are unchanged; three production reverts were demonstrated to fail the new guards; strict workspace clippy clean and the affected targets green; the full serial suite and the `--no-default-features` parity remain outstanding | `plans/closure/operations-cli/001-status.md` |
| Dashboard | M013 — observation projection derivation reconciliation | closed — the transcoding summary, the cache read ratio, the time-to-first-token population and storage layer, the routing-policy facts, and the month-label lookup each have one owner consumed by both the dashboard and the CLI; no published field name or shape changed and no configuration key, table, column, index, migration, or dependency was added; two published field values change where the prior derivations disagreed; seven pre-change scenarios were each demonstrated to fail the new guards; strict workspace clippy clean, the `--no-default-features` check passes, and the affected targets green; the full serial workspace suite and the external Python-oracle parity qualification remain outstanding | `plans/closure/dashboard/013-status.md` |
| Shared provider profile contract M002 — OpenCode Go wire hints for the current endpoint table | closed — four placed-but-unhinted models resolve on their reviewed surfaces with `fixed = false` hints; `gemini-4-argon-contributor` and the five unplaced catalog ids asserted unresolved; the six no-longer-placed entries keep their hints with a recorded disposition and reason; dated catalog snapshot plus a drift guard that passes on the committed artifacts and is demonstrated failing on an injected delta; manual, atomic, CI-free snapshot refresh; crate dependency list and sans-I/O property unchanged; no credential, prompt, raw body or cache key in any artifact | `plans/closure/shared-provider-profile-contract/002-status.md`, implementation `ffac612` |
| On-prem user access M023 — short-window team fairness | closed — opt-in bounded token buckets, pre-body 429/Retry-After, modified-binary ordinary-Linux/NVMe no-rule matrix, and three active-throttle runs; no registered successor or capacity SLA | `plans/closure/on-prem-user-access/023-performance-qualification.md`; implementation `84482e9`, qualification tooling `83c0779` |
| On-prem concurrency performance M002 — ordinary-server team baseline | closed — 27 passing Q008 runs across nine cells; Q009 hard-budget, provider-fault, user-disable and restart evidence; bounded competing-I/O and SQLite gate diagnostics; measured host-specific sizing guidance; M023 promoted to ready | `plans/closure/on-prem-concurrency-performance/002-status.md`, artifacts under `artifacts/qualification/on-prem-concurrency-performance/ordinary-server/` |
| On-prem user access M022 — hard-budget agent interoperability | closed — Codex CLI 0.162.1 omitted a Responses output cap; OpenCode 1.18.35 sent configured `max_output_tokens`; stable proof-failure and exhausted-budget codes added; default/no-default verification passes; M023 is now ready after M002 operational evidence | `plans/closure/on-prem-user-access/022-status.md`, implementation `dee65ae` |
| On-prem user access M021 — default member policy and provisioning | closed — transactional future-only default profile selection, CLI/browser creation, explicit no-profile override, additive schema 63 migration, policy visibility and budget-separation behavior pass; M022 subsequently closed; M023 remains blocked on ordinary-server evidence | `plans/closure/on-prem-user-access/021-status.md`, implementation `b76d359`, CLI journey `ee563be`, race test `3f1ca54` |
| On-prem user access M020 — managed transition and headless administration | closed — first member transition, profile assignment and audit are atomic; headless add/show/disable/enable workflow and stale-config auth rejection pass; M021 promoted to ready | `plans/closure/on-prem-user-access/020-status.md`, implementation `8a875a1` |
| On-prem user access M019 — login transport and solo LAN operability corrective | closed — remote plaintext login GET returns no credential form; loopback and configured HTTPS-proxy paths pass; solo SSH tunnel and team proxy guidance documented; M020 promoted to ready | `plans/closure/on-prem-user-access/019-status.md`, implementation `b18a561` |
| On-prem user access M018 — local operator status corrective | closed — status and runtime status use bounded private control-socket readback without a server API key; managed HTTP authorization is unchanged; no successor unblocked | `plans/closure/on-prem-user-access/018-status.md`, implementation `8e2b66a`, CLI fixture `3be5d83` |
| On-prem user access M017 — managed client credential reconciliation | closed — managed local config uses an explicitly supplied active personal token; keyless remote profiles remain read-only; client/helper suites and default/no-default serial workspace suites pass; M022 promoted to ready | `plans/closure/on-prem-user-access/017-status.md`, implementation `d726bce`, `218df45` |
| On-prem user access M016 — final PR #7 integration and merge-readiness reconciliation | closed — local Rust/tooling, Q007, release-doc, browser and installed release-binary evidence pass; exact PR head `3d68c715` hosted CI, Linux/macOS installer and cargo-deny pass; merge held because no maintainer PR review has been submitted; no future plan depends on M016 | `plans/closure/on-prem-user-access/016-status.md`, PR #7, base `9a0a903c`, head `3d68c715`, CI `38721601336`, installer `38721601305`, audit `38721601318` |
| On-prem user access M015 — independent authentication/authorization threat review | closed — independent review findings on hard-cost proof bounds and browser-session access corrected and re-reviewed; default 1,024 / no-default 1,025 tests pass (one ignored each); exact-SHA CI, installer and dependency audit pass on `d8fd9c2a`; no M015 security findings remain; M016 promoted to ready | `plans/closure/on-prem-user-access/015-status.md`, implementation `a526980c`, `deb1622f`, `d8fd9c2a`, security scan `52bd79cc`, CI `38720507683`, installer `38720509034`, audit `38720507720` |
| On-prem user access M014 — Rustls dependency security-advisory corrective | closed — lockfile-only Rustls 0.23.46 patch, unchanged transport feature graph; local default/no-default and 1,009/1,010-test workspace suites, provider TLS (trusted root, wrong-root and hostname rejection), budget/coordinator suites, release build and tooling pass; hosted cargo-deny, CI and both installers pass on `7462263ec482c5fda4bd1a70a9a55b0efc313cd3`; main remains pending PR merge | `plans/closure/on-prem-user-access/014-status.md`, implementation `7462263e`, audit `38671345587`, CI `38671345679`, installers `38671345597` |
| On-prem concurrency performance M001 — Pi 5/microSD concurrent tail attribution | conditionally closed — three paired physical microSD/tmpfs runs identify durable publication/SQLite/storage as the dominant tested four-way tail contributor; no production change or capacity claim; SSD/NVMe is not required | `plans/closure/on-prem-concurrency-performance/001-status.md`, six Q008 reports + manifest under `artifacts/qualification/on-prem-concurrency-performance/`, artifact guard `tests/tooling/test_on_prem_mmc_tail_attribution.py` |
| On-prem user access M012 — physical SBC small-team qualification and final disposition | closed — Pi 5 managed envelope measured (functional 25 cells + 3×30 benchmark batches all pass, 145/145 each, 0 pending/active; Argon2id 51 ms 1-way / 71–73 ms 2-way; 49 named users at 51 ms p50 with spot logins passing; concurrency 1/2/4/8 completes with 8-way saturating at p50 4.1 s); runner managed-auth corrective + 2 contract tests; solo practical, 49 users inexpensive metadata, no 50-concurrent claim; one medium (MMC tails exceed Sep-21 gates, envelope restricted) + six low findings, no high | `plans/closure/on-prem-user-access/012-status.md`, artifacts `artifacts/qualification/on-prem-m012-pi5-functional.json` + `on-prem-m012-pi5-benchmark-run-{1,2,3}.json`, docs `docs/raspberry-pi.md` envelope |
| On-prem user access M011 — admin/member dashboard browser qualification | closed — frozen Python oracle rebuilt and run against head with 54 parity gaps, identical to the pre-M011 baseline (0 new, 0 resolved); on-prem admin/member pages qualified against a Rust-owned approved reference (70 observations, 22 captures) covering both roles, the negative matrix, no-JS sign-in, HiDPI/mobile/themed viewports, and a DOM credential-leak guard; browser sign-in, top-nav overflow at 1280px, unstyled usage pages, missing viewport declarations, unbounded tables, and budget labelling all corrected; 967 serial Rust tests and 175 tooling tests pass at the M011 baseline (re-verified post-integration — see integration note) | `plans/closure/on-prem-user-access/011-status.md` |
| On-prem user access M010 — budget crash recovery and schema-54 restore | closed
| On-prem user access M007 — small-team qualification, migration, and release hardening | conditionally closed — local 964 serial Rust tests + 166 tooling tests; hosted CI `38497388228`, installer qualification `38497388225`, and dependency audit `38497388244` pass; seven synthetic loopback requests complete with no reservations left; physical Pi/Le Potato capacity and schema-54 restore remain unmeasured, so no 49-user or concurrency claim | `plans/closure/on-prem-user-access/007-status.md`, local artifact `artifacts/qualification/on-prem-user-access-m007-local.json` |
| On-prem user access M006 — web administration and personal client integration | conditionally closed — migration 60, session/role/CSRF-fenced admin and member screens, one-time/revocable personal credentials, bounded audit/list reads, 964 serial Rust tests + 166 tooling tests; browser parity captures and M005 estimate/physical evidence carried into M007 | `plans/closure/on-prem-user-access/006-status.md`, implementation `a3f2d00a` |
| On-prem user access M004 — profile-based model authorization | closed — migration 58, exact profile and user policy, concrete-dispatch checks, filtered model discovery and policy ETag; default/no-default compile gates and 957 all-target tests passed; M005 promoted to ready | `plans/closure/on-prem-user-access/004-status.md`, implementation `f537df4`, concurrency fixture `c53bf4d` |
| On-prem user access M005 — durable user/model budgets | conditionally closed — migration 59, transactional holds, retry/terminal settlement, bounded authenticated usage and 962 serial workspace tests; exact output/USD caps, global scope, crash/restore tests and overhead measurement remain explicit conditions; M006 promoted to ready | `plans/closure/on-prem-user-access/005-status.md`, implementation `49dc6ce` |
| On-prem user access M003 — principal propagation and attribution | conditionally closed — typed principal and durable logical request ownership across finite, stream, compact, retries, and selector child; 953 integration tests passed, 1 ignored; two unrelated update unit-test failures and physical measurement carried as explicit conditions; M004 promoted to ready | `plans/closure/on-prem-user-access/003-status.md`, implementation `9af8787a` |
| On-prem user access M002 — browser sessions and route authorization | closed — opaque sessions, CSRF, role-separated managed routes, trusted-proxy transport checks; 947 Rust tests passed, 1 ignored; M003 promoted to ready; SBC hash qualification remains with M007 | `plans/closure/on-prem-user-access/002-status.md`, implementation `087953a3` |
| On-prem user access M001 — admin bootstrap and client credentials | conditionally closed — migration 55, local admin and personal-token lifecycle, legacy-key cutover, 939 workspace tests passed; physical SBC Argon2 qualification carried to M007; M002 promoted to ready | `plans/closure/on-prem-user-access/001-status.md`, implementation `ecdc313` |
| Deployment and packaging M005 — active PATH integration detection corrective | closed — 78-case installer harness (73 baseline + 5 new Gregg/array regressions), Linux Bash and macOS arm64 zsh hosted qualification, full CI; no dependent plan was unblocked | `plans/closure/deployment-packaging/005-status.md`, implementation `576b462d`, CI `38030165909`, shell run `38030165862` |
| Deployment and packaging M004 — shell command discovery and activation corrective | closed — 73-case installer harness, Linux Bash and macOS arm64 zsh hosted qualification, full CI; no dependent plan was unblocked | `plans/closure/deployment-packaging/004-status.md`, implementation `57af5bc` + test hardening `45149d2`, CI `38026958264`, shell run `38026958268` |
| Provider transport M006 — Eggress 1.0.12 dependency refresh and requalification | closed — all 16 registry-resolved Eggress crates at 1.0.12; provider/coordinator/wire and default/no-default workspace gates passed; release binary +520 bytes (+0.0017%); hosted CI and dependency audit green; no plan unblocked | `plans/closure/provider-transport/006-status.md`, implementation `d8be1599` |
| Shared provider profile contract M001 — neutral provider-profile contract and OpenCode Go surface hints | closed — secret-free sans-I/O `eggpool-provider-profile` crate owns the contract and the single canonical bundled asset; EggPool projects all 23 providers onto an identical `ProviderConfig`; 30 reviewed non-fixed OpenCode Go hints; discovery is never credential proof; 930 workspace tests green; no in-repo plan unblocked, CodeGG unblocked at `9ac6a131` | `plans/closure/shared-provider-profile-contract/001-status.md`, implementation `9ac6a131` |
| Persistence M010 — control/projection storage boundary architecture investigation | closed — schema-54 ownership/index inventory and test-only replay prototype complete; 12-to-2 modeled explicit secondary indexes, but 700-to-800 foreground row mutations and no lossless finite outage policy; no ADR or production split | `plans/closure/persistence/010-status.md`, implementation `dfa2a2ab` |
| Persistence M009 — M008 documentation reconciliation corrective pass | closed — current lifecycle docs agree; focused guard 3 passed and full tooling suite 164 passed, 3 skipped; zero production/source/schema diff; M010 subsequently activated | `plans/closure/persistence/009-status.md`, implementation `4bff728c` |
| Persistence M008 — rollback-journal PERSIST qualification and write-amplification attribution | closed — PERSIST/EXTRA failed request-p95 gates on Pi 5/MMC; worker-I/O proxy was about 2.04× control; production remains WAL/NORMAL; no registered successor unblocked | `plans/closure/persistence/008-status.md`, implementation `fac930b9`, physical corpus `artifacts/qualification/m008-pi5-2026-10-03/` |
| Routing selection M003 — bounded exact affinity LRU | closed — slot-indexed exact LRU preserves behavior; 4,096-entry Pi 5 p95 improved about 90×; full default/no-default, strict Clippy, and fmt pass | `plans/closure/routing-selection/003-status.md`, implementation `dc6ea713`
| Routing selection M002 — semantic-affinity exact-LRU cost qualification | closed — Pi 5 evidence showed O(n) hit cost reaches 92.056 μs p95 at 4,096 entries; M003 follow-up qualified and closed | `plans/closure/routing-selection/002-affinity-lru-qualification.md`
| Persistence M007 — dedicated checkpointer qualification experiment | closed — rejected on Pi 5/MMC WAL progress and convergence gates; production topology unchanged | `plans/closure/persistence/007-pi5-qualification.md`, binary `6f0cfd53`, runner corrections `a7ee7a4a`/`6c068681`/`ed3a01a7`/`79f678f5` |
| Provider profile metadata planning/documentation reconciliation C001 — closed-roadmap and source-truth reconciliation | closed — predecessor roadmap terminal; Together canonical `.ai` and legacy `.xyz` source truth prominent; no production/template changes | `plans/closure/provider-profile-metadata-planning-reconciliation/001-status.md`, status transition `a30ddf3d` |
| Runtime Efficiency M003 — dashboard TTFT percentile query qualification | closed — adaptive grouped-frequency/window query for histories >=50k eligible TTFT rows; exact legacy parity; 10k p50 near baseline and 100k summary/writer p50 improved about 37%; default/no-default and release/tooling gates passed | `plans/closure/runtime-efficiency/003-status.md`, implementation `6401ad84`, closure transition `2cad7eba` |
| Runtime Efficiency M002 — catalog refresh projection and lock-tenure cleanup | closed — direct narrow diff/persistence projections, schema-54 parity, forced persistence failure/retry retention, large 600-model fixture, default/no-default full suites and locked release pass; M003 remains ready | `plans/closure/runtime-efficiency/002-status.md`, implementation `93f0a157`, closure transition `5adf6d3f` |
| Runtime Efficiency M001 — provider/wire-resolver hot-path ownership and cache cleanup | closed — borrowed synchronous preparation, equivalent fingerprinting, bounded indexed LRU; default/no-default suites, strict Clippy/fmt, locked release build pass; M002/M003 remain ready | `plans/closure/runtime-efficiency/001-status.md`, implementation `77664694`, correction `c5c33b69` |
| Dashboard M012 — lifecycle closure and documentation polish | closed — terminal planning/docs reconciliation; M001–M012 closed, no registered successor; three stale dashboard ownership paths corrected; zero Rust/Cargo/asset/oracle diff; merge-head CI and dependency audit green | `plans/closure/dashboard/012-status.md`, documentation `bdd22aef`, hosted CI `37090882257`, dependency audit `37090882237` |
| Provider Transport M002 — Eggfetch 0.2.2 transport failure classification adoption | closed — typed upstream classifications adopted without changing EggPool policy; default/test-support/no-default provider fixtures and hosted gates pass | `plans/closure/provider-transport/002-status.md`, implementation `67d6ceb3`, hosted CI `37070421301`, dependency audit `37070423689` |
| Provider profile metadata corrective M001 — provider template endpoint/source reconciliation | closed — all 23 bundled templates dispositioned against first-party docs; Together retained on canonical `.ai` (plan's `.xyz` correction refuted), OpenCode Go retained on `/zen/go/v1`; 5 template regression tests + authority docs; zero template diff; 815 workspace tests green | `plans/closure/provider-profile-metadata/001-status.md`, implementation `805d6f70` |
| Request admission and wire M006 — external semantic-producer consumer contract | closed — source-neutral canonical requests, surface encoding/options, bounded tool-call accumulation, isolated consumer compile, and EggPool regression gates pass; downstream pin available | `plans/closure/request-admission-wire/006-status.md`, implementation `f05b18b7358d9a4125d1e20c491151eec265e403` |
| Deployment and packaging M003 — config publication ownership corrective | closed — transaction-owned staging + no-clobber publish, concurrent config preserved, final never deleted, 60-case qualification green, zero Rust diff | `plans/closure/deployment-packaging/003-status.md`, implementation `292a1e3f` |
| Dashboard M006 — Full parity qualification and closure | closed — strict report retains nine explicitly accepted source-backed differences; 144 matched captures, eight interaction runs, lifecycle shutdown/restart, and full local Rust/tooling gates passed | `plans/closure/dashboard/006-status.md`, browser manifest `plans/closure/dashboard/006-browser-manifest.json`; implementation `6bfa1781`, `5bb3aba1` |
| Dashboard corrective pass 007 — deterministic recovery-summary qualification | closed | `plans/closure/dashboard/007-status.md`; two consecutive strict empty/populated Reliability runs pass with the startup event inside the summary window. |
| Dashboard M011 — hosted oracle history qualification | closed — checkout now includes the pinned oracle source commit; all hosted CI gates pass | `plans/closure/dashboard/011-status.md`, run `37049147155`, implementation `30b8282a` |
| Dashboard M008 — post-merge strict-CI and planning reconciliation | closed — strict Clippy, no-default, serial Rust, tooling, strict oracle baseline, and hosted CI all pass; M009 ready | `plans/closure/dashboard/008-status.md`, run `37049147155`, implementation `3c40e34f` |
| Dashboard M009 — production module decomposition and ownership cleanup | closed — strict parity, full local/hosted gates pass; M010 is ready | `plans/closure/dashboard/009-status.md`, implementation `caa0b042`, hosted run `37053752173` |
| Dashboard M010 — parity qualification harness decomposition | closed — stable command facade, unchanged oracle/report semantics, browser/lifecycle and full local/hosted gates pass; no successor unblocked | `plans/closure/dashboard/010-status.md`, implementation `06c950f`, hosted run `37057371175` |
| Dashboard M004 — telemetry, routing, reliability, and trace parity | closed | `plans/closure/dashboard/004-status.md` plus additive resolution `plans/closure/dashboard/004-follow-up-007.md`; all M004 routes pass strict qualification. |
| Dashboard M005 — Runtime and Cache Observability Parity | closed | `plans/closure/dashboard/005-status.md` plus additive resolution `plans/closure/dashboard/005-follow-up-006.md`; M006 accepted the four source-truth dispositions, 815 serial Rust tests, 152 tooling tests (one skipped), and 144 matched browser captures. |
| Dashboard M003 — Overview, Accounts, Models, and Model Detail parity | closed — restored bounded current-owner projections; exact populated Model Detail oracle comparison; 32 paired captures; four accepted source-truth differences | `plans/closure/dashboard/003-status.md`, implementation/qualification `03b4988`, `8261e5a` |
| Dashboard M002 — shared shell, interaction, and dashboard API restoration | closed — exact shared-shell projection passed all 28 route/state cells; timeseries APIs matched empty/populated/private cases; eight paired desktop/mobile interaction runs and clean browser checks; 804 serial Rust tests passed | `plans/closure/dashboard/002-status.md`, implementation `da8183d`, paired viewport gate `00c69f5` |
| Dashboard M001 — Python oracle freeze and strict parity substrate | closed — fixed 14-page/8-API oracle, 50 static/theme hashes, reproducible sanitized captures, strict negative tests, 50-cell current-gap report; no production diff | `plans/closure/dashboard/001-status.md`, implementation `3f3d5de`, theme inventory `286b70a` |
| Deployment and packaging M002 — installer transaction and collision corrective | closed — fresh `--force` refuses unowned files, config-seed failure rolls back executable, 46-case qualification green, zero Rust diff | `plans/closure/deployment-packaging/002-status.md`, implementation `02ee2873` |
| Deployment and packaging M001 — binary-first quick installer and ownership cleanup | closed — verified raw-binary fresh path, owner delegation, 42-case deterministic qualification, docs/validators green, zero Rust diff | `plans/closure/deployment-packaging/001-status.md`, implementation `05600504` |
| Provider transport M005 — Eggfetch 0.2.1 / Eggress 1.0.11 refresh | closed — provider, consumer, workspace, dependency, tooling, hosted CI, and audit qualification passed | `plans/closure/provider-transport/005-status.md`, implementation `e08c0e25`, test portability correction `7715a448` |
| Persistence M006 — SQLite NOOP and WAL-reset safety baseline | closed — bundled SQLite 3.53.2; NOOP regression and full default/no-default qualification passed | `plans/closure/persistence/006-status.md`, implementation `c3a72720` |
| Persistence M003 — event-driven checkpoint coordination | closed — rejected by paired Pi/MMC gates; candidate moved multi-second tails into finalization gate wait and was reverted | `plans/closure/persistence/003-status.md`, attempted implementation `b5d145fb`, revert `29bcbb4e` |
| Persistence M005 — M004 evidence and planning reconciliation corrective pass | closed — M004 evidence interpretation and M003 lifecycle corrected | `plans/closure/persistence/005-status.md`, guard `tests/tooling/test_persistence_m004_evidence.py` (17 tests; 14 accepted M004 artifacts machine-checked; zero production Rust diff) |
| Persistence M004 — physical checkpoint qualification and final disposition (periodic strategy rejected on target) | closed — M003's evidence dependency satisfied; narration corrected by M005 | `plans/closure/persistence/004-status.md`, implementation `8113d264` (zero production Rust diff; 14 accepted Pi 5 / ext4 / MMC physical artifacts at `artifacts/qualification/m004/`) |
| Routing selection M001 — ordered quota-scoring and candidate-allocation cleanup | closed | `plans/closure/routing-selection/001-status.md`, implementation `4db8000d` |
| Persistence M001 — bounded passive checkpoint scheduling and target qualification | conditionally closed (periodic claim now disproven by M004; mechanism retained as additive-safe) | `plans/closure/persistence/001-status.md`, implementation `6eae94db` |
| Persistence M002 — single-gate tenure and metrics-flush allocation cleanup | closed | `plans/closure/persistence/002-status.md`, implementation `52494140` |
| Request admission and wire M001 — inference body resource admission hardening | closed | `plans/closure/request-admission-wire/001-status.md`, implementation `a87790ad` |
| Provider transport M004 — typed transport diagnostic evidence | closed | `plans/closure/provider-transport/004-status.md`, implementation `a87790ad` |
| Provider transport M003 — Eggress 1.0.10 adoption and requalification | closed | `plans/closure/provider-transport/003-status.md`, implementation `a87790ad` |
| Provider transport M001 — Eggfetch adapter contract hardening | closed | `plans/closure/provider-transport/001-status.md`, implementation `a87790ad` |
| Plan 250 — EggServe 0.3.0 direct-Tower migration (legacy flat) | closed | `plans/250-eggserve-0.3.0-direct-tower-migration-and-requalification.md`, `7879cbf9` |
| Planning governance M001 | closing → closed on acceptance of `plans/closure/planning-governance/001-status.md` | Plan 251 authorizes; bootstrap files + verification in closure record |
| Server transport M001 — EggServe 0.4.0 adoption and requalification | closed | `plans/closure/server-transport/001-status.md`, implementation `409491ea` |
| Request admission and wire M002 — wire-kernel extraction seam and contract freeze | closed | `plans/closure/request-admission-wire/002-status.md`, implementation `ca3d16b3` |
| Request admission and wire M003 — sans-I/O wire-kernel extraction and EggPool cutover | closed | `plans/closure/request-admission-wire/003-status.md`, implementation `5f373c98` |
| Request admission and wire M004 — fidelity, provenance, and conformance hardening | closed | `plans/closure/request-admission-wire/004-status.md`, implementation `72d6d442` |
| Request admission and wire M005 — planning reconciliation and minor wire cleanup | closed | `plans/closure/request-admission-wire/005-status.md`, implementation `4a1315a1` |

## Unblock audit

Provider-transport M002 is unblocked by the coordinated Eggfetch 0.2.2
release published on 2026-10-02. Upstream now exposes the general native
`TransportFailureKind` / `Error::transport_failure_kind()` seam required by
the roadmap, while admission/timeouts/custom-dialer facts remain on their
existing typed APIs. M002 is registered `ready` at baseline `8db2d16c73cb7bb832c23a2e037d5fe0823021f7`;
Eggress stays 1.0.11 and no separate upstream work remains before downstream
implementation.

Provider-transport M002 is now closed. Hosted CI `37070421301` and hosted
dependency audit `37070423689` passed at implementation head
`6f0cfd532e753a43c454b370453850c55679aab5`. Its completion unblocks no
registered plan; the ready provider-profile metadata C001 plan is independent.
The Provider Transport roadmap was closed after M001–M005; explicit
upstream Eggress 1.0.12 patch adoption reopens it for dependency-ready M006.
M002's Eggfetch 0.2.2 classification closure remains unchanged.

Historical unblock-audit snapshot: Dashboard M008 was registered `ready` at
baseline `299a0b3657667af509742a184e658c14df22d406` after hosted run
`37040025250` reported two strict-Clippy findings. This snapshot is superseded
by M008's closure record and the current tables above. M008 and M011 are now
closed, M009 is closed and M010 is ready, Deployment/Packaging M003 remains closed,
Provider-transport M002 is ready, and Routing-selection M002 remains
evidence-gated.

Post-closure review of deployment-packaging M001 found two concrete invariant
gaps not exercised by its 42-case qualification: fresh `--force` can replace
an unowned regular destination that was never classified as an EggPool owner,
and first-time `init-config` failure after executable commit can return
failure while leaving that executable installed. M001 remains immutable and
closed as historical evidence; corrective M002 is registered `ready` at
baseline `6639d4e6028e7aeebfc46b0c6743b67dc19774c7` to add the missing
regressions, tighten repair attribution, make fresh config seeding rollback
transactional, and reconcile stale roadmap current-state prose. No new ADR or
external dependency is required. Provider-transport M002 remains independently
blocked and is unaffected.


Explicit user direction opens deployment-packaging M001 at baseline `5dc8aeccf06c0f012d6fae81410a805586c9d9d3`. `plans/adrs/ADR-0001-binary-first-quick-install-authority.md` establishes the fresh native curl-install ownership contract; the existing qualified raw release assets/checksum sidecar and standalone provenance/update path satisfy M001's interface dependencies, so `plans/implementation/deployment-packaging/001-binary-first-quick-installer.md` is `ready`. Provider-transport M002 remains independently blocked and is unaffected.

Provider-transport M001, M003, and M004 are closed. M004 did not depend on
M002 and did not change its upstream API blocker.
Provider-transport M002 remains blocked on an upstream Eggfetch typed
classification interface and is not promoted to an implementation plan.
Request-admission-wire M001 is closed after consuming the stable
server-transport interface. Explicit user direction reopened that subsystem for
the wire-kernel extraction sequence: M002 is closed (`ca3d16b3` seam +
`wire_extraction_contract`/`wire_kernel_boundary` corpus); M003 is closed
(`5f373c98` crate + cutover); M004 is closed (`72d6d442` fidelity/provenance/
conformance, additive only). Explicit user direction opens M005 as a bounded
corrective/polish pass for planning reconciliation plus the two low-severity
cleanup findings recorded by M004. M005 is closed (`4a1315a1` shared classifier
+ `Approximated` reservation + planning reconciliation, 781 default / 782
no-default tests green, no medium-or-higher finding); the
request-admission-wire subsystem is closed with no ready successor. No
provider-transport blocked work is promoted; Provider
M002 remains blocked on upstream Eggfetch API work.

Provider transport M005 is closed (`e08c0e25`, test-only error-source
assertion correction `7715a448`; `plans/closure/provider-transport/005-status.md`).
Eggfetch 0.2.1 preserves its API and does not supply the typed classification
interface M002 requires. M002 remains blocked and no future plan became
dependency-ready in this closure pass.


Explicit user direction opens the persistence performance workstream at the current Rust baseline. Persistence M001 is dependency-ready against the completed Plan 239 diagnostic and Plan 240 design constraints; its physical SBC requirement is operational closure evidence, not a reason to invent a different storage architecture. Provider-transport M002 remains blocked and is unaffected.

Persistence M001 is conditionally closed (`6eae94db` implementation, `plans/closure/persistence/001-status.md`): the bounded passive-checkpoint mechanism, tuning hooks, report plumbing, and host verification are landed. The M004 closure now resolves the physical-evidence condition with a rejection verdict; the M001 mechanism is retained as additive-safe and its performance claim is unfulfilled.

Persistence M002 is closed (`52494140` implementation, `plans/closure/persistence/002-status.md`): gate-tenure and metrics-flush ownership cleanup with row/rebuffer equivalence.

Routing-selection M001 is closed (`4db8000d` implementation, `plans/closure/routing-selection/001-status.md`): ordered quota-scoring and candidate-allocation cleanup with seam parity and twin-router determinism.

Persistence M004 (`8113d264` zero-Rust-diff closure, `plans/closure/persistence/004-status.md`) collected 14 accepted Pi 5 / ext4 / MMC physical artifacts at `artifacts/qualification/m004/` (11 phase-diagnostic runs plus 3 ordinary benchmark runs) against the production M001 mechanism. The 60s/256 candidate maxima were 1709 / 561 / 10 943 ms; bounded matrix candidates (60s/128, 60s/64, 30s/64) and the minimum-cadence 1s/64 stress run all left the foreground tail. Three ordinary `--benchmark-samples 10` runs converged (`pending_requests=0`, `active_reservations=0`, peak RSS ≈ 18.2 MB) with backup / recovery / restart / shutdown / rehash / bounded-maintenance / graceful-shutdown all green. Periodic strategy is rejected on the target class; landed 60s/256 constants untouched per plan §6.2.

Unblock audit (M004 closed): the M004 evidence satisfies M003's hard evidence dependency ("M001 closure evidence must explicitly show the periodic strategy is insufficient"). M005 corrects the M004 record's reading of that evidence: measured-window checkpoint activity is zero in every 30 s/60 s phase run, and the 1s/64 stress run deferred all three in-window checkpoint ticks on the busy foreground gate, so M003 inherits concrete gate-contention evidence rather than in-batch PASSIVE checkpoint evidence. At M005 closure this returned M003 to `proposed`; the current M006/M003 planning state is recorded below.

Unblock audit (M005 closed, historical disposition): persistence M003 moved
from `ready` back to `proposed` because its architecture review and dedicated
`003` implementation plan did not yet exist. That M005 condition has now been
addressed by the registered M003 plan, but new M006 research exposed a prior
hard dependency: the locked SQLite 3.50.2 baseline predates true
`wal_checkpoint(NOOP)` and the WAL-reset fix. M003 is therefore currently
`blocked` on M006 rather than `proposed` or `ready`. Routing-selection M002
stays evidence-gated and Provider-transport M002 remains blocked on the
upstream Eggfetch typed classification interface.


Persistence M005 is **closed** (`plans/closure/persistence/005-status.md`,
guard `tests/tooling/test_persistence_m004_evidence.py`, 17 tests green). It
preserved the immutable M004 closure, machine-checked the committed artifact
deltas — 14 accepted artifacts, zero in-batch checkpoint ticks in every 30 s and
60 s phase run, three in-window ticks all `gate_busy` in the 1s/64 stress run —
corrected the current roadmap/architecture statements that had carried
cumulative counters into the measured window, and reserved local
implementation-plan number 003 for the future event-driven checkpoint
milestone. The M004 closure's own accepted-artifact count and
cumulative-vs-delta narration remain historical text; M005's closure record is
the authoritative correction. Zero production Rust change in the pass.


Persistence M006 is registered as the dependency-ready engine-safety prerequisite
for M003. Repository and upstream research found that the current
tokio-rusqlite 0.7 / rusqlite 0.37 / libsqlite3-sys 0.35 graph bundles SQLite
3.50.2, which predates the 3.51.0 NOOP checkpoint mode and the 3.51.3 WAL-reset
fix. M006 changes no checkpoint scheduling policy; it must establish a
true-observational NOOP and fixed bundled engine before event-driven checkpoint
coordination is handed off.


Persistence M003 was unblocked after M006 closed and received paired physical
qualification on a Raspberry Pi 5 / ext4 / MMC target. The event-assisted
candidate reduced foreground publication COMMIT maxima to 349–376 µs but
introduced 1.88–3.30 s finalization gate waits in all three runs; this fails
M003's explicit no-tail-transfer gate. Its runtime implementation was reverted
(`29bcbb4e`), and M003 is closed with a rejected outcome in
`plans/closure/persistence/003-status.md`. No successor is eligible from this
closure. Any future checkpoint redesign must be separately planned and must
address storage stalls without transferring them to foreground gate wait.

Explicit user direction opens Provider transport M005 as a dependency-ready
patch refresh at baseline `869964c236cfd985a771beeb0ee815df3e1cf601`. It upgrades
`eggfetch-core 0.2.0 -> 0.2.1`, resolves `eggfetch-http-connect` to 0.2.1,
and upgrades the live Eggress family `1.0.10 -> 1.0.11` under the existing
listener-free outbound/typed-error/default-no-default contracts. Upstream
Eggfetch 0.2.1 is documented as runtime/API-equivalent to 0.2.0; Eggress
1.0.11 contains substantive parser/timeout/redaction/SSH/protocol fixes and
therefore requires full provider-transport requalification. EggServe needs no
parallel plan: `eggserve-server 0.4.0` is already the latest published line.
Provider M002 remains blocked and is not promoted by M005.

Unblock audit (deployment-packaging M001 closed, implementation `05600504`,
`plans/closure/deployment-packaging/001-status.md`): M001 had no hard
dependencies and consumed only already-stable raw/checksum and provenance/update
interfaces. Closure promotes no blocked work. Provider-transport M002 remains
independently blocked on the upstream Eggfetch typed classification interface.
Routing-selection M002 stays evidence-gated. No deployment-packaging successor
is registered; future hardening (attestations, system/root distribution,
additional targets) requires new bounded plans.

Unblock audit (deployment-packaging M002 closed, implementation `02ee2873`,
`plans/closure/deployment-packaging/002-status.md`): M002 is a corrective to
closed M001 with no hard dependencies; it consumed only the stable ADR-0001
ownership/failure invariants and the M001 binary-first baseline. Closure
promotes no blocked work and unblocks no future plan. Provider-transport M002
remains independently blocked on the upstream Eggfetch typed classification
interface. Routing-selection M002 stays evidence-gated (no affinity workload
measured). Persistence has no eligible successor. No deployment-packaging
successor is registered; future hardening requires new bounded plans.

Unblock audit (deployment-packaging M003 closed, implementation `292a1e3f`,
`plans/closure/deployment-packaging/003-status.md`): M003 is a corrective to
closed M002 with no hard dependencies; it consumed only the stable ADR-0001
ownership/failure invariants and the M002 binary-first + guarded-rollback
baseline. Closure promotes no blocked work and unblocks no future plan.
Provider-transport M002 remains independently blocked on the upstream Eggfetch
typed classification interface. Routing-selection M002 stays evidence-gated (no
affinity workload measured). Persistence has no eligible successor.
Dashboard M001-M006 are now closed with no successor registered.
No deployment-packaging successor is registered; future hardening
(attestations, system/root distribution, additional targets) requires new
bounded plans.


Explicit user direction reopened request-admission-wire after M005 closure for M006 at baseline `8067ad3d1eef5a40ae6e300923d8be3b75437d26`; M006 is now closed at `f05b18b7358d9a4125d1e20c491151eec265e403`. The work remains bounded to the extracted `eggpool-wire` API and does not reopen provider transport, routing, accounts, persistence, or public HTTP behavior. `eggpool-wire 0.1.0` is already on crates.io; this milestone neither released a new version nor changed versioning policy. The downstream CodeGG adoption dependency is satisfied and may proceed using the immutable pin. No future in-repository plan was registered as blocked on M006, so no other blocked row was promoted; provider transport retains its independent upstream blocker and dashboard is closed.


Explicit user direction opened the provider-profile metadata corrective at baseline `c17a55218b2810791fcf6f3136b8805becfa27c1`. Its planning-time Together drift premise (`api.together.ai` versus `.xyz`) was refuted at M001 closure: `.ai` is canonical and `.xyz` is a legacy alias. OpenCode Go `/zen/go/v1` was retained. This historical premise is superseded; the immutable closure is authoritative. M001 is closed and did not reopen provider transport, routing, request-admission/wire, persistence, or publication work.

Unblock audit (provider-profile-metadata M001 closed, implementation
`805d6f70`, `plans/closure/provider-profile-metadata/001-status.md`):
execution-time review of canonical `docs.together.ai` refuted the Together
drift premise — the bundled `api.together.ai/v1` is the current first-party
value and the `.xyz` host is a legacy alias — so no template correction was
applied and the regression surface locks the confirmed compositions
instead. The roadmap's completion definition is met with no unresolved
medium-or-higher finding and no registered successor. No blocked work in any
subsystem depended on M001 (provider-transport M002 stays upstream-blocked,
 dashboard is closed with no successor, routing-selection M002 stays
evidence-gated), so this closure promotes nothing and unblocks no future
plan; the two low deferred discovery-probing items (Fireworks/Alibaba
models paths) require new bounded plans with live verification if pursued.
oracle is commit `c23a70961f4b7858fdb0264cfb27b7ea26a8a334`. M001-M006 and
corrective pass 007 are closed; the complete sanitized oracle, strict
comparator, matched browser evidence, and closure/unblock audit are recorded in
the dashboard closure records. M006 retains nine explicitly accepted
source-backed differences and the strict report remains `gaps`, with zero M004
differences. The 144-capture matched browser manifest, eight interaction runs,
bounded shutdown/restart, and full local Rust/tooling verification are recorded
in `plans/closure/dashboard/006-status.md`. No dashboard work is blocked on
M006, no successor remains registered, and no plan is newly eligible from this
closure. Provider Transport M002 and Routing Selection M002 retain their
unrelated independent blockers/evidence gates.


Explicit user direction opens a documentation-only provider-profile metadata reconciliation at baseline `43c987ea458bd563d5108fd8051ad31185704bb0`. The accepted technical outcome remains M001 closure `805d6f70`: canonical Together `api.together.ai/v1` retained, OpenCode Go `/zen/go/v1` retained, zero template diff, two low discovery uncertainties deferred. C001 exists only to reconcile the predecessor roadmap's stale top-level `active` state, the registry's active/no-successor contradiction, and planning-time Together `.xyz` wording that was superseded by execution-time first-party evidence. No provider/template/runtime work is reopened.

Unblock audit (dashboard M012 closed, documentation `bdd22aef`, `plans/closure/dashboard/012-status.md`): At that audit point M012 was a terminal planning/documentation pass with no hard dependency beyond the already-closed M001–M011. It promoted nothing and unblocks nothing. Dashboard M001–M012 are closed, the dashboard roadmap is closed, and no dashboard plan remains in the dependency-ready, active, or blocked tables. Persistence M007 stays blocked for the same independent reason: paired physical aarch64 Pi-class MMC control/candidate evidence is an operational requirement no planning change can satisfy. Routing-selection M002 was subsequently measured on the current Pi 5, promoting M003; M003 is now closed after exact-LRU implementation and Pi 5 qualification. Provider Transport M002 stays closed and its upstream blocker is historical. The only remaining ready row is the independent provider-profile-metadata planning/documentation reconciliation C001, which M012 does not affect. M012's closure record carries three out-of-scope documentation findings, none of them a dashboard defect: one medium (`docs/thinking.md` still describes Python-era thinking counters, `GET /api/stats/thinking`, and coordinator/health Python members that have no Rust owner) and two low (`docs/rust-dashboard-qualification.md` does not mention the frozen Python oracle runner; `tests/tooling/test_release_docs.py` invokes `uv run` without `--frozen` and so rewrites the committed `uv.lock` `requires-python`). Each requires a new bounded plan outside this subsystem.


Unblock audit (routing-selection M003 closed, implementation `dc6ea713`,
`plans/closure/routing-selection/003-status.md`): M003 had no downstream
blocked implementation dependency. The active implementation-plan inventory is
empty; dependency-ready and blocked-work tables are empty. Persistence remains an
active roadmap because its checkpoint tail is unresolved, with M007 rejected on
Pi 5 WAL progress/convergence evidence and no successor registered. Deployment
and packaging has no ready successor. Provider profile metadata C001 and Runtime
Efficiency M001–M003 are closed. No additional existing plan became eligible.


Explicit user direction opens Persistence M008 at baseline `849dfd5fd26afc027fc1f00316dea70d62973a52`. M007's Pi 5/MMC rejection is the hard evidence dependency: the dedicated PASSIVE candidate removed foreground latency but failed WAL progress/convergence. M008 therefore tests a different journaling architecture rather than another checkpoint schedule. The ready plan keeps one connection/gate/worker, compares WAL/NORMAL control with isolated PERSIST/EXTRA candidate databases, and gathers separate Linux worker-write attribution evidence without contaminating the latency corpus. Production remains WAL/NORMAL; positive evidence requires a later ADR/adoption plan, while rejection may justify separately bounded control/outbox/analytics persistence research.


Explicit user direction opens Persistence M010 at baseline `3efd650e63eef4999e450bdef738af9e54971d4b`. M008's rejected PERSIST/EXTRA result and worker-write attribution are the hard evidence basis. M010 is bounded to ownership inventory, durable-outbox/projector/backup/failure design, and an isolated test-only transaction-shape prototype. It does not add a production database, projector, schema migration, API/config surface, or accepted ADR. If a viable boundary survives, ADR-0002 may be created only as proposed; production implementation requires later explicit acceptance and separate milestones.


Explicit user direction opens Persistence M011 at baseline `9c1d86bc17a2a91b2ed8441bf23bd3e80b6603ed`. The accepted M009/M010 technical outcomes are unchanged: M009 closed the first lifecycle-doc drift; M010 rejected the control/outbox/analytics split under the current history/outage contract and proposed no ADR. M011 is restricted to current-authority roadmap/docs status reconciliation plus a targeted guard. It does not reopen storage architecture, retention policy, SQLite tuning, or production behavior.

Shared provider profile contract M001 unblock audit (closed, implementation
`9ac6a131`, `plans/closure/shared-provider-profile-contract/001-status.md`):
searched `plans/implementation/`, `plans/subsystems/`, and the dependency-ready,
active, and blocked registry tables. No registered plan declared shared provider
profile contract M001 as a hard, interface, or operational dependency, and the
blocked-work table has no entry for this subsystem, so nothing was promoted. The
roadmap's only milestone is closed, which closes the roadmap terminal with no
successor. The one gated consumer, CodeGG, lives in a downstream repository and
was recorded as operational: it is unblocked by immutable revision
`9ac6a1318e8db3c034b5ab54987317752d5ffea6`, which it should pin (the crate is
unpublished and carries no semver promise). Per the source plan, CodeGG's product
catalog — like any sibling repository — is not first-party provider authority;
bundled profile facts remain reviewed transcriptions of current provider
documentation.

Shared provider profile contract M002 registration (baseline `e48f021e`,
`plans/implementation/shared-provider-profile-contract/002-opencode-go-current-endpoint-wire-hints.md`):
M002 re-opens the terminal roadmap on evidence, not preference. The M001
reviewed table is a hand transcription of a first-party page that changes; by
2026-10-22 the current first-party endpoint table placed four models the
canonical wire map did not hint, and the live public catalog confirmed that two
of them — `beam-free` and `minimax-m3.1-flash-free` — are served. A downstream
consumer selecting either free route therefore fails closed at
`wire_unresolved`. That is a finding about this repository's shared profile,
not a consumer defect, and it is recorded on the CodeGG side, at that
repository's `CodeGG:plans/closure/harness-lab/evidence/route-audit-20261022.json`
rather than in this one.

M002's own unblock audit: searched `plans/implementation/`,
`plans/subsystems/`, and the dependency-ready, active, and blocked registry
tables. No registered plan declares shared provider profile contract M002 as a
hard, interface, or operational dependency, and the blocked-work table has no
entry for this subsystem, so **M002's registration promotes nothing**. The
consumer that M002 unblocks is CodeGG, which lives in a downstream repository
and consumes an immutable revision; it is unblocked by the M002 **closure**
revision, not by the plan's existence. The M001 audit's statement that the
roadmap closed terminal with no successor was true at that audit point and is
recorded as historical, not rewritten.

M002's scope excludes adding a wire surface: `gemini-4-argon-contributor` is
placed by the first-party table on a Google Generative AI surface that
`eggpool-provider-profile` does not model, so it stays unresolved rather than
being mapped onto an existing surface, and the catalog ids with no first-party
placement stay unresolved for the same reason.
`longcat-2.5-preview-free` is withdrawn upstream and answers HTTP 402; a hint
for an unavailable model is harmless, and restoring its availability is not
this repository's to do.

Unblock audit (deployment-packaging M005 closed, implementation `576b462d`,
`plans/closure/deployment-packaging/005-status.md`): M005 is a polish
corrective to closed M004 with no hard dependencies beyond M004's closed
installer/ownership baseline and no interface/operational dependency on any
sibling repository. Searched `plans/implementation/deployment-packaging/`, the
deployment-packaging roadmap dependency graph, and the ready/active/blocked
registry tables. No registered plan declares M005 as a hard, interface, soft,
or operational dependency, and the blocked-work table has no deployment-packaging
entry. Closure promotes no blocked work and unblocks no future plan. Persistence
M011 remains independently ready and is unaffected. Provider transport, routing
selection, runtime efficiency, shared provider profile contract, and dashboard
remain closed with no successor. No deployment-packaging successor is registered;
future hardening (attestations, system/root distribution, additional targets)
requires new bounded plans.

Unblock audit (on-prem user access M001 conditionally closed, implementation
`ecdc313`, `plans/closure/on-prem-user-access/001-status.md`): searched
`plans/implementation/on-prem-user-access/`, the subsystem dependency graph,
and the ready/active/blocked registry tables. M002's hard M001 credential and
schema dependency is satisfied by the accepted closure and was promoted to
ready. M003 remains blocked on M002 and its stable principal/scope contract;
M004 remains blocked on M003 attribution; M005 on M004 policy gates; M006 on
M005 durable budgets; M007 on M006 and physical qualification. No other
registered plan depends on M001. The M001 Argon2 SBC measurement condition is
carried into M007, so promotion of M002 does not assert physical sizing.


Unblock audit (on-prem user access M006 conditionally closed, implementation
`a3f2d00a`, `plans/closure/on-prem-user-access/006-status.md`): searched
`plans/implementation/on-prem-user-access/`, the roadmap dependency graph, and
the dependency-ready, active, and blocked registry tables. M007's hard M006
dependency is satisfied and it is promoted to ready. Its physical SBC and
ordinary-host measurements remain operational evidence required for M007
closure; they do not block preparatory compatibility and recovery work. No
other registered plan declares M006 as a dependency, so no other plan was
promoted. M005 estimate/global-scope/recovery findings and M001 Argon2 hardware
measurement remain carried conditions.


Unblock audit (on-prem user access M007 conditionally closed, implementation
`a3f2d00a`, `plans/closure/on-prem-user-access/007-status.md`): searched
`plans/implementation/on-prem-user-access/`, the roadmap dependency graph, and
the dependency-ready, active, and blocked registry tables. M001–M007 are
terminal and no successor or downstream plan declares this workstream as a
dependency. No plan was promoted. The physical SBC, exact schema-54 restore,
dashboard parity, M005 budget, and hosted-CI conditions remain visible in the
closure; no named-user capacity claim is supported by the local synthetic run.


Unblock audit (on-prem user access M011 closed,
`plans/closure/on-prem-user-access/011-status.md`): searched
`plans/implementation/on-prem-user-access/`, the roadmap dependency graph, and
the dependency-ready, active, and blocked registry tables. M011 left the
dependency-ready table. At M011's own baseline its hard-dependency note held
(M008 still ready, M009/M010 blocked on it); on integration into this branch,
M008/M009/M010/M013 are all accepted closures, so M012's hard dependency
(M008–M011 accepted closures) is now fully satisfied and M012 stays blocked
solely on its operational requirement (provisioned physical SBC and reference
ordinary Linux target). No plan was promoted: nothing else declares these
milestones as a dependency. M006's recorded browser-parity condition is
satisfied additively here; the immutable M006 record is not retroactively
altered.

Integration note (M011 cherry-picked as `d495d483` from
`feat/auto-20261014-e526194d` onto this branch): the pick's registry and
roadmap edits were reconciled to current truth (M008/M009/M010/M013 already
closed here); the `render/admin.rs` budget-form conflict resolved in favour of
the mode-aware M008 form (the M011 labels predated enforcement modes) while
keeping M011's `on_prem_page`/`scroll_table` structural fixes; the M011 plan
file moved `ready` → `implemented` (activation was skipped in the separate
session — recorded here, not rewritten).

Unblock audit (on-prem user access M012 blocked closure,
`plans/closure/on-prem-user-access/012-status.md`): searched
`plans/implementation/on-prem-user-access/`, the roadmap dependency graph, and
the dependency-ready, active, and blocked registry tables. M012's hard
dependency (accepted M008–M011 closures) is satisfied; the operational
requirement (provisioned physical SBC and reference ordinary Linux target)
is not, so M012 stays blocked and the roadmap stays `active`. The closure
pass lands only the tooling boundary-contract suite
(`tests/tooling/test_on_prem_sbc_qualification.py`, 9 tests) with zero
production diff; no plan is promoted and nothing else declares these
milestones as a dependency. No capacity, latency, Argon2, RSS, WAL, or
named-user claim is accepted. When hardware is provisioned, resume at plan
work packages 2–4 with predeclared gates.

Unblock audit (on-prem user access M012 closed,
`plans/closure/on-prem-user-access/012-status.md`): searched
`plans/implementation/on-prem-user-access/`, the roadmap dependency graph, and
the dependency-ready, active, and blocked registry tables at this commit.
M012's hard dependency (accepted M008–M011 closures) was already satisfied;
the operational requirement is now met with Pi 5 managed evidence
(functional + 3× benchmark + Argon2 + 49 named users + 1/2/4/8 concurrency),
so M012 moves `blocked` → `implemented`/`closed` and the on-prem roadmap
moves `active` → `closed`. This pass lands the runner managed-auth corrective,
two contract tests (11 total), four Pi 5 artifacts, and the `raspberry-pi.md`
envelope with zero production diff. No plan is promoted: nothing else
declares M012 as a dependency, M013 is already closed, and the only ready row
(Persistence M011) is independent and unaffected. The blocked-work table is
now empty. Future hardware-class or higher-concurrency claims require new
bounded plans; this closure record is not to be rewritten.

Unblock audit (on-prem concurrency performance M001 conditionally closed,
`plans/closure/on-prem-concurrency-performance/001-status.md`): searched
`plans/implementation/`, the subsystem dependency graph, and the dependency-
ready, active, and blocked registry tables. No registered plan declares M001
as a hard, interface, soft, or operational dependency. No plan is promoted or
unblocked. On-prem user-access M016 is ready after M014/M015 closure and still
requires final-SHA gates; this optional diagnostic workstream has no edge
to that sequence. The concurrency-performance roadmap remains active for any
future separately scoped controlled-I/O/media/budget work; none is registered.

Unblock audit (on-prem user access M017 closed, `plans/closure/on-prem-user-access/017-status.md`): searched `plans/implementation/on-prem-user-access/`, the roadmap dependency graph, and the dependency-ready, active, and blocked registry tables. M022's sole hard dependency, M017, is closed and its shared client/wire and hard-budget proof interfaces were already stable, so M022 moves `blocked` → `ready`. M020 remains blocked on M019; M021 remains blocked on M020; M023 remains blocked on M022 and its ordinary-server operational evidence. No other registered plan became eligible. M018 and M019 were already independent ready plans. No 49-user, team-capacity, or generated-output cap claim is introduced by this audit.

Unblock audit (on-prem user access M018 closed, `plans/closure/on-prem-user-access/018-status.md`): searched the on-prem implementation plans, roadmap dependency graph, and dependency-ready, active, and blocked registry tables. No registered plan has a hard M018 dependency, so no blocked row is promoted. M019 and M022 remain ready; M020 remains blocked on M019, M021 on M020, and M023 on M022 plus ordinary-server operational evidence. No capacity or generated-output cap claim is introduced.

Unblock audit (on-prem user access M019 closed, `plans/closure/on-prem-user-access/019-status.md`): searched the on-prem implementation plans, roadmap dependency graph, and dependency-ready, active, and blocked registry tables. M020's hard dependencies M017 and M019 are both closed, so M020 moves `blocked` → `ready`. M021 remains blocked on M020. M023 remains blocked on M022 and ordinary-server operational evidence. M022 remains ready; no other plan is promoted and no capacity claim is introduced.

Unblock audit (on-prem user access M020 closed, `plans/closure/on-prem-user-access/020-status.md`): searched the on-prem implementation plans, roadmap dependency graph, and dependency-ready, active, and blocked registry tables. M021's sole hard dependency, M020, is closed, so M021 moves `blocked` → `ready`. M023 remains blocked on M022 and ordinary-server operational evidence; M022 remains ready. No other plan is promoted and no team-capacity claim is introduced.

Unblock audit (on-prem user access M021 closed, `plans/closure/on-prem-user-access/021-status.md`): searched the on-prem implementation plans, roadmap dependency graph, and dependency-ready, active, and blocked registry tables. M022 was already ready after M017 closed and remains ready; M021 was not a prerequisite. M023 remains blocked on hard M022 closure and the operational ordinary-server M002 evidence; M002's tooling readiness does not satisfy that evidence gate. No newly-ready plan is promoted and no team-capacity claim is introduced.

Unblock audit (on-prem user access M022 closed, `plans/closure/on-prem-user-access/022-status.md`): searched the on-prem implementation plans, roadmap dependency graph, and dependency-ready, active, and blocked registry tables. M023's hard M022 dependency is satisfied, but its source-plan stop condition still requires measured ordinary-server Linux with durable SSD/NVMe storage. The M002 tooling/setup slice is conditionally closed; the attested Ubuntu host exceeded the predeclared I/O-pressure gate before qualification collection, so no capacity measurement exists. The available runner is a Darwin MacBook Pro and cannot supply the missing evidence. M023 therefore remains blocked pending an isolated ordinary-server result. No other registered on-prem user-access plan after M023 is eligible; no capacity claim is introduced.

Unblock audit (on-prem concurrency performance M002 conditionally closed,
`plans/closure/on-prem-concurrency-performance/002-status.md`): searched
`plans/implementation/`, the on-prem concurrency and user-access roadmap
dependency sections, and the dependency-ready, active, and blocked registry
tables. No future plan declares M002 as a hard, interface, or soft dependency.
On-prem user-access M023 names ordinary-server evidence as an operational
requirement, but the evidence remains unmeasured and M023 remains blocked on
its hard M022 dependency. No plan was promoted or unblocked. A future isolated
server run remains required before M023 can use M002 as its operational
capacity evidence.

Unblock audit (on-prem concurrency performance M002 closed,
`plans/closure/on-prem-concurrency-performance/002-status.md`): searched
`plans/implementation/`, the on-prem concurrency and user-access roadmap
dependency sections, and the dependency-ready, active, and blocked registry
tables. M023's hard M022 dependency is closed and its named operational M002
requirement is satisfied by the durable-NVMe host matrix and Q009 lifecycle
evidence, so M023 moves `blocked` → `ready`. No other registered future plan
declares M002 as a hard, interface, or soft dependency. M001 remains
conditionally closed; no production storage optimization is authorized by
this evidence.


Unblock audit (on-prem user access M023 conditionally closed,
`plans/closure/on-prem-user-access/023-status.md`): searched
`plans/implementation/`, the on-prem user-access roadmap dependency graph,
and the dependency-ready, active, and blocked registry tables. M023 has no
registered successor or downstream plan, so no future work is newly
unblocked and no status promotion is required. Its ordinary-Linux
post-change performance comparison remains a closure condition, not a
dependency for any registered plan. No capacity claim is introduced.

M023 performance closure audit (`plans/closure/on-prem-user-access/023-performance-qualification.md`, baseline `83c0779a`): the modified release binary passed its 27-run no-rule ordinary-server matrix and three active-throttle runs on the attested Ubuntu/NVMe host. Reviewed the on-prem implementation plans, subsystem dependency graph, and dependency-ready, active, and blocked registry tables. M023 has no registered successor or downstream plan; no future work is newly unblocked, no blocked row is promoted, and no other plan status changes. The on-prem user-access roadmap is closed with its historical closure records retained. No capacity SLA is introduced.
