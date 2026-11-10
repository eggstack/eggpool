# On-Prem Member Access Milestone 005 — Personal upstream browser grant

Status: implemented

Repository baseline: `9da5e2d383c73035850c37f8125c145b8b7ff9ff`

Source roadmap:

- `plans/subsystems/on-prem-member-access-roadmap.md#milestone-5--personal-upstream-browser-grant`

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`

Applicable ADRs:

- `plans/adrs/ADR-0010-personal-upstream-account-custody.md`
- `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`

Primary class: capability

## 1. Objective

Let an administrator set the personal-upstream grant in the browser, and
let a granted member add or remove their own bundled-provider account
there. A denied member sees no secret field.

## 2. Why this milestone is ready

It is not ready. Hard dependency: milestone 004 is blocked on ADR-0010 and
owns custody, the binding, the file transaction, and eligibility. This
milestone starts only after 004 is closed and its CLI contract is the one
shipped. The browser calls that contract. It does not write `.env` or
TOML by a second path.

## 3. Current implementation evidence

Member mutations already use a browser session, CSRF, and the transport
gate. `issue_personal_api_token_from_browser` is the pattern for a
member-scoped form. `admin_only` guards user administration.
`personal_credential_allowed` in `rust/src/server/middleware.rs` admits
personal API tokens only to inference, discovery, the integration profile,
and `/api/user-budget`.

`/my-account` renders client credentials and does not mention upstream
accounts. `/users` does not mention a grant. Milestone 003, if it lands
first, groups the roster; this milestone adds a grant section beside that
roster rather than replacing it.

There is no browser route for upstream custody because milestone 004 has
not shipped.

## 4. Invariants that must not regress

- Personal API tokens do not gain a mutation scope.
- CSRF, same-origin, and the local-or-trusted-HTTPS gate stay on every
  new POST.
- The secret is not rendered back, logged, or stored in the session.
- A member cannot set the instance grant or another user's grant.
- Eligibility and file restore behavior remain milestone 004's. This
  milestone does not catch a failed rehash and continue.

## 5. Scope

### In scope

- Administrator controls for the instance default and for one user's
  grant, on the users surface.
- A member section on `/my-account` to list, add, and remove that member's
  own accounts when the effective grant is `allow`.
- Route-role entries so those pages stay on the member or admin surface
  they belong to.
- Tests that a denied member and a personal API token are refused.

### Explicitly out of scope

- A new custody design, a new provider, or a custom base URL.
- Changing milestone 004's add/remove order.
- Showing the secret after submit.
- Letting the member set weight, proxy, or priority.

## 6. Required production changes

Add browser routes:

- Admin: instance default, per-user grant, and remove, posted under the
  existing `/admin/users/` family or a sibling `/admin/upstreams/` family.
  They call the same service the CLI calls.
- Member: `GET` list on `/my-account` and `POST` add/remove under
  `/my-account/upstreams`. Add them to the member account-surface allow
  list in `rust/src/server/middleware.rs`. Do not add them to
  `personal_credential_allowed`.

The member form posts `provider_id`, `name`, and `secret`. `provider_id`
is a `<select>` of bundled profile ids. The secret input is
`type="password"` and `autocomplete="off"`. On success, redirect back to
`/my-account` without the secret in the query string. On failure, the
error page names the refusal and does not echo the secret.

When the effective grant is `deny`, `/my-account` shows that self-service
is off and renders no secret input and no remove button. Direct POST still
checks the grant and returns 403.

An administrator's users section shows the instance default, each user's
effective grant, and that user's personal account names. The administrator
can change the grant and can remove an account while the user's
self-service grant is deny. The administrator add-for-another-user action
may stay CLI-only if the browser remove and grant controls are present.
If a browser add-for-user form is included, it uses the same password
field rules and the same service, and it is `admin_only`.

Member navigation from milestone 003 gains no new top-level item. The
control lives on My account. If milestone 003 has not landed, this page
still must not add operator links to the member view.

## 7. Ordered work packages

### Work package A — Route gate

Intent: The new posts are browser-only and role-correct.

Required changes: Register routes in `rust/src/server/mod.rs` and extend
the member and admin match lists in `rust/src/server/middleware.rs`.
Reject a personal API token with the existing 401 or 403 used for other
dashboard posts.

Acceptance evidence: A personal token POST receives a refusal and does
not create a binding. An unauthenticated POST does not create a binding.
A member POST to the admin grant route receives the member 403 page.

### Work package B — Administrator grant controls

Intent: The administrator can allow or deny self-service without the CLI.

Required changes: Forms on the users page calling the milestone 004 grant
and remove operations. Render account names only.

Acceptance evidence: Setting deny hides the secret field and rejects a
later member add or remove. Accounts already bound to that user stay
selectable by that user until an administrator removes them or the user
is disabled. Those two operations are the milestone 004 withdraw paths.
A grant change does not delete files and does not withdraw bindings.

### Work package C — Member add and remove

Intent: A granted member can attach their own bundled-provider key.

Required changes: The My account section and posts described in section 6.
The handler reads the form secret, calls the 004 add/remove service, and
drops the secret from memory without writing it to the response.

Acceptance evidence: A granted member adds `opencode-go`, sees the account
name, and can complete an inference selection that uses it in the routing
test. Another member does not see the account and cannot select it. The
HTML response and the access log do not contain the secret. A denied
member has no secret input. Remove uses the 004 remove order.

## 8. Failure, cancellation, restart, contention semantics

The handler waits for the 004 service result. On failure it renders an
error and does not show a success list that includes the account. A
double submit is the 004 unique-name rejection. Browser disconnect after
the service commits is the same as a committed CLI add. Session
revocation uses the existing session fence so a disable that lands before
commit fails the write.

## 9. Compatibility and migration

No new table. No change to the inference body. New HTML routes are
additive. Personal tokens keep their current scopes.

## 10. Required tests

- `rust/tests/on_prem_admin_ui.rs` for grant rendering, denied-member
  absence of the secret field, admin-only grant mutation, and member 403
  on the admin route.
- `rust/tests/on_prem_sessions.rs` for CSRF failure and personal-token
  refusal.
- A routing assertion, reusing milestone 004's helper, that the account
  created through the form is owner-only.
- A response-body assertion that the submitted secret is absent.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run python scripts/validate_release_docs.py
```

Include `--no-default-features` clippy if the middleware or config path
changes in a way that touches proxy features. A pure HTML handler should
not need a dependency change.

## 12. Documentation updates

- `docs/deployment.md` small-team section: a granted member can add a
  personal bundled-provider account from My account; the administrator
  controls the grant.
- `architecture/deep-dive-on-prem-user-access.md` member surface list.
- `docs/api-reference.md` only if the new routes are documented as HTTP
  API. HTML forms do not require an inference-API change.

## 13. Acceptance criteria

- Default deny: My account has no upstream secret field.
- An administrator can allow one user. That user can add an OpenCode Go
  account in the browser and is the only principal routed to it.
- The administrator can deny the user again. The secret field is gone
  and a member add or remove is refused. An account the administrator
  has not removed stays selectable by that user.
- A personal API token cannot add or remove an upstream account.
- The secret appears in no HTML response, audit row, or log line produced
  by the new handlers.
- Amazon Bedrock is not offered in the provider select.

## 14. Stop conditions

Stop if milestone 004 is not closed. Stop if the browser path needs a
second writer for `.env` or TOML. Stop if the shipped 004 service treats
a grant denial as withdrawing existing bindings; report that mismatch
instead of changing custody in this milestone. Stop if the provider
select would include an id that is not in the bundled profile asset.

## 15. Closure evidence required

The grant, denial, cross-user, token, and CSRF tests. A captured form
submission whose secret string is absent from the response and the
database. Commands and results. Registry closure of the roadmap only when
001–004 are already closed or are closed in the same commit by their own
records.

## 16. Handoff notes

Read the closed 004 service and call it. A grant denial blocks member
add and remove and leaves existing bindings selectable. User disable and
administrator remove are the withdraw paths. If 004 shipped denial as a
withdraw, stop and report that mismatch instead of changing custody here.
Serial tests only. Keep the member navigation limited to member
destinations.
