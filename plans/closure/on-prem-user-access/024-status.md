# On-prem User Access Milestone 024 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/024-browser-session-csrf-delivery-and-member-account-surface-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#7-milestones`

Repository baseline reviewed: `d04ad6831c6e030b7f5fc8a56501fdfecab982bd`

Implementation commits or pull requests:

- `c2ca0a6ebd5db6407f38cf380ea060d81340f4e8` — Implement the browser session CSRF delivery and member account surface corrective

Governing decision:

- `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`
  (accepted 2026-10-23)

## 1. Executive finding

Milestone 024 is closed. The three defects the milestone set out to correct are
corrected, each by the mechanism ADR-0004 adopts, and each by a test that fails
when the production behavior is reverted.

The CSRF cookie is now delivered on exactly the requests on which the session
cookie is delivered. Before, the CSRF cookie was `SameSite=Strict` and the
session cookie was `SameSite=Lax`, so a `Lax` cookie arrived on a cross-site
top-level navigation and a `Strict` one did not. An operator who reached an
administrative page that way was authenticated but every mutation was refused
with `403 Invalid request token.`, because the renderers derive the hidden
CSRF field from the cookie alone and the server cannot re-emit the token: only
the digest is persisted. The CSRF cookie is now `SameSite=Lax` on both its set
and its clear paths. `Lax` and `Strict` behave identically for cross-site
unsafe requests, so the only behavior that changed is the top-level GET
navigation that was broken. The server-side same-origin check on unsafe methods
and the double-submit token requirement are unchanged, and the account page
still derives its hidden field from the cookie alone, so the double-submit
property is unchanged.

The sign-in throttle now bounds an account rather than an (account, source
address) pair. The account bucket is keyed on the normalized username alone, so
ten failures against an account exhaust that account's budget no matter how
many source addresses they come from, and a rotating address no longer buys a
fresh budget. A successful credential check clears the account bucket, so a run
of failures that does not end in a successful sign-in is what consumes the
budget and a later run starts from a full budget. The per-peer bucket
deliberately survives a success, because clearing it would let any valid
account reset an address's cross-account failure budget and multiply that
address's brute-force throughput without bound. The limits are unchanged: 10
per account, 30 per peer IP, a 15-minute window, a 2048-bucket capacity.

The sign-in and member surfaces are now usable. One document builder renders
the sign-in page, the account page, the account-error page, the transport
refusal, the new member landing page at `/account/landing`, and the member
refusal, so a browser never meets an unstyled fragment. Every sign-in failure
renders that document with a `role="alert"` message. Every sign-in response
carries `Cache-Control: no-store`, because `/login` is an unconditionally
public path that the middleware returns from before it applies that header. A
member who signs in lands on `/account/landing`, which links the member
destinations and exposes no administrative route and no administrative datum. A
member refused an administrative page receives an HTML page naming the refusal
and pointing at the member surface, instead of a bare text body.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| The CSRF cookie is delivered on exactly the requests the session cookie is delivered on | `rust/src/web_session.rs` — `csrf_set_cookie` and the clear path emit `SameSite=Lax`; test `both_browser_cookies_are_http_only_lax_and_root_scoped`; HTTP test `browser_sign_in_sets_both_cookies_with_the_same_attributes` | pass | Both cookies keep `HttpOnly`, `Path=/`, `Max-Age=604800`, and the existing `Secure` determination. |
| The double-submit CSRF requirement is unchanged | HTTP test `a_session_with_both_cookies_renders_a_verifying_field_and_the_mutation_succeeds` | pass | The account page still derives its hidden field from the cookie alone; `verify_csrf` is unchanged. |
| A session without the CSRF cookie renders an empty field and the mutation is refused | HTTP test `a_session_without_the_csrf_cookie_renders_an_empty_field_and_the_mutation_is_refused` | pass | This pins the current derivation, so a future change to it has to be deliberate. |
| A cross-site unsafe request is rejected by the origin check before the handler runs | HTTP test `a_cross_site_unsafe_request_is_rejected_by_the_origin_check_before_the_handler_runs` | pass | The session lookup runs before the origin check, so a request with no cookie at all is answered `401 Sign in required`. The test pins the two reachable origin rejections: a cross-site unsafe request carrying the session cookie, and a cross-site POST to `/login` carrying no cookie. Both assert the origin rejection rather than the token rejection. |
| The per-username sign-in limit applies to the account across source addresses | `rust/src/web_session.rs` — `username_throttle_key` hashes the normalized username alone; test `the_account_bucket_key_does_not_contain_the_peer_address`; test `the_account_bucket_is_shared_across_source_addresses` | pass | `rust/tests/on_prem_sessions.rs` — `login_throttle_is_bounded_by_username_across_peers` pins that a different source address does not buy a fresh budget. |
| A successful sign-in clears the account counter but not the per-peer bucket | `rust/src/web_session.rs` — `clear_login_failures` removes the account bucket only; test `a_successful_sign_in_clears_the_account_failure_counter`; test `a_successful_sign_in_does_not_clear_the_per_peer_bucket`; HTTP test `a_successful_sign_in_clears_the_account_failure_counter` | pass | The per-peer bucket survives so a valid account cannot reset an address's cross-account budget. |
| Failures for one username do not consume another peer's per-IP budget, and the per-IP limit still holds | test `one_peers_failures_do_not_consume_another_peers_ip_budget` | pass | The limits, the window, and the bucket capacity are unchanged. |
| Every sign-in surface is a complete document that links both stylesheets and resolves a complete theme variable set | `rust/src/server/auth.rs` — `session_document`; test `every_session_document_links_both_stylesheets_and_declares_the_viewport`; test `every_sign_in_failure_is_an_html_alert_that_is_not_cacheable` | pass | The sign-in surfaces carry no `var(` of their own. The stylesheet's own variables resolve in every bundled theme, which the Dashboard M014 guard `every_stylesheet_variable_resolves_in_every_bundled_theme` proves. |
| Every sign-in failure renders the same document shape with a `role="alert"` message | test `every_sign_in_failure_is_an_html_alert_that_is_not_cacheable`; HTTP test `every_sign_in_failure_is_a_no_store_html_page_with_an_alert` | pass | The transport refusal keeps the operator guidance — the SSH port forward example and the HTTPS reverse proxy and VPN paths — in substance. |
| Every sign-in response carries `Cache-Control: no-store` | `rust/src/server/auth.rs` — `sign_in_response`; test `every_sign_in_failure_is_an_html_alert_that_is_not_cacheable`; HTTP test `every_sign_in_failure_is_a_no_store_html_page_with_an_alert` | pass | `/login` returns from the middleware before the header is applied, so the handler sets it. |
| A member who signs in lands on a page that links the member surface | HTTP test `a_member_lands_on_the_member_landing_page_and_reaches_the_member_surface`; test `the_member_landing_page_links_only_the_member_surface` | pass | `/account/landing` links `/my-account`, `/my-account/models`, and `/usage`, and exposes no administrative route. |
| An administrator who signs in still lands on `/account`, and the account page links back to the dashboard | HTTP test `an_administrator_still_lands_on_the_account_page_which_links_the_dashboard`; test `the_account_page_points_each_role_at_its_own_surface` | pass | The administrator's target is the dashboard overview at `/`. |
| A member refused an administrative page gets an actionable HTML response | HTTP test `a_member_refused_an_administrative_page_gets_an_actionable_html_page`; test `the_member_refusal_keeps_its_message_and_points_at_the_member_surface` | pass | Status `403` and the message text are unchanged; the refused-path set is otherwise unchanged. |
| An operator who must change their password is still redirected to `/account?password=required` | HTTP test `an_operator_who_must_change_their_password_is_still_redirected_to_the_account_page` | pass | The landing page is reachable without a redirect of its own, and no redirect loop exists between `/account` and `/account/landing`. |
| No schema, configuration, dependency, or published API change | `rust/Cargo.toml` and `rust/Cargo.lock` are unchanged; no migration was added; the published dashboard JSON field names and shapes are unchanged | pass | One member-only HTML route was added. |
| The default and no-default workspace suites pass | See §4 | pass | |

## 3. Production implementation evidence

- `rust/src/web_session.rs`
  - `csrf_set_cookie` emits `SameSite=Lax`; `clear_cookies` clears the CSRF
    cookie with the same attribute, so the set and clear paths stay consistent.
    The session cookie is unchanged.
  - `username_throttle_key` now hashes the normalized username alone. The
    per-peer bucket key is unchanged.
  - New `clear_login_failures(username)` removes the account bucket only.
  - `login_allowed` still consults both buckets before the credential check
    and refuses hard, so the account bound stays a guess bound rather than an
    advisory one.
- `rust/src/server/auth.rs`
  - `session_document` is the single document builder for the sign-in page,
    the account page, the account-error page, the transport refusal, the member
    landing page, and the member refusal.
  - `sign_in_response` applies `Cache-Control: no-store, max-age=0`.
  - `login_submit` clears the account bucket on a successful credential check,
    and redirects a member to `/account/landing` and an administrator to
    `/account`.
  - `member_landing_page` and `member_refusal_html` are new.
  - The credential fields, their `autocomplete` attributes, the password length
    bounds, the `role="alert"`, the form actions, and the `html_escape` usage
    are preserved.
- `rust/src/server/middleware.rs`
  - `/account/landing` is added to the member-allowed surface set, the
    browser-session list, and the authentication list.
  - The member refusal is now a complete HTML document; status and message
    text are unchanged.
- `rust/src/server/mod.rs` registers `GET /account/landing`.
- `rust/tests/on_prem_admin_ui.rs` — 10 HTTP-level tests over raw HTTP/1.1.
- `rust/tests/on_prem_sessions.rs` — the throttle test now pins the
  account-scoped bucket.
- `architecture/deep-dive-on-prem-user-access.md` and
  `architecture/deep-dive-security.md` — the cookie contract, the throttle
  keying and clear semantics, the uniform sign-in document, and the member
  landing surface.
- `architecture/overview.md` — the route topology lists `/account/landing`;
  `tests/tooling/test_architecture_census.py::test_route_topology_covers_every_registered_route`
  requires it.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
```

### Results

| Command | Result |
|---|---|
| `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` | clean |
| `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings` | clean, no warnings |
| `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` | passes |
| `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings` | clean, no warnings |
| `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1` | **1208 passed, 0 failed** across every target |
| `uv run pytest tests/tooling/ -q --tb=short` | **247 passed, 1 skipped** (the skip is the release-packaging artifact inspection, which needs a release build) |
| `uv run ruff format --check scripts/ tests/tooling/` | 71 files already formatted |
| `uv run ruff check scripts/ tests/tooling/` | all checks passed |
| `uv run pyright scripts/` | 0 errors, 0 warnings, 0 informations |
| `uv run python scripts/validate_release_docs.py` | pass, 7 documents checked, 0.8.2 published |

Mutation checks — each revert makes the named tests fail, so the tests are
real rather than decorative:

| Revert | Failing test |
|---|---|
| CSRF cookie back to `SameSite=Strict` | `both_browser_cookies_are_http_only_lax_and_root_scoped`, `browser_sign_in_sets_both_cookies_with_the_same_attributes` |
| Account throttle key made peer-scoped again | four `web_session` throttle tests |
| The `clear_login_failures` call removed from `login_submit` | `a_successful_sign_in_clears_the_account_failure_counter` |
| `no_store` dropped from `sign_in_response` | two sign-in document tests |
| Member redirect always `/account` | `a_member_lands_on_the_member_landing_page_and_reaches_the_member_surface` |
| Member refusal back to bare text | `a_member_refused_an_administrative_page_gets_an_actionable_html_page` |

### Post-closure verification evidence

Added after the closure was recorded. The disposition and every conclusion
above stand; this only supplies the one gate the closure had not executed.

The `--no-default-features` **test** suite had never been run in this
repository — CI runs no-default only for `check` and `clippy` — so the
closure's acceptance criterion "the default and no-default workspace suites
pass" rested on the default suite plus the no-default check and Clippy. The
no-default suite was executed afterwards and passes:

```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features --offline --locked -- --test-threads=1
```

1209 passed, 0 failed. The acceptance criterion is now fully satisfied.

`--offline --locked` is required in this environment: without it, cargo blocks
trying to refresh the crates.io index over the blocked network. The build is
otherwise identical to the default-features one, and `--locked` asserts that
the lockfile is unchanged by the run.

## 5. Invariant review

- **The server-side origin check stays authoritative.** The same-origin check
  on unsafe methods is untouched, and it runs before the handler. The CSRF
  cookie attribute is a delivery hint; it is not the CSRF defense.
- **The double-submit property is unchanged.** The account page still derives
  its hidden field from the cookie alone, `verify_csrf` still compares in
  constant time, and a request with no CSRF cookie still renders an empty field
  whose mutation is refused.
- **The member and administrator surfaces stay disjoint.** The landing page
  links only the member destinations and exposes no administrative route or
  administrative datum. The refused-path set is unchanged.
- **The throttle stays a hard refusal consulted before the credential check.**
  The account bound is a guess bound, not an advisory one.
- **No credential, prompt, raw body, or cache key** entered a rendered
  document, a response, a log, or test evidence. The HTTP tests assert on
  markup and status codes, never on a credential value.
- **The throttle stays process-local and unpersisted**, as it was before.
- **No new restart or reload key list** was introduced, and no configuration
  key was added.

## 6. Failure and recovery review

- The transport refusal keeps the operator's recovery path in substance: the
  SSH port-forward example and the HTTPS reverse-proxy and VPN guidance. The
  refusal is now a document rather than a bare string, so the guidance is
  readable.
- The throttled attempt and the unavailable service are recoverable surfaces:
  the throttled attempt is an HTML document the operator can retry from, and
  the unavailable service names the condition.
- A member refused an administrative page is given a route forward — the member
  landing page — rather than a dead end.
- An operator who must change their password is still redirected to
  `/account?password=required`, and that page is reachable without a redirect
  of its own, so no redirect loop exists.
- The account lockout remains a hard refusal: once the account's budget is
  exhausted, an attempt is refused before the credential is checked, so a
  correct password does not break an active lockout. A successful sign-in
  clears the account bucket, so the counter is not carried across a successful
  sign-in. The consequence is recorded in ADR-0004's Consequences and in §10.

## 7. Migration and compatibility review

- No schema change and no migration. The database is untouched.
- No configuration key was added, removed, or reinterpreted.
- No dependency was added; `rust/Cargo.toml` and `rust/Cargo.lock` are
  unchanged.
- The cookie names (`eggpool_session`, `eggpool_csrf`), the `Path`, the
  `Max-Age`, and the `Secure` determination are unchanged. The only cookie
  change is the CSRF cookie's `SameSite`, from `Strict` to `Lax`.
- The published JSON API is unchanged: no endpoint, field, or response shape
  changed. One member-only HTML route was added at `/account/landing`.
- A browser that already holds a `Strict` CSRF cookie keeps it until that
  cookie's seven-day `Max-Age` expires, because the server cannot re-emit the
  token — only the digest is persisted. This is recorded in ADR-0004's
  migration tail, and it matches the session's absolute expiry.

## 8. Security review

- **CSRF.** The CSRF cookie is delivered on the same requests as the session
  cookie, so a deep-linked operator's page can be acted on. The cross-site
  unsafe request is still refused: `Lax` and `Strict` withhold the cookie
  identically on a cross-site unsafe request, and the origin check refuses the
  request before the handler runs.
- **Throttle.** The account's budget is now the account's. A distributed
  attempt against one account meets one budget rather than one budget per
  source address. The per-peer bucket still bounds an address across accounts,
  and at 30 failures per window against 10 failures per account, one address
  can lock out at most three accounts per window.
- **Success clear.** Only the account bucket is cleared. The per-peer bucket
  survives, so a valid account cannot reset an address's cross-account
  failure budget and multiply that address's brute-force throughput without
  bound.
- **Enumeration.** The account bucket is created on a failure regardless of
  whether the account exists, and `verify_password` uses a dummy-hash path for
  an unknown user, so the throttle reveals nothing about account existence. The
  failure messages are uniform.
- **Transport.** The sign-in surface still refuses a non-secure, non-loopback
  transport, and the refusal keeps the operator guidance.
- **Secrets.** No credential, token, cookie value, prompt, or raw body appears
  in a rendered document, a response, a log, or a test assertion.

## 9. Documentation and operations

- `architecture/deep-dive-on-prem-user-access.md` — the cookie contract, the
  account-scoped throttle keying and its clear semantics, the uniform sign-in
  document, and the member landing surface.
- `architecture/deep-dive-security.md` — the cookie description for both
  cookies, `/account/landing` as a session-only route, the throttle semantics,
  and the sign-in `no-store` header.
- `architecture/overview.md` — the route topology lists `/account/landing`.
- `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`
  — accepted; its Decision and Consequences match the implementation.
- `plans/implementation/on-prem-user-access/024-browser-session-csrf-delivery-and-member-account-surface-corrective.md`
  — §2, §6, and work package B's acceptance evidence were aligned with the
  accepted ADR before implementation.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Low | The account lockout is a hard refusal: once the account's budget is exhausted, an attempt is refused before the credential is checked, so a correct password does not break an active lockout for the rest of the 15-minute window. | An operator who mistypes the password ten times cannot sign in for up to 15 minutes, and a deliberate attacker can lock a known account out for the same window. The per-peer limit bounds one address to three account lockouts per window. | Recorded as the intended behavior of an account-scoped limiter in ADR-0004. A bounded grace verification — one credential check after the limit is reached, spent by any failure and cleared by any success — would remove the lockout at the cost of raising an address's guess budget against a targeted account from 10 to 11 per window, and of making the account bound partly advisory. That is a maintainer decision, not a defect in this milestone. |
| Low | `tests/fixtures/dashboard-on-prem-browser/reference.json` records `member_denied.*.message` as the bare string `Administrator access required.`, which was the whole body of the refusal. The refusal is now an HTML page, so `document.body.innerText` differs. | The on-prem browser reference is stale for the member-denied observations. It is a physical-SBC qualification artifact and is not consumed by CI, so no test fails. | Regenerate the reference on hardware with the on-prem browser qualification. The recorded evidence must come from a real run, so it was not hand-edited. |
| Low | `rust/src/server/dashboard/admin.rs` answers the bare text `Administrator access required.` on the administrative handlers themselves. | A member refused by an administrative handler sees a bare text body, while the middleware refusal now renders a page. The two refusals differ in presentation. | Out of scope for this milestone: the dashboard module is the Dashboard M014 family's ownership, and a uniform refusal across the administrative handlers is a separate change. |

## 11. Roadmap disposition

`plans/subsystems/on-prem-user-access-roadmap.md` is updated: M024 moves from
`ready` to `closed`, the milestone section records the closure, the
reopening note is resolved, and the Completion definition records that the
subsystem closes again on this evidence. M001–M023 stay closed and immutable.

The subsystem's status returns to `closed`. The two findings in §10 that remain
open are recorded against the subsystem rather than carried by a new milestone,
because neither is a defect in the corrected behavior: one is a documented
consequence of the account-scoped limiter and the other is a stale
physical-SBC reference artifact that a hardware run must regenerate.

## 12. Registry updates

- `plans/registry.md` — the On-prem user access row moves from `active` to
  `closed`; the M024 entry moves from the dependency-ready table to the
  recently-closed table; the narrative paragraph records the acceptance of
  ADR-0004 and the closure.
- `plans/subsystems/on-prem-user-access-roadmap.md` — M024 is closed and the
  ADR-0004 dependency is discharged.
- No other subsystem's rows changed.
