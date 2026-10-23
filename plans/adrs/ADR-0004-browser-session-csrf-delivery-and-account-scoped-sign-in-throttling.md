# ADR-0004: Browser session CSRF delivery and account-scoped sign-in throttling

Status: accepted

Date: 2026-10-23

Accepted: 2026-10-23 by the project maintainers, on explicit maintainer
direction. The acceptance revision is the commit that changes this record from
`proposed` to `accepted`; `plans/closure/on-prem-user-access/024-status.md`
records the implementing revision.

Two passages in the `proposed` draft were corrected before acceptance, because
the draft contradicted its own Decision:

- The Negative consequences claimed that a limit was raised so that "a single
  address cannot lock an account on its own". No limit is raised. The account
  limit stays at 10 and the per-peer limit stays at 30; a single address can
  exhaust an account's budget on its own, and the per-peer limit bounds that
  address's blast radius to three accounts per window. The corrected wording
  states the bound that actually holds.
- The Decision and the Consequences said that a successful sign-in clears "the
  failure buckets for that username and peer". The per-peer bucket is not
  cleared, because clearing it would let any valid account reset an address's
  cross-account failure budget and thereby multiply that address's brute-force
  throughput without bound. The Decision now clears the account bucket only,
  and records the reason.

The corrected passages are the decision. No other content changed, and no
option, driver, or consequence was added or removed.

Decision owners: project maintainers

Related specification sections:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for principals, sessions, and the managed-user boundary
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Affected subsystem roadmaps:

- `plans/subsystems/on-prem-user-access-roadmap.md`
- `plans/subsystems/dashboard-roadmap.md`

## Context

Browser sign-in issues two cookies: `eggpool_session` and `eggpool_csrf`.
`rust/src/web_session.rs:270` emits the session cookie as
`HttpOnly; SameSite=Lax` and `:277` emits the CSRF cookie as
`HttpOnly; SameSite=Strict`. `architecture/deep-dive-on-prem-user-access.md:104`
records the asymmetry as deliberate, and `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md:175`
lists SameSite cookie behavior as part of the accepted security posture.

The two attributes differ in exactly one browser behavior that matters here.
`SameSite=Lax` cookies are delivered on a cross-site top-level `GET` navigation;
`SameSite=Strict` cookies are not. Both attributes withhold the cookie on a
cross-site unsafe-method request, so the difference is confined to
top-level navigation.

That difference is a total feature outage for an authenticated operator who
arrives by deep link. A signed-in admin who clicks a dashboard URL from a chat
client, a mail client, or a bookmark on another site causes the browser to send
`eggpool_session` — the page renders — and to withhold `eggpool_csrf`. The
renderers derive the hidden CSRF field from the cookie alone
(`rust/src/server/dashboard/admin.rs:206`, `:243`, `:348`), so the field is
empty, and `csrf_valid` (`rust/src/server/dashboard/admin.rs:153`) requires both
the supplied field and the cookie to verify against the session digest. Every
mutation on the page then fails with `403 Invalid request token.` The operator
sees a fully rendered dashboard whose buttons all reject.

The failure is not recoverable by a retry on the same page and it is not
distinguishable from an expired session. It is also not repairable from the
server side: `web_session::issue` persists only the CSRF digest
(`rust/src/web_session.rs:137`), so the plaintext token exists only in the
login response, and the cookie is set exactly once, at
`rust/src/server/auth.rs:119-126`. The server has no way to re-emit the token
value, and therefore no way to re-emit the cookie with a corrected attribute,
without changing what is persisted.

The second defect is in the sign-in throttle. `web_session::login_allowed`
(`rust/src/web_session.rs:300`) and `record_login_failure`
(`rust/src/web_session.rs:320`) key the per-username bucket on
`username_throttle_key` (`rust/src/web_session.rs:411`), which folds the peer IP
into the key. The documented limit — "10 failures per username and 30 per peer
IP" — is therefore enforced per (username, source IP) pair, not per account. A
distributed attempt against one account is bounded only by the per-IP limit of
30, and each additional source address resets the per-username budget to 10. A
legitimate operator whose source address rotates also never reaches the
documented limit. Separately, a successful sign-in does not clear the bucket
(`web_session.rs` has no success recorder). Failures therefore accumulate
across a successful sign-in: a run of failures that ends in a successful
sign-in still leaves the account's budget consumed, and ten typist failures
lock an account out for the full 15-minute window.

Neither defect is a drafting accident in a security control that does not work.
Both are the boundary between what the server checks and what the browser
delivers.

## Decision drivers

- An authenticated operator's mutations must work regardless of how they
  navigated to the page. A control that depends on the referrer the browser
  chose to attach is not a usable control.
- CSRF protection must remain enforced by the server, not by a browser cookie
  attribute. The double-submit token is defense in depth; the authoritative
  defense is the same-origin check the middleware already applies to every
  unsafe method (`rust/src/server/middleware.rs:125`).
- The server must not have to persist a secret in order to operate its own
  session. Widening what is stored to fix a delivery attribute would trade one
  defect for a larger one.
- A throttle that is documented as per-username must be per-username.
- The change must not weaken the rejection of cross-site unsafe requests, and
  must not weaken the transport gate or the credential check.

## Considered options

### Option A — Deliver the CSRF cookie as `SameSite=Lax`; keep the double-submit token and the server-side same-origin check

The CSRF cookie becomes `HttpOnly; SameSite=Lax`, matching the session cookie.
The cookie is then delivered on the cross-site top-level navigation that
already delivers the session cookie, so the rendered field and the cookie agree
again.

The security posture is unchanged in the case the attribute was meant to
protect: `Lax` and `Strict` behave identically for cross-site unsafe requests,
so an attacker's cross-site form POST still carries neither cookie and is
still rejected by the middleware's origin check before any handler runs. The
only newly delivered request is a cross-site top-level `GET`, which is a
read: it renders a page and mutates nothing.

### Option B — Keep `SameSite=Strict` and let the server re-issue the CSRF token

The server would mint a fresh CSRF token on each authenticated render, replace
the session row's digest, and set the cookie again. This reaches an
already-signed-in browser immediately.

It costs a durable write on every authenticated page render, on the single
SQLite gate the repository treats as a performance boundary. It also requires
either persisting the token in plaintext so the server can re-emit it, or
rotating it per render, and the first of those widens the blast radius of a
session-row disclosure. It replaces a delivery defect with a write-amplification
and secret-lifetime problem.

### Option C — Keep `SameSite=Strict` and accept that a deep-linked operator gets a non-functional page

The administrator changes the cookie attribute, or reloads from a same-site
navigation, and the page recovers. No code changes.

The operator's first experience after following a link is a dashboard whose
every action returns `Invalid request token.` The failure is not self-evident,
is not reported in any way that distinguishes it from an expired session, and
costs an operator escalation to diagnose. For a console whose whole purpose is
administrative action, a reachable-but-inert console is worse than an
unreachable one.

### Option D — Make the session cookie `SameSite=Strict` as well

The two cookies then behave identically: a cross-site deep link is not
authenticated at all, and the operator sees the sign-in page.

This is internally consistent, but it changes the documented session-cookie
contract (`architecture/deep-dive-security.md:58` already describes both
cookies as `SameSite=Lax`) and removes deep-link arrival for a console that is
otherwise reachable by bookmark, by chat client, and by mail client. It treats a
delivery attribute as the wrong lever for a delivery problem.

## Decision

Option A is selected, together with an account-scoped sign-in throttle.

- The CSRF cookie is emitted as `HttpOnly; SameSite=Lax`, matching
  `eggpool_session`. Both gain `Secure` under the existing determination.
- The double-submit CSRF token remains: the session's CSRF digest must still
  verify against both the submitted form field and the cookie.
- The server-side same-origin check on unsafe methods remains the authoritative
  CSRF defense. No change to `same_origin`, to the transport gate, or to the
  credential verification path.
- The per-username throttle bucket is keyed on the username alone. The per-peer
  bucket stays keyed on the peer IP. Both limits and the 15-minute window stay
  as they are.
- A successful sign-in clears the account's failure bucket, so a run of failures
  that does not end in a successful sign-in is what consumes the account's
  budget, and a later run of failures starts from a full budget. The per-peer
  bucket is deliberately not cleared on success: clearing it would let any
  valid account reset an address's cross-account failure budget and thereby
  multiply that address's brute-force throughput without bound. Because a
  success cannot occur while the per-peer bucket is exhausted, the per-peer
  bound is unconditional.

The deeper contract this records is that browser-session security rests on
server-side checks over the request, and that a cookie attribute is a delivery
hint, not a security boundary. A cookie attribute may narrow delivery; it may
never be the only thing standing between a cross-site request and a mutation.

## Consequences

### Positive

- An authenticated operator's mutations work regardless of how they reached the
  page, including by cross-site deep link.
- The cross-site unsafe-request posture is unchanged: neither cookie is
  delivered on a cross-site POST, and the origin check still rejects the
  request before a handler runs.
- The throttle now bounds an account, which is what it is documented to do.
- A successful sign-in clears the account's counter, so a legitimate operator
  who recovers from a run of typos starts the next run with a full budget and
  is not locked out by a stale counter.
- The security deep dive's description of both cookies as `SameSite=Lax`
  becomes accurate.

### Negative

- The CSRF cookie is now delivered on a cross-site top-level `GET` navigation.
  A page rendered by that navigation carries a usable CSRF field. This is
  accepted because the request is a read, and because the server rejects the
  unsafe methods that follow it.
- The throttle is stricter against an account than against a source address. A
  shared NAT or a rotating address no longer gives an attacker a fresh
  per-username budget; it may also cause a legitimate operator behind a
  rotating address to reach the account limit sooner than before. No limit is
  raised. The account limit stays at 10 failures and the per-peer limit stays
  at 30 over the same 15-minute window, so a single address can exhaust one
  account's budget on its own. The per-peer limit bounds that address's blast
  radius: at 30 failures per window against 10 failures per account, one
  address can lock out at most three accounts per window.
- A correct password is still refused once the account's budget is exhausted,
  because the account bucket is a hard refusal consulted before the credential
  check. That is the intended behavior of an account-scoped brute-force
  limiter. The alternative — verifying the credential whenever the account
  bucket is exhausted — would make the account bound advisory and raise an
  address's guess budget against a targeted account from 10 to 30, which
  defeats the account scoping this decision adopts.

### Neutral or deferred

- The session cookie's attribute, its 12-hour idle bound, its 7-day absolute
  expiry, and the 15-minute `last_used_at` refresh interval are unchanged.
- The cookie `Secure` determination, the transport gate, the credential
  verification path, and the password hashing parameters are unchanged.
- The `eggpool_csrf` cookie's `Max-Age` continues to match the session's
  absolute expiry.
- The dashboard's rendering, theme delivery, and asset contracts are out of
  scope here; they are planned separately.

## Compatibility and migration

No storage migration. No table, column, index, or schema version changes. No
configuration key is added, removed, or reclassified.

The attribute change applies to cookies issued at sign-in. A browser that
already holds a `SameSite=Strict` CSRF cookie keeps that stored cookie until
it expires; the stored attribute, not the value, is what governs delivery, so
the server cannot correct it in place. Because the CSRF cookie's `Max-Age`
already matches the session's 7-day absolute expiry, and the cookie and the
session are set together at sign-in, the affected population is bounded by the
remaining lifetime of a session that was already signed in before the change
lands. Those operators get the corrected attribute when they next sign in.

An operator who is signed in across the change and follows a cross-site deep
link before re-signing keeps the inert-page symptom until that sign-in. The
deployment note must therefore say that the fix takes effect at the next
sign-in, not that it takes effect on restart.

Clearing both cookies at sign-out and on password change already happens
(`rust/src/server/auth.rs:409`), so an operator who signs out and back in
picks up the new attribute immediately.

No published API, field, endpoint, or response shape changes.

## Security and reliability implications

The CSRF protection is unchanged in strength and unchanged in ordering: the
middleware's same-origin check on unsafe methods still runs before the
handler, and the double-submit token check still runs inside the handler.

The failure mode that this decision removes is a silent availability defect in
the administrative surface. The failure mode it introduces is that the CSRF
cookie is delivered on one class of request it previously was not. That request
is a top-level `GET` navigation. It cannot by itself cause a mutation: the
mutation would still have to be a same-origin unsafe request carrying both a
matching form field and the cookie.

The throttle change makes the lockout an account property. A distributed
attacker against a single account now meets one budget rather than one budget
per source address, which is the intended bound. The account lockout remains a
hard refusal: once the account's budget is exhausted, an attempt is refused
before the credential is checked, so a correct password does not break an
active lockout. The success-clear prevents the counter from being carried
across a successful sign-in; it does not shorten or suspend a lockout that is
already in force. The counter's process-local lifetime is unchanged: it is not
persisted, so a restart clears it, and that remains a known property rather
than a new one.

No credential, prompt, raw body, or cache key enters any of the affected
paths. The throttle keys are digests of the username and peer address, as
before.

## Verification

- A test asserts that a sign-in response emits `eggpool_csrf` with
  `SameSite=Lax` and `HttpOnly`, and `eggpool_session` with the same
  attributes, and that `Secure` is added under the existing determination.
- A test asserts that a request carrying the session cookie but no CSRF cookie
  renders a page whose hidden CSRF field is empty, and that a mutation with
  that empty field is rejected — pinning the current behaviour so a future
  change to the derivation is deliberate.
- A test asserts that a request carrying both cookies, as a browser would
  after the change, renders a page whose hidden CSRF field verifies against the
  session digest and that the mutation succeeds.
- A test asserts that a cross-site unsafe request, carrying no cookie, is
  rejected by the origin check before the handler runs.
- A test asserts that the per-username bucket is shared across two different
  peer addresses and that the per-peer bucket is not.
- A test asserts that a successful sign-in clears the account's failure
  counter, and that it does not clear the per-peer bucket.

## Supersession

None. This record supersedes the `SameSite=Strict` CSRF-cookie decision
recorded in `architecture/deep-dive-on-prem-user-access.md:104-106` and the
`SameSite/HttpOnly/Secure cookies` clause of
`plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md:175` only
with respect to the CSRF cookie's `SameSite` attribute and the sign-in
throttle's bucket key. ADR-0002 remains accepted and otherwise unmodified.
