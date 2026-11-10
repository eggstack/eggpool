# On-Prem Member Access Milestone 002 — Usage windows and member limits

Status: implemented

Repository baseline: `9da5e2d383c73035850c37f8125c145b8b7ff9ff`

Source roadmap:

- `plans/subsystems/on-prem-member-access-roadmap.md#milestone-2--usage-windows-and-member-limits`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`

Applicable ADRs:

- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md`
- None of ADR-0010.

Primary class: capability

## 1. Objective

Show usage inside a single window kind, and show a member their own limits,
their own remaining allowance, and the configured instance ceilings without
anyone else's consumption.

## 2. Why this milestone is ready

No hard dependencies. Settlement already writes one day row and one month
row. This milestone changes projection only.

## 3. Current implementation evidence

`user_budget.rs` settlement inserts a `day` row and a `month` row for the
same request. `usage_page` in `rust/src/server/dashboard/admin.rs` sums
`user_budget_usage` grouped by user and model, so each request contributes
twice and every historical window is folded in. `render_my_usage` prints
`window_start` and drops `window`.

`GET /api/user-budget` in `rust/src/server/health.rs` returns `rules` and
`usage` for the caller. An administrator also receives `instance_rules` and
`instance_usage`. The comment states that omission is so a member cannot
infer other users' consumption. `docs/model-limits.md` says instance
ceilings are admin-visible only.

Active holds live on `user_budget_reservations`. The member page mentions
holds and does not display them or a remaining figure.

## 4. Invariants that must not regress

- Settlement still writes both buckets. This milestone does not change
  admission or the counters.
- A member query stays constrained to that member's `user_id`.
- An administrator is required to read another user's budget.
- Instance usage totals and other users' rows stay off the member payload.
- No secret, prompt, or raw body is added to the projection.

## 5. Scope

### In scope

- Admin usage projection, member usage projection, and `/api/user-budget`.
- Remaining allowance against the caller's own rules.
- Instance ceiling configuration on the member projection.
- The `docs/model-limits.md` sentence that currently hides the ceiling
  configuration as well as the aggregate.

### Explicitly out of scope

- Changing estimated versus hard enforcement.
- A schema migration.
- Redesigning the users-page budget form. That is milestone 003.
- Revealing remaining shared headroom.

## 6. Required production changes

Admin HTML shows the current UTC day and the current UTC month as separate
tables, each grouped by user and model inside that window kind and that
window start. Historical rows may appear in a third listing that is still
split by window kind. No query sums a day row with a month row.

Member HTML adds the window kind column. It shows, for the current UTC day
and current UTC month, each applicable own rule's ceiling and the remaining
amount. Remaining is the ceiling minus settled usage in that window minus
active holds that count against that rule. A rule with a null ceiling for
a dimension shows no remaining for that dimension. Holds that are not yet
settled must not be omitted from the subtraction.

Member HTML and the member JSON add the instance ceiling rules: model,
window, enforcement, and the four ceilings. They do not add instance usage,
other users, or a remaining figure for the instance ceiling. The page says
the shared ceiling also applies and that other people's consumption is not
shown.

Administrator JSON keeps `instance_rules` and `instance_usage`. Adding
member fields is allowed. Removing or renaming `rules` or `usage` is not.
Microdollar values stay integers. Display may also show USD as
microdollars divided by 1_000_000. The stored unit does not change.

The truncation notice uses the same cap the query applies, and it does not
tell the reader to edit the query.

## 7. Ordered work packages

### Work package A — Window-faithful reads

Intent: Every displayed total is inside one window kind.

Required changes: Replace the summing admin query. Include `window` in the
member row renderer. Keep the 500-row cap and make the notice describe the
cap that was applied.

Acceptance evidence: A fixture with one settled request produces a day
total and a month total that each equal that request, not twice that
request. An admin page that renders both does not add them.

### Work package B — Own remaining allowance

Intent: A member can see headroom against their own rules.

Required changes: Add a read in `rust/src/user_budget.rs` that returns
current-window settled totals and active hold totals for one user. Render
them on `/usage` and include them in `/api/user-budget` for that user.
An administrator requesting another `user_id` receives that user's own
remaining, not the instance remaining.

Acceptance evidence: A rule of 100 requests, 40 settled, and 10 held
reports 50 remaining. A second user's rows do not appear. A dimension
without a ceiling has no numeric remaining.

### Work package C — Instance ceiling configuration

Intent: A member can see the shared cap that may deny them, without seeing
who else consumed it.

Required changes: Load instance rules for the member response. Do not call
the instance usage query for a member. Update `docs/model-limits.md` and
the on-prem deep dive so ceiling configuration is member-visible and
instance usage remains admin-only.

Acceptance evidence: Member JSON contains the ceiling numbers and omits
instance usage. Two members with different personal usage receive the same
ceiling object. Admin JSON still contains instance usage.

## 8. Failure, cancellation, restart, contention semantics

These are reads. A database error returns the existing degraded response
and does not render a partial table that looks complete. Remaining is
computed in one read of settled rows plus active reservations so a
concurrent settle cannot be subtracted twice by adding two independently
stale copies in the page. If that single read cannot be offered, return
the degraded response rather than a remaining figure.

## 9. Compatibility and migration

No migration. Clients that ignore unknown JSON fields keep working.
`rules` and `usage` stay. Document the added fields in
`docs/model-limits.md` and `docs/api-reference.md` if that page describes
`/api/user-budget`.

## 10. Required tests

- `rust/tests/on_prem_budgets.rs` and `rust/tests/on_prem_global_budgets.rs`
  for the doubled-window regression, remaining arithmetic, and member
  denial of another user's usage.
- Dashboard render tests in `rust/src/server/dashboard/tests.rs` for the
  window column and the truncation sentence.
- A member principal fixture against `/api/user-budget` that asserts the
  absence of instance usage keys.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_global_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib server::dashboard::tests -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run python scripts/validate_release_docs.py
```

## 12. Documentation updates

- `docs/model-limits.md`
- `docs/api-reference.md` if it documents `/api/user-budget`
- `architecture/deep-dive-on-prem-user-access.md` projection paragraph

## 13. Acceptance criteria

- One settled request appears once in the current day total and once in
  the current month total, and those totals are not added together.
- A member page names day and month on each usage row.
- Remaining matches ceiling minus settled minus active holds for that
  member and window.
- A member response includes instance ceiling configuration and excludes
  instance usage and other users.
- An administrator can still read instance usage.

## 14. Stop conditions

Stop if a correct remaining figure requires a second SQLite connection or
a change to settlement. Stop if showing ceiling configuration cannot be
done without also shipping instance usage to the member. Stop if the
change needs a migration.

## 15. Closure evidence required

The doubled-count fixture, the remaining fixture, and the member payload
assertion. Commands and results. A statement that settlement code and
schema checksums did not change.

## 16. Handoff notes

Do not "fix" the double count by deleting the month bucket. Both buckets
are the enforcement model. USD display is a rendering of the integer
microdollar value. Milestone 003 will show the same USD rendering on the
users page; keep one helper so the two pages cannot drift. Serial tests
only.
