# On-Prem User Access M011 — Admin/member dashboard browser qualification

Status: ready

Repository baseline: `c18dcc2051ae4987a2cb95a7cd046b8aff2095ff` on `plans/on-prem-small-team-user-access` (PR #7), schema 60, M001–M007 closure records as of 2026-10-13.

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m011--adminmember-browser-qualification-corrective`

Long-term references:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: polish

## 1. Objective

Admin/member dashboard browser qualification. This is a **new corrective** to the conditionally closed M005/M006/M007 records, not a rewrite of historical closure.

## 2. Readiness and dependencies

**Hard:** none; M006 browser UI, theme and role/session interfaces are
implemented and the corrective can begin now. **Operational:** a
render-capable browser runner is needed for closure; pin the historical
Python oracle only for **pre-existing pages**, because the new admin/
member pages did not exist in the Python implementation.
M008/M009 may change budget wording later; capture CSS/layout and
access controls now, then rerun the affected budget labels at integration.

## 3. Evidence and why prior tests missed it

`plans/closure/on-prem-user-access/006-status.md` reports that matched
screenshots/browser interactions for the new admin/member pages were
not run because a pinned Python oracle worktree and
`EGGPOOL_DASHBOARD_ORACLE_*` environment were unavailable.
`plans/closure/on-prem-user-access/007-status.md` carries that
condition forward. Existing `rust/src/server/dashboard/admin.rs`,
`render/admin.rs`, `render/layout.rs`, `rust/src/server/auth.rs`,
`rust/tests/on_prem_admin_ui.rs` and
`scripts/dashboard_parity/` provide current sources.
The legacy oracle has **no equivalent new admin/member pages**; a
pixel-for-pixel matching requirement for those would be invalid.
Prior unit tests verified HTTP/HTML and authorization, not actual
responsive rendering or browser interaction across themes.

## 4. Non-regressing invariants

- Canonical use is **one administrator on one local/SBC instance**, optional <50 named users. No SaaS, organization tenancy, billing platform, centralized IAM, new database writer/service, built-in WAN/VPN/reverse proxy, or mandatory cloud calls.
- Upstream provider topology, API keys, routing and self-hosted model endpoints remain in TOML/environment; SQLite owns users, personal credentials, profiles, policy, downstream budgets, accounting and audit. Do not place upstream keys in user tables or web administration.
- Maintain generation leases, existing coordinator publication/finalization, a **single SQLite connection/gate/worker with WAL/NORMAL**, canonical wire and transparent streaming, bounded hot-path memory and provider fairness. Do not turn downstream user budget refusal into upstream provider unhealthiness.
- Preserve published OpenAI Chat/Responses/compact and Anthropic Messages behavior, permitted client auth headers, existing client profiles, dashboard themes and local legacy upgrade behavior.
- Keep identity and cost metadata redacted, prohibit secret/password/bearer leakage in logs, HTML snapshots or qualification artifacts, and fail closed rather than silently weaken configured security limits.

## 5. Required qualification work

### A — Correct the visual oracle boundary

Split two independent contracts:
1. **Existing dashboard:** preserve previously accepted Python-vs-Rust
   shell, navigation, charts, theme/CSS and interaction parity on old routes,
   using the pinned frozen Python oracle/manifest and its explicitly accepted
   historical differences; no reopening old M001–M012 dashboard closures.
2. **New pages:** freeze a sanitized Rust-owned approved screenshot/DOM/
   accessibility reference for `/users`, `/profiles`,
   `/admin/audit`, `/my-account`, `/my-account/models`,
   `/usage`, token reveal/reset/expiry states, and login/forced
   password change. Compare these with a stable reference on subsequent runs.
   Do not invent Python page equivalents.

A pinned git revision/asset hash is essential for reproducibility.
Design a small browser manifest using present
`scripts/dashboard_parity/browser.py`, `runner.py`,
`projection.py`, `report.py` rather than embedding a second
visual harness. If the oracle is unavailable, historical-page gate is
explicitly unqualified; new-page independent browser tests can run.

### B — Real browser interaction and negative cases

Test admin and member accounts at 1280x800 and narrow mobile viewport
(and one standard HiDPI where supported), with mouse and keyboard:
session creation/expiry/logout; first-login forced password change;
CSRF errors; user create/disable; profile assign/edit and conflict;
member-only catalog/usage; budget hard-vs-estimated labels;
one-time personal token visibility and refresh; member/agent/
anonymous denial; accessible focus order/forms; error banners;
no-JS ordinary form operation; dark/light and representative Halloy
themes; no horizontal overflow, clipped dialogs, unreadable badges,
unbounded table lengths, or accidental server-secret render.
Use synthetic provider/user labels, never live tokens.

### C — Bounded implementation fixes and guards

Only fix demonstrated local renderer/CSS/semantic HTML issues;
keep `dashboard/render/` pure, not a second session/policy
authority. Add deterministic browser assertions/manifest for
new pages and static guards that prohibit provider passwords/
API tokens in HTML snapshots and require session/CSRF fences.
If defects require unrelated dashboard architecture rewrite, open
separate implementation plan; don't expand this pass.

### D — Documentation / parity disposition

Update current `architecture/deep-dive-dashboard.md`,
`docs/deployment.md`, browser qualification docs and M006
superseding **additive** closure note (in new M011 closure).
Record source-backed intentional design differences rather than
rewriting accepted old oracle output.

## 6. Explicit non-goals

No UI framework migration, provider-TOML web editor, identity redesign,
new Rust stateful frontend, admin/API security work without a failing
test, or claiming Python parity for pages Python never had.

## 7. Ordered work packages

1. Freeze the old-page oracle contract and new-page browser fixture
   manifest. Pin tool versions, reference hashes, accounts and viewports.
2. Run admin/member browser flows, all role negative cases and theme/
   accessibility checks; capture sanitized images and DOM facts.
3. Fix only observed renderer/interaction regressions; rerun legacy-page
   comparison, Rust route/security suites and new-page browser suite.
4. Record explicit absent-oracle condition if old page comparison cannot
   execute; never pretend a green unit suite proves visual parity.

## 8. Failure, cancellation, restart and contention

Browser-session expiry and password reset must immediately deny
new mutations while already-rendered static content remains safe.
Concurrent profile edits surface a conflict without stale last-write
success; token plaintext only appears once and is not cached.
No credentials or raw API prompts in browser traces/fixtures.
The generated reference manifest must be deterministic and bounded.
New browser tooling must use disposable DB/home and loopback fake
provider only; no live config mutation.

## 9. Compatibility

Retain existing themes and dashboard assets, without new CDN,
external JS or front-end service. Preserve the accepted historical
browser parity contract on all pre-existing routes. Make a new
independent golden for the admin/member pages and explain why
old Python pages cannot serve as their oracle.

## 10. Tests and focused commands

Add/extend `tests/tooling/test_dashboard_parity_modules.py`,
`test_dashboard_parity_projection.py`, a narrow new browser
qualification fixture, `rust/tests/on_prem_admin_ui.rs`,
`dashboard_filters.rs`, `on_prem_sessions.rs` and
`operations_o005.rs`. Browser runner entry:
`scripts/qualification_dashboard_parity.py` with existing validated
flags/env (inspect `--help`; do not invent invocation options).
```bash
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test dashboard_filters -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
uv run pytest tests/tooling/test_dashboard_parity_modules.py tests/tooling/test_dashboard_parity_projection.py -q
```
## 11. Required verification commands

Run applicable focused targets in §10, then the full default/no-default gates:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
git diff --check
```
Run `cargo deny --manifest-path rust/Cargo.toml check`, the feature tree,
and `cargo build --manifest-path rust/Cargo.toml --locked --release` when
Cargo dependencies/features change. Use the repository's tooling/test commands
when scripts are changed. Closure records must distinguish local vs hosted
vs physical hardware results; never claim unrun tests as passed.
## 12. Documentation

Update `architecture/deep-dive-dashboard.md`,
`docs/deployment.md` and the dashboard test/qualification documentation
with old-page oracle versus new-page Rust reference rules and traceability.

## 13. Acceptance criteria

Every new admin/member page has a reusable redacted
desktop/mobile browser baseline with documented approved appearance;
forms and role guards work interactively, including forced password
change and one-time token. Old pages show no unreviewed regression
against the frozen Python oracle. A screen without historical Python
equivalent is **not** judged by invented Python pixel parity.
UI accurately distinguishes hard versus estimated quotas after M008.
No leakage, missed role/CSRF fence or serious accessibility defect remains.

## 14. Stop conditions

Stop if the oracle dependency is unavailable at final closure;
mark conditional/blocked evidence and request the exact worktree or
runner artifact rather than bypassing regression checks. Stop
on newly revealed auth/privacy flaw (new security corrective owner),
massive frontend rewrite or raw secret fixture.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/011-status.md`:
browser-manifest hashes, per-page/view/theme interaction and
negative-role matrix, historical-route parity results with accepted
differences, new Rust-owned reference screenshots, accessibility
checks, actual commands and CI results, residuals, registry audit.
If any mandatory browser gate cannot run, do not mark fully closed.

## 16. Handoff

Independent browser qualification can proceed in parallel with
budget correctness; rerun budget wording once M008/M009 land.
