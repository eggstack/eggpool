# On-Prem Member Access Milestone 003 — Member navigation and admin roster

Status: active

Repository baseline: `9da5e2d383c73035850c37f8125c145b8b7ff9ff`

Source roadmap:

- `plans/subsystems/on-prem-member-access-roadmap.md#milestone-3--member-navigation-and-admin-roster`

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`

Applicable ADRs:

- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md`
- `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`

Primary class: polish

## 1. Objective

Give a signed-in member navigation that contains only member destinations,
and make the users page show the profile, budget rules, overrides, and
credential labels that the administrator has already set.

## 2. Why this milestone is ready

No hard dependencies. Route authorization already confines a member to the
member surfaces. This milestone changes what those pages offer and what the
administrator can see. It does not grant new routes.

Soft: if milestone 002 lands first, reuse its USD helper and do not replace
its usage tables. If it has not landed, format microdollars locally on the
users page and leave `/usage` alone.

## 3. Current implementation evidence

`DASHBOARD_NAVIGATION` in `rust/src/server/dashboard/render/layout.rs` is
one 17-link list. `dashboard_standalone_page` uses it for `/usage`,
`/my-account`, and `/my-account/models`. A member who follows Overview,
Users, Accounts, or Runtime receives the HTML 403 from
`member_refusal_html` in `rust/src/server/auth.rs`.

`render_users_admin` prints username, role, state, and the password flag.
The actions cell stacks state, password, profile, override, budget, and
token forms. The profile `<select>` does not mark the assigned profile.
The row does not list current overrides, budget rules, or credential
labels. `UserSummary` in `rust/src/identity.rs` does not carry that
assignment. Profile assignment is available from `model_policy.rs`, budget
rules from `user_budget.rs`, and credential labels from
`list_user_credentials`.

The landing page at `/account/landing` already links only to member
destinations. This milestone keeps that page and aligns the later pages
with it.

## 4. Invariants that must not regress

- A member who requests an operator path still receives 403 HTML and no
  operator data.
- Mutations keep the CSRF check and `admin_only`.
- Temporary passwords and issued tokens are still shown once, on the
  existing one-time page, and are not copied into the roster.
- The last active administrator still cannot be disabled.
- Theme selection and the operator navigation on administrator pages stay.

## 5. Scope

### In scope

- A member navigation set for member standalone pages.
- Roster data: assigned profile, overrides, budget rules, credential
  labels and created times.
- Grouped actions and a selected profile option.
- USD text beside stored microdollar ceilings on this page.

### Explicitly out of scope

- Usage arithmetic and `/api/user-budget`. Milestone 002 owns those.
- New grant or upstream-account controls. Milestone 005 owns those.
- Changing who may call each mutation.
- A SPA or a new stylesheet language.

## 6. Required production changes

Add a member navigation set: My account, My models, Usage, and Password
(`/account`). Member responses for `/my-account`, `/my-account/models`,
and `/usage` use that set. Administrator pages, including an
administrator's `/usage` and `/my-account`, keep the operator set so the
administrator can return to the overview.

`users_page` loads assignments, overrides, rules, and credential labels
through the existing services and passes them to the renderer. Each user
section shows those facts, then the forms that change them. The profile
control marks the current profile. An unassigned member is labeled so the
deny-by-default outcome is visible. Override rows and budget rows are
listed above the forms that add or remove them. Credential rows show
label, created time, and last used time. They do not show a selector or a
digest.

Microdollar ceilings render with a USD equivalent. The input name and the
integer stored by `set_user_budget` stay microdollars. The label says so.

The landing-card sentence that says My account contains budget should
point budget at `/usage`, matching the page that shows it.

## 7. Ordered work packages

### Work package A — Member navigation

Intent: Following the visible navigation cannot be the way a member
discovers an operator 403.

Required changes: Role-selected markup in
`rust/src/server/dashboard/render/layout.rs`, chosen by the handlers in
`rust/src/server/dashboard/admin.rs`. Do not remove the 403 refusal.

Acceptance evidence: Rendered member HTML for the three member pages
contains the member links and does not contain `href="/users"`,
`href="/accounts"`, `href="/profiles"`, or `href="/runtime"`.
Administrator overview HTML still contains Users and Accounts. A member
GET of `/accounts` is still 403.

### Work package B — Roster facts

Intent: The administrator sees the current assignment before changing it.

Required changes: Load the facts in `users_page` and render them in
`render/admin.rs`. Failure to load any of the lists returns the existing
degraded page rather than a roster that implies the user has no profile.

Acceptance evidence: A member with a profile, one override, one budget
rule, and one credential label shows all four. An unassigned member shows
the deny-by-default label. The profile option for the assigned profile is
selected.

### Work package C — Action grouping and USD label

Intent: The forms stay usable and the unit is explicit.

Required changes: Place each form with the fact it edits. Add the USD
rendering next to microdollar ceilings. Keep one integer input.

Acceptance evidence: The page still posts to the existing action paths.
A ceiling of 1500000 microdollars displays as 1.5 USD and submits
microdollars. The one-time token page is unchanged.

## 8. Failure, cancellation, restart, contention semantics

Reads are per request. A mutation still redirects back to `/users` after
commit. A degraded load does not render empty facts as if they were
configured empty. No new background work.

## 9. Compatibility and migration

No schema, route, or form-field rename. Existing bookmarks to operator
URLs behave as they do now for a member: 403. Administrator bookmarks
keep working.

## 10. Required tests

- `rust/src/server/dashboard/tests.rs` for both navigation sets.
- `rust/tests/on_prem_admin_ui.rs` for the roster facts, the selected
  profile, the member 403, and the absence of secrets in the roster HTML.
- A regression that an unassigned member is not rendered as if a default
  profile were already assigned.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib server::dashboard::tests -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

Run `uv run python scripts/validate_release_docs.py` if a doc file changes.
This milestone does not require a doc change to be complete.

## 12. Documentation updates

- `architecture/deep-dive-dashboard.md` and
  `architecture/deep-dive-on-prem-user-access.md` if they say member pages
  share the operator navigation.
- No CLI or API change.

## 13. Acceptance criteria

- A member session's My account, My models, and Usage pages link to member
  destinations only.
- An administrator session still reaches Users, Profiles, and Accounts
  from the operator navigation.
- The users page shows the current profile, overrides, budget rules with
  USD text, and credential labels.
- The roster HTML contains no password, token, or digest.
- Existing admin form actions still succeed for an administrator and still
  fail for a member.

## 14. Stop conditions

Stop if showing the assignment requires a new privilege or a new public
API. Stop if the member navigation change would hide Usage or My account
from the member. Stop if USD formatting would change the integer posted
to the budget action.

## 15. Closure evidence required

HTML assertions for both roles, the roster fixture, and the secret-absence
check. Commands and results. Confirmation that middleware role gates were
not widened.

## 16. Handoff notes

Member landing is already a separate document in `rust/src/server/auth.rs`.
Leave its card layout. The standalone dashboard shell is the surface to
change. Serial tests only. Do not add personal-upstream controls in this
milestone; the users page will gain them in milestone 005.
