# Security Hardening Milestone 001 — Provider request credential-header exclusion

Status: active

Repository baseline: `dcbd820e9b978d47bd43d237eded1f488d3449ee`

Source roadmap:

- `plans/subsystems/security-hardening-roadmap.md#milestone-1--provider-request-credential-header-exclusion`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  item 8, secret-free diagnostics
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining`

Applicable ADRs:

- None required. This is an internal header-policy correction: no public
  contract, no dependency, no durable schema, and no authentication decision
  changes.

Primary class: invariant

## 1. Objective

Stop forwarding inbound browser-state credential headers to model providers.
The concrete outcome: no `Cookie` or `Cookie2` header from a client reaches a
provider on finite, streaming, or compact inference, while the existing
authorization, host, framing, and route-session exclusions keep working.

## 2. Why this milestone is ready

No dependencies. The two filter functions each have one owner and one call
surface, and the observed-upstream fixture already proves header forwarding
end to end.

## 3. Current implementation evidence

- `rust/src/server/inference.rs` — `filtered_incoming_headers` copies every
  header except a ten-entry `BLOCKED` list. `cookie` is absent.
- `rust/src/coordinator/attempt.rs` — `add_forwarded_headers` copies every
  incoming header except `HOP_BY_HOP_HEADERS`, `LOCAL_HEADERS`, and
  connection-nominated names. `cookie` is absent from both lists.
- `rust/src/coordinator/finite.rs` — `filter_response_headers` already drops
  `set-cookie` and `cookie` from provider responses, so the leak is
  request-direction only.
- `rust/tests/coordinator_c013.rs` observes the real upstream request text and
  asserts that `authorization` is replaced, `x-eggpool-route-session` is
  dropped, and a connection-nominated header is dropped. It has no cookie
  case.
- `rust/tests/server_transport.rs` exercises the downstream listener; the
  audit did not find a cookie-forwarding assertion there.
- `architecture/deep-dive-security.md` states that the filter drops exactly
  ten entries.

A client that carries EggPool-origin cookies — a same-origin browser call, an
SDK with a cookie jar, or a user's `curl -b` — therefore sends
`eggpool_session` and `eggpool_csrf` to a third-party provider. The session
cookie is a seven-day dashboard credential. Conditions are narrow, but no
provider needs the header.

## 4. Invariants that must not regress

- The provider still receives exactly one authorization header, built from the
  selected account credential; a client-supplied `Authorization` or
  `x-api-key` never survives.
- `Host`, `Content-Length`, `x-eggpool-route-session`, hop-by-hop headers, and
  connection-nominated headers remain excluded.
- Response-side `set-cookie` and `cookie` stripping is unchanged.
- No request or response body buffering is introduced.
- Header handling remains pure and allocation-comparable to the current copy
  loop.

## 5. Scope

### In scope

- Add `cookie` and `cookie2` to the server pre-filter and the attempt filter.
- Add unit coverage for both filters and an observed-upstream integration case.
- Correct the header-count sentence in the security deep dive.

### Explicitly out of scope

- An allowlist/allow-by-default header policy.
- CSRF header names, `Origin`/`Referer` forwarding, or CORS behavior.
- Provider response cookie handling beyond the existing strip.
- Any change to session issuance, cookie attributes, or inference auth.

## 6. Required production changes

- `rust/src/server/inference.rs`: extend `BLOCKED` with the cookie family.
- `rust/src/coordinator/attempt.rs`: extend `LOCAL_HEADERS` with the same
  names. Keep the two lists independent; they serve different layers and a
  later refactor is out of scope.
- No config, migration, protocol, or dependency change.

## 7. Ordered work packages

### Work package A — Close both filters

Intent: reject the header at the earliest server filter and again at the last
forwarding point.

Required changes: add `cookie` and `cookie2` to both lists; keep the existing
case normalization in `add_forwarded_headers`.

Acceptance evidence: a unit test in `rust/src/coordinator/attempt.rs` builds
an attempt with `Cookie: eggpool_session=…` and asserts the prepared headers
have no cookie name; a server-side unit or transport test asserts the
pre-filter output has no cookie name.

### Work package B — Prove the end-to-end behavior

Intent: prove the leak is closed on every inference surface.

Required changes: extend `rust/tests/coordinator_c013.rs` (or the narrowest
existing observed-request fixture) with a client cookie header on finite,
streaming, and compact cases; assert the observed upstream request text
contains no `cookie:` line and still contains the expected account
authorization.

Acceptance evidence: the new assertions fail on the baseline and pass after
Work package A.

### Work package C — Correct the documentation claim

Intent: keep the security deep dive truthful.

Required changes: update the blocked-header count and the listed names in
`architecture/deep-dive-security.md`; no other prose change.

Acceptance evidence: the document lists the cookie family and no longer says
ten entries.

## 8. Failure, cancellation, restart, contention semantics

Header filtering is synchronous and pure. There is no partial failure,
cancellation window, restart state, or contention path. A request that
previously carried a cookie behaves identically apart from the absent header.

## 9. Compatibility and migration

No migration. No configuration. A client that relied on the provider seeing
its cookies would observe a behavior change; no provider profile in the
repository uses request cookies, and the response side already strips them.

## 10. Required tests

- Unit: `rust/src/coordinator/attempt.rs` cookie exclusion and preservation of
  the existing blocked set.
- Unit or transport: `rust/src/server/inference.rs` pre-filter exclusion.
- Integration: `rust/tests/coordinator_c013.rs` observed upstream request for
  finite and streaming; add compact if the fixture supports it.
- Negative: a duplicate cookie header is dropped in full, not truncated.
- Full serial workspace suite; no-default check/clippy.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test coordinator_c013 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
```

## 12. Documentation updates

- `architecture/deep-dive-security.md` — header list and count.
- `plans/registry.md` — status promotion when the plan starts and closes.
- No `docs/` change: the operator never configures provider cookies.

## 13. Acceptance criteria

- A request carrying `Cookie` or `Cookie2` produces an upstream request with
  neither header, on finite, streaming, and compact inference.
- The account credential is still the only `Authorization` value.
- The existing dropped-header set is unchanged.
- Default and no-default suites pass, and the new test fails on the baseline.

## 14. Stop conditions

Stop and report if:

- a bundled provider profile requires request cookies (none does);
- the fix would require a shared header-policy refactor across the server and
  coordinator crates;
- an observed-request fixture cannot avoid recording credential material in
  test output.

## 15. Closure evidence required

- The exact filter diff.
- The new test names and the observed-request assertion snippets (no real
  credentials).
- Command output for the focused targets and the full serial suite.
- A statement that response-side cookie stripping was not changed.

## 16. Handoff notes

Two modules, two lists, one observed-request fixture. Tests run serially. Do
not log the client cookie while debugging the test.
