# On-prem User Access Milestone 024 — Browser session CSRF delivery and member account surface corrective

Status: implemented

Repository baseline: `d04ad6831c6e030b7f5fc8a56501fdfecab982bd`

Source roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for principals, sessions, and the managed-user boundary
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`
  (**accepted** 2026-10-23 — this milestone implements that decision; see §2)

Primary class: invariant

## 1. Objective

Make the browser session work for an operator who reaches it the way operators
actually reach a console — by following a link from a chat client, a mail
client, or a bookmark on another site — and make the signed-in account surface
a place an operator can navigate from, rather than a dead end they have to know
the URL of.

Concretely: deliver the CSRF cookie on the same requests that already deliver
the session cookie, keep the server-side CSRF and origin enforcement exactly as
strict as it is now, make the sign-in throttle bound an account rather than an
account-and-address pair, and give the sign-in, account, and member pages a
presentation and a navigation that match the rest of the product.

## 2. Why this milestone is ready

**Ready.** This milestone changes auth semantics, so the repository's ADR
threshold required an architecture decision for it. That decision — the CSRF
cookie's `SameSite` attribute, the sign-in throttle's bucket key, and the rule
that a cookie attribute is a delivery hint rather than a security boundary — is
recorded in
`plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`,
which the project maintainers accepted on 2026-10-23.

The milestone has no hard dependency on another milestone, it changes no schema
and no public API, and every defect it addresses is reproducible from the
repository at the stated baseline.

Two passages in the ADR's `proposed` draft were corrected before acceptance, and
this plan follows the corrected decision:

- No throttle limit is raised. The account limit stays at 10 failures and the
  per-peer limit stays at 30 over the same 15-minute window.
- A successful sign-in clears the **account's** failure bucket only. The
  per-peer bucket is not cleared, because clearing it would let any valid
  account reset an address's cross-account failure budget.

§6 and work package B state the corrected contract.

## 3. Current implementation evidence

**The CSRF cookie is withheld on a cross-site navigation that the session
cookie is delivered on.** `rust/src/web_session.rs:270` emits
`eggpool_session` as `HttpOnly; SameSite=Lax; Max-Age=604800` and `:277` emits
`eggpool_csrf` as `HttpOnly; SameSite=Strict; Max-Age=604800`. `Lax` cookies
are delivered on a cross-site top-level `GET` navigation; `Strict` cookies are
not. Both attributes withhold the cookie on a cross-site unsafe-method
request, so the difference is confined to top-level navigation.

The page then renders with an empty hidden CSRF field, because the renderers
derive the field from the cookie alone:

- `rust/src/server/dashboard/admin.rs:206` — the users page
- `rust/src/server/dashboard/admin.rs:243` — the profiles page
- `rust/src/server/dashboard/admin.rs:348` — the my-account page

`csrf_valid` (`rust/src/server/dashboard/admin.rs:153`) requires **both** the
submitted field and the cookie to verify against the session's CSRF digest, so
the mutation is rejected with `403 Invalid request token.` The same
double-submit requirement is enforced on the password change
(`rust/src/server/auth.rs:156`) and the logout path.

The server cannot repair this on an already-signed-in browser.
`web_session::issue` persists only the CSRF digest
(`rust/src/web_session.rs:137`); the plaintext token exists only in the login
response, and the cookie is set exactly once, at
`rust/src/server/auth.rs:119-126`.

`architecture/deep-dive-on-prem-user-access.md:104-106` records the asymmetry
as deliberate and `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md:175`
lists SameSite cookie behavior as accepted posture, which is why the change
needs an ADR rather than a patch. `architecture/deep-dive-security.md:57-58`
already describes both cookies as `SameSite=Lax`, which is inaccurate for the
CSRF cookie.

**The sign-in throttle bounds an account-and-address pair, not an account.**
`web_session::login_allowed` (`rust/src/web_session.rs:300`) and
`record_login_failure` (`rust/src/web_session.rs:320`) key the per-username
bucket on `username_throttle_key` (`rust/src/web_session.rs:411`), which folds
the peer address into the key: `"{username}:{peer}"`. The documented limits —
10 failures per username, 30 per peer IP, over a 15-minute window
(`rust/src/web_session.rs:17-19`) — are therefore enforced per
(username, source address) pair. A second source address presents a fresh
per-username budget of 10, and only the per-IP limit of 30 bounds a
distributed attempt against one account. The same keying means a legitimate
operator whose source address rotates never accumulates the documented limit.

`web_session.rs` has no success recorder: `record_login_failure` is the only
writer, and it is called only on a failed credential check
(`rust/src/server/auth.rs:102`). Ten failed attempts therefore lock the account
for the remainder of the 15-minute window even when the next attempt carries
the correct password, because `login_allowed` is consulted before the
credential is verified (`rust/src/server/auth.rs:82`).

**The sign-in and account pages carry no stylesheet at all.**
`rust/src/server/auth.rs:415` (`login_html`), `:130` (`account_page`), and `:424`
(`account_error_html`) each emit a complete HTML document that references no
stylesheet. The sign-in page — the first thing every operator sees — renders
as browser-default HTML while the dashboard behind it is a themed application.

The dashboard's own stylesheet declares no custom properties of its own
(`rust/assets/dashboard/static/dashboard.css` has no `:root` block; see
`rust/src/server/dashboard/assets.rs:31-36`), so the complete variable set
comes from `/static/theme.css`, which `rust/src/server/dashboard/assets.rs:37`
serves and which resolves an absent or unknown `theme` parameter to the
default theme. That route is registered unconditionally
(`rust/src/server/mod.rs:1184-1188`), so it is available to the sign-in pages
whether or not the dashboard is enabled.

**Some sign-in failures return a plain-text body and some return a page.**
`rust/src/server/auth.rs:67-80` returns `Browser sign-in requires HTTPS or
loopback.` and `Cross-origin sign-in is not allowed.` as bare text;
`:82-88` returns the styled sign-in page for a throttled attempt; `:89-92`
returns `Authentication service unavailable.` as bare text. The one failure the
operator is most likely to hit — a throttled sign-in — is the only one that
renders as a page.

**The sign-in response is cacheable.** `is_unconditionally_public`
(`rust/src/server/middleware.rs:179`) exempts `/login`, and the middleware
returns early for an unconditionally public path at
`rust/src/server/middleware.rs:11-13`. The `Cache-Control: no-store` header is
applied only to responses that reach `next.run`
(`rust/src/server/middleware.rs:146-150`), so the sign-in page is served
without it.

**A member has no landing page and no way to reach the member surface.**
`rust/src/server/auth.rs:113-118` redirects every successful sign-in to
`/account`. `account_page` (`rust/src/server/auth.rs:130-147`) renders the
password form and the sign-out form and nothing else — no link to
`/my-account`, `/my-account/models`, or `/usage`.

`rust/src/server/middleware.rs:94-107` restricts a non-admin principal to a
fixed account surface (`/account`, `/account/password`, `/logout`,
`/api/user-budget`, `/my-account`, `/my-account/models`, `/usage`, and the
`/account/tokens` prefix). Every dashboard page, including the shared
navigation that links "My Account" → `/my-account`
(`rust/src/server/dashboard/render/layout.rs:94`), is refused for a member with
`403 Administrator access required.` — a bare text body. The member hub, the
only page that lists the member's accessible models, personal credentials, and
usage links, is therefore reachable only by typing its URL.

**A member refused a dashboard page receives text, not a page.**
`rust/src/server/middleware.rs:105-107` returns
`(StatusCode::FORBIDDEN, "Administrator access required.").into_response()`,
which axum renders as `text/plain`.

## 4. Invariants that must not regress

- The server-side same-origin check on unsafe methods remains the authoritative
  CSRF defense, and it still runs before the handler. See
  `rust/src/server/middleware.rs:121-133`.
- The double-submit CSRF token requirement remains: the session's CSRF digest
  must verify against both the submitted form field and the cookie.
- The credential verification path, the transport gate
  (`rust/src/server/auth.rs:67`), the password hashing parameters, and the
  session's idle and absolute expiry bounds are unchanged.
- Sessions, CSRF digests, and throttle keys remain persisted or held as
  digests. No secret is persisted in plaintext to make a delivery attribute
  work.
- No new durable write is added to an authenticated page render.
- A member never reaches an administrative page or an administrative datum.
  The member budget response keeps withholding the instance ceilings
  (`rust/src/server/health.rs:371`).
- The administrative surface remains owner-scoped: a member's `/usage` reads
  only the member's own rows (`rust/src/server/dashboard/admin.rs:288`).
- Credentials, prompts, raw bodies, and cache keys stay out of rendered HTML,
  responses, logs, and test evidence.
- The session cookie's attribute, `Secure` determination, `Max-Age`, and
  `Path` are unchanged.
- No new dependency, and no change to the inference or personal-credential
  paths.

## 5. Scope

### In scope

- The CSRF cookie's `SameSite` attribute, per ADR-0004.
- The sign-in throttle's per-username bucket key, and clearing the failure
  counter on a successful sign-in, per ADR-0004.
- A shared document builder for the sign-in, account, account-error, and
  transport-refusal pages that links the dashboard stylesheet and the complete
  theme variable set, and that renders the form inside the product's panel
  treatment.
- A single presentation for every sign-in failure, including the transport
  refusal, the origin refusal, the throttled attempt, and the unavailable
  service.
- `Cache-Control: no-store` on the sign-in responses.
- A member landing page that links the member surface, and a sign-in redirect
  that lands a member on it.
- An HTML response, rather than a plain-text body, for a member refused an
  administrative page, that points at the member surface the member does have.
- Regression tests for each of the above.

### Explicitly out of scope

- The dashboard's own pages, its theme delivery on the dashboard-owned shells,
  its asset corrections, and its accessibility work. Those are planned in
  Dashboard M014.
- Any change to the inference path, the personal-credential path, the model
  policy, the budget service, or the model list.
- Any change to the session token format, the idle or absolute expiry, or the
  revocation model.
- Any change to the password hashing parameters, the Argon2id configuration, or
  the dummy-hash timing path in
  `rust/src/identity.rs:1338`.
- Any new rate-limiting dimension beyond moving the existing per-username
  bucket off the peer address.
- Any new dependency, and any persistence of a CSRF or throttle value in
  plaintext.
- Any change to the administrative route surface or the role model.

## 6. Required production changes

**CSRF cookie delivery.** The CSRF cookie is emitted with `SameSite=Lax`,
matching the session cookie, under the same `HttpOnly`, `Path`, `Max-Age`, and
`Secure` determination. The session cookie is unchanged. The double-submit
check and the server-side origin check are unchanged. The change is a
cookie-attribute change in `rust/src/web_session.rs`; the same attribute must
be reflected in the sign-out and password-change cookie clearing so the two
stay consistent.

**Account-scoped sign-in throttle.** The per-username bucket is keyed on the
normalized username alone. The per-peer bucket stays keyed on the peer
address. The limits (10 per username, 30 per peer IP), the 15-minute window,
and the 2048-bucket capacity stay as they are, and no limit is raised. A
successful credential check clears the account's failure bucket, so a run of
failures that does not end in a successful sign-in is what consumes the
account's budget and a later run starts from a full budget. A successful
credential check does **not** clear the per-peer bucket: clearing it would let
any valid account reset an address's cross-account failure budget and thereby
multiply that address's brute-force throughput without bound. The throttle
remains process-local and is not persisted.

**Shared sign-in and account document.** The sign-in page, the account page,
the account error page, and the transport-refusal page are rendered through one
helper that emits a complete document with the viewport declaration, the
dashboard stylesheet, the theme stylesheet, and the product's panel treatment.
The theme stylesheet is requested without a theme parameter so the route's
existing default applies; the sign-in surface has no operator-selected theme
because the theme is carried in the query string of the dashboard navigation
rather than stored.

The helper must keep every existing element: the credential fields, their
`autocomplete` attributes, the password length bounds, the `role="alert"` on
the error message, and the form actions.

**Uniform failure presentation.** Every sign-in failure returns the same
document shape with an `role="alert"` message. The transport refusal keeps
its current guidance about the SSH port forward and the HTTPS reverse proxy,
because that text is operator-facing documentation of the supported paths, not
a generic error.

**No-store on the sign-in surface.** The sign-in responses carry
`Cache-Control: no-store, max-age=0`. The middleware's unconditionally-public
early return currently skips the header that other authenticated responses
receive, so the sign-in responses must set it themselves rather than relying
on the middleware path they do not take.

**Member landing page and redirect.** The account page gains a navigation
section. For a member it links `/my-account`, `/my-account/models`, and
`/usage`. For an administrator it links back to the dashboard overview. A
successful sign-in redirects a member to the member landing page and an
administrator to `/account`, so a member lands somewhere with a way onward
rather than on a bare password form.

The redirect target must respect the existing forced-password-change path: an
operator who must change their password still lands on
`/account?password=required`, and the middleware's forced-change redirect
(`rust/src/server/middleware.rs:108-112`) continues to send them there from
any other page.

**Member refusal as a page.** The middleware's refusal of an administrative
page for a non-admin principal returns an HTML document that states the
refusal and links the member surface, instead of a bare text body. The status
code stays `403` and the refusal itself is unchanged.

## 7. Ordered work packages

### Work package A — CSRF cookie delivery

Intent:

Make the CSRF token reachable on every request that already carries the
session, so a deep-linked operator's mutations work.

Required changes:

- Change the CSRF cookie's `SameSite` attribute to `Lax` in
  `rust/src/web_session.rs`, in both the set and the clear path.
- Keep the session cookie's attribute unchanged.
- Update `architecture/deep-dive-on-prem-user-access.md` and
  `architecture/deep-dive-security.md` to describe the corrected contract.

Acceptance evidence:

- A test asserts the sign-in response sets both cookies with `HttpOnly`,
  `SameSite=Lax`, `Path=/`, the expected `Max-Age`, and `Secure` under the
  existing determination.
- A test asserts that a request carrying the session cookie but no CSRF cookie
  renders a page whose hidden CSRF field is empty and whose mutation is
  rejected with `403`, pinning the current behaviour so that a future change to
  the derivation is deliberate rather than accidental.
- A test asserts that a request carrying both cookies renders a page whose
  hidden CSRF field verifies against the session digest and that the mutation
  succeeds.
- A test asserts that a cross-site unsafe request carrying no cookie is
  rejected by the origin check before the handler runs, and that the rejection
  is the origin rejection and not the token rejection.

### Work package B — Account-scoped sign-in throttle

Intent:

Make the documented per-username limit apply to the account, and let a
correct password clear a stale counter.

Required changes:

- Key the per-username bucket on the normalized username alone.
- Clear the account's failure bucket after a successful credential check. Do
  not clear the per-peer bucket.
- Keep the per-peer bucket, the limits, the window, and the bucket capacity
  as they are.

Acceptance evidence:

- A test asserts that ten failures for one username from two different peer
  addresses exhaust the account's budget, and that a further attempt is refused.
- A test asserts that failures for one username from one peer do not consume
  another peer's per-IP budget, and that the per-IP limit still holds.
- A test asserts that a successful sign-in clears the account's failure
  counter, so the counter is not carried across a successful sign-in: after
  nine failures followed by a successful sign-in, the account can absorb nine
  further failures before it is refused.
- A test asserts that a successful sign-in does **not** clear the per-peer
  bucket, so a valid account cannot reset an address's cross-account failure
  budget.

### Work package C — Shared sign-in and account document

Intent:

Give the sign-in and account surfaces the same presentation as the rest of
the product.

Required changes:

- Introduce one document builder in `rust/src/server/auth.rs` that emits the
  complete document, links `/static/dashboard.css` and `/static/theme.css`,
  and renders the body inside the product's panel treatment.
- Route `login_html`, `account_page`, `account_error_html`, and the transport
  refusal page through it.
- Preserve every existing field, bound, `autocomplete` attribute, form
  action, and `role="alert"`.

Acceptance evidence:

- A test asserts each of the four pages links both stylesheets, declares the
  viewport, and contains no unresolved `var(` dependency.
- A test asserts the sign-in form still carries its `autocomplete` attributes
  and that the account page still carries the password length bounds.

### Work package D — Uniform failure presentation and no-store

Intent:

Give every sign-in failure the same page shape, and stop the sign-in
responses from being cached.

Required changes:

- Return the shared document for the transport refusal, the origin refusal,
  the throttled attempt, and the unavailable service, each with an
  `role="alert"` message.
- Preserve the transport refusal's guidance about the SSH port forward and the
  HTTPS reverse proxy.
- Set `Cache-Control: no-store, max-age=0` on the sign-in responses.

Acceptance evidence:

- A test asserts each failure response is `text/html`, contains an
  `role="alert"`, and carries `Cache-Control: no-store`.
- A test asserts the transport refusal still names the SSH port forward.

### Work package E — Member landing page and sign-in redirect

Intent:

Land a member somewhere they can navigate from.

Required changes:

- Add a navigation section to the account page: for a member, links to
  `/my-account`, `/my-account/models`, and `/usage`; for an administrator, a
  link to the dashboard overview.
- Redirect a member to the member landing page after a successful sign-in, and
  keep the administrator on `/account`.
- Keep the forced-password-change redirect as it is.

Acceptance evidence:

- A test asserts a member's sign-in response redirects to the member landing
  page, and that the landing page links all three member destinations.
- A test asserts an administrator's sign-in response still redirects to
  `/account`, and that the account page links back to the dashboard.
- A test asserts an operator who must change their password is still
  redirected to `/account?password=required` and that the middleware's
  forced-change redirect is unchanged.

### Work package F — Member refusal as a page

Intent:

Turn a bare-text refusal into something an operator can act on.

Required changes:

- Replace the plain-text refusal at `rust/src/server/middleware.rs:105-107`
  with an HTML response carrying the same status code and message, plus a link
  to the member surface.
- Do not change which paths a member is refused.

Acceptance evidence:

- A test asserts a member refused a dashboard page receives `403`, an HTML
  body, and a link to the member surface.
- A test asserts the refused path set is unchanged.

## 8. Failure, cancellation, restart, contention semantics

- No new await is added to the authentication path, and no durable write is
  added to an authenticated page render. The work package B counter clear
  happens in the same in-process map the limit is read from, under the same
  lock, and adds no I/O.
- The throttle map remains process-local. A restart clears it, as it does
  today; that property is unchanged and is not a defect this milestone
  introduces.
- No cancellation path is added or changed. The sign-in handler's existing
  behavior on a dropped connection is unchanged.
- The cookie change applies only to cookies issued at sign-in. A browser that
  already holds a `SameSite=Strict` CSRF cookie keeps it until it expires,
  because the stored attribute governs delivery and the server cannot re-emit
  the token value. Because the CSRF cookie's `Max-Age` already matches the
  session's absolute expiry, the affected population is bounded by the
  remaining lifetime of a session signed in before the change, and those
  operators get the corrected attribute at their next sign-in. The deployment
  note must say this rather than saying the fix applies on restart.
- The forced-password-change redirect is a middleware redirect that runs before
  the handler; moving the sign-in redirect target must not create a loop
  between `/account` and the landing page. The landing page must therefore be
  reachable without a redirect of its own.
- No new contention is introduced: the account page and the sign-in page are
  read-only renders.

## 9. Compatibility and migration

- No storage migration. No table, column, index, or schema version changes.
- No configuration key is added, removed, or reclassified in
  `rust/src/config_reload_policy.rs`.
- No published API, endpoint, field, or response shape changes. The status
  codes, the session token format, the cookie names, and the `Path` and
  `Max-Age` attributes are unchanged.
- The CSRF cookie's `SameSite` attribute changes from `Strict` to `Lax` for
  cookies issued at sign-in. See §8 for the bounded migration population.
- The per-username throttle limit becomes stricter for an account under a
  distributed attempt and looser for a legitimate operator whose source address
  rotates. This is the intended correction.
- The sign-in, account, account-error, and transport-refusal pages change
  their rendered HTML to include stylesheets and a panel treatment. They are
  operator-facing surfaces, not a published contract, and no test or client
  depends on their exact markup beyond the elements this milestone preserves.
- The member refusal response changes from `text/plain` to `text/html` with
  the same status code. No client depends on the content type of a refusal.

## 10. Required tests

- `rust/tests/on_prem_admin_ui.rs` — the existing browser-driven on-prem
  target. Extend it with the member landing page, the member refusal, and the
  themed sign-in page, and with the cross-site-navigation case in work package
  A.
- `rust/src/server/auth.rs`'s in-module tests — the cookie attributes, the
  shared document, the uniform failure presentation, and the no-store header.
- `rust/src/server/middleware.rs`'s in-module tests, if it has them, or the
  nearest existing server target — the member refusal response and the
  unchanged refusal path set.
- `rust/src/web_session.rs`'s in-module tests — the account-scoped bucket, the
  per-peer bucket, and the success clear.
- A new focused target is acceptable if the existing on-prem target cannot
  host the browser-driven cases; it must be registered in `rust/Cargo.toml`
  and run serially.
- All Rust tests run with `--test-threads=1`.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

Plus the narrowest focused target for each work package. Do not claim commands
not actually run in the closure record.

## 12. Documentation updates

- `architecture/deep-dive-on-prem-user-access.md` — the corrected cookie
  contract, the corrected throttle keying, and the member landing surface.
- `architecture/deep-dive-security.md` — the cookie description at `:57-58`,
  which already states `SameSite=Lax` for both cookies, must be made accurate
  and must not be left describing a contract the code does not implement.
- `architecture/overview.md` — only if the change alters the module ownership
  map. It is not expected to.
- `docs/` — only if an operator-facing document describes the sign-in or the
  member surface. Check before editing; do not add a new document.

## 13. Acceptance criteria

- A signed-in operator who reaches any administrative page by a cross-site
  top-level navigation finds that every mutation on the page works.
- The CSRF cookie is delivered on exactly the requests on which the session
  cookie is delivered, and a cross-site unsafe request is still rejected by
  the origin check before any handler runs.
- The double-submit CSRF requirement is unchanged: a mutation without a
  matching cookie, or without a matching field, is still refused.
- The per-username sign-in limit applies to the account across source
  addresses, and a successful sign-in clears the counter.
- The sign-in page, the account page, the account error page, and the
  transport refusal page render with the dashboard stylesheet and a complete
  theme variable set, and none of them renders an unresolved `var()`.
- Every sign-in failure renders the same document shape with an
  `role="alert"` message.
- The sign-in responses carry `Cache-Control: no-store`.
- A member who signs in lands on a page that links the member surface, and can
  reach the member surface without knowing a URL.
- A member refused an administrative page receives an actionable HTML
  response rather than a bare text body.
- No schema, configuration, dependency, or published API change.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- ADR-0004 has not been accepted.
- The CSRF cookie change would require persisting a token or digest that is
  not currently persisted, or adding a durable write to an authenticated page
  render.
- The same-origin check on unsafe methods would have to weaken, move, or be
  bypassed for any path.
- The account-scoped throttle would make a legitimate operator's lockout
  materially worse than the current behavior without an accepted decision.
- The member landing page would have to expose an administrative route or an
  administrative datum.
- Evidence contradicts an invariant stated in `AGENTS.md` or in
  `architecture/deep-dive-on-prem-user-access.md`.

## 15. Closure evidence required

- The implementation commits, and the ADR-0004 acceptance revision.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- The cookie attributes before and after, and the browser-delivery reasoning
  that justifies the change.
- Evidence that the cross-site unsafe-request rejection path is unchanged.
- Evidence that the member and administrator surfaces are still disjoint.
- Any severity-tagged unresolved finding, with a recommendation.
- The registry update that closes this milestone, in its own commit.

## 16. Handoff notes

This is a corrective pass over a closed milestone family. M001–M023 remain
closed and immutable; this plan does not reopen any of them, and it does not
revisit the transport posture, the managed transition, the budget semantics,
or the credential model that those milestones settled.

The file ownership is disjoint from the dashboard corrective plan: this
milestone owns `rust/src/server/auth.rs`, `rust/src/web_session.rs`, and the
member refusal in `rust/src/server/middleware.rs`. The dashboard corrective
plan owns `rust/src/server/dashboard/**` and `rust/assets/dashboard/static/**`.
The two may proceed in parallel.

The one shared concept is the theme stylesheet. This milestone requests
`/static/theme.css` without a theme parameter, which the route already
resolves to the default theme; it does not need the dashboard's theme
resolution helper, so there is no cross-milestone dependency.

Rust tests must run serially. Strict Clippy with `-D warnings` is an invariant
of this repository. Preserve unrelated user changes in the working tree.
