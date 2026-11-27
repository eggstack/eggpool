# Provider OAuth Milestone 002 — Headless device-code login

Status: blocked

Repository baseline: 5b212d2d69137b99ed39f0dbeab10331c839e54e

Source roadmap:

- plans/subsystems/provider-oauth-roadmap.md#milestone-2--headless-device-code-login

Long-term requirements:

- plans/000-long-term-specification.md#1-product-definition
- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms

Applicable ADRs:

- plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md (proposed; hard blocker)
- plans/adrs/ADR-0010-personal-upstream-account-custody.md (accepted context only; does not authorize OAuth)

Primary class: capability

## 1. Objective

After M001 and the policy gate close, let an authorized headless CLI user complete an approved OAuth device-code grant, atomically store it against the intended account, and use it through M001's account-scoped token resolver.

## 2. Why this milestone is ready

It is not ready. Hard dependencies: accepted ADR-0012 must authorize the account owner/custody class, provider profile, and device grant; M001 must close with a stable credential-manager interface. No consumer-subscription OAuth account is presumed pool-eligible.

The current CLI has an existing personal-upstream command family under eggpool users upstream. If ADR-0012 chooses owner-bound accounts, use that owner/grant path rather than creating a second account-ownership system. Do not silently overload static eggpool connect or the separate eggpool-connect desktop helper.

## 3. Current implementation evidence

- rust/src/cli.rs exposes Clap commands and the users upstream family; current connect/logout account operations are static-key oriented.
- rust/src/operations/config_mutation.rs implements local static account selection and file/config transactions; it does not implement OAuth polling.
- docs/providers.md excludes OAuth-only consumer integrations and device-code/browser flows.
- eggpass-auth 0.1.1 exposes request_device_code and cancellable poll_device_authorization primitives over a caller-provided Eggfetch client.
- eggpass 0.1.3 src/auth/flow.rs prints the verified URL and user code, treats opening a browser as best effort, validates verification hosts, honors provider expiry, and uses browser-loopback only when the device endpoint is unavailable for that provider. Its src/cli/commands/login.rs selects device mode by provider capability. Reference: https://github.com/eggstack/eggpass/tree/664207cd898432024f43dff4910ac3fd06c54b6e.

## 4. Invariants that must not regress

- No local browser, callback listener, browser binary, or public endpoint is required for device flow.
- Display only the verification URL and user code; never display or log device_code, access/refresh token, or raw token response.
- Validate verification URLs against the approved provider host allowlist before display or best-effort open.
- Honor server expiry, minimum polling interval, slow_down, denial, and cancellation; never busy-poll.
- Save only after a successful validated grant. Failed/canceled re-login preserves the last valid credential.
- Bind the credential to the exact accepted account and owner; no shared-pool assumption or cross-account fallback.
- Keep provider credentials out of SQLite, logs, diagnostics, and provider-profile metadata.

## 5. Scope

### In scope

- A headless login operation in the approved account CLI surface, with explicit provider/account selection and non-secret status output.
- Device-code request, verified URL/code display, polling, cancellation, expiry, denial, and success handling.
- Atomic credential commit through the M001 interface and cleanup of transient device state.
- Re-auth behavior that does not destroy an existing working credential until replacement succeeds.
- Deterministic local OAuth-stub and CLI process tests plus documentation for remote-browser workflow.

### Explicitly out of scope

- An unapproved provider, grant, or consumer-subscription account class.
- Desktop loopback/callback flow (M003).
- Public HTTP callback endpoints, arbitrary verification URLs, API-key fallback, or raw token import.
- A second token store, SQLite secret columns, or new transport implementation.
- Automatic inference retry after a generic 401 unless accepted ADR-0012 and M001 define it.

## 6. Required production changes

Add the headless operation to the CLI/account surface selected by ADR-0012. In the proposed owner-bound model, authorize it through the existing personal-upstream grant and owner binding. Require an explicit target account identity; provider and endpoint choices come only from the approved bundled profile or validated config.

Use eggpass-auth 0.1.1's async device-code primitives with EggPool's host-owned Eggfetch client and runtime. Present the normal verification URI and user code in bounded terminal output; opening the browser may be attempted but is nonessential and failure must not stop a headless login. Follow device expiry and server polling intervals, including slow_down; connect Ctrl-C/cancellation to the library cancellation token and clear transient state.

Check the returned URL host against the exact approved provider host set even if the library performs its own validation. Do not print the device authorization secret or full token response. On grant success validate issuer/client/account metadata, then call M001's atomic save. Only after save succeeds report the account label and freshness/expiry summary. On denial, timeout, bad URL, canceled poll, or failed save, preserve the previous credential and report a bounded non-secret error.

Do not implement generic device flow for every profile. Provider capability and host allowlist must be explicit and reviewed in the accepted policy/profile contract. Device endpoint unavailability must not silently change to another grant unless that exact fallback is authorized; desktop fallback belongs to M003.

## 7. Ordered work packages

### Work package A — CLI authorization boundary

Intent: Expose login only to the principal/account class authorized by ADR-0012.

Required changes: Add command/flags under the approved existing account family, retain default-deny grants, refuse unknown provider/account identities, and avoid placing secrets in argv or environment dumps.

Acceptance evidence: CLI contract tests show unauthorized, disabled, wrong-owner, and unknown-provider requests make no network call and write no credential.

### Work package B — Device-code presentation and polling

Intent: Complete login without local browser or callback-server requirements.

Required changes: Request a device code, validate/present URL and user code, best-effort open only after validation, poll through the library with bounded cancellation/expiry, and sanitize terminal errors.

Acceptance evidence: Headless test runs with browser opening unavailable; the user can complete the stub flow using only printed URI/code; polling respects interval/slow_down and stops on expiry, denial, and cancellation.

### Work package C — Atomic account save and re-login

Intent: Make login durable only after grant success.

Required changes: Pass library Credentials to M001's store; validate provider/account identity; preserve old credential on failure; update only the target account.

Acceptance evidence: Restart reads the new grant; failed, denied, expired, or interrupted re-login retains the prior grant; no unrelated account changes.

### Work package D — User-facing contract and security tests

Intent: Prove the CLI is safe for a remote/headless host.

Required changes: Add process-level output/exit tests, URL-allowlist negative cases, redaction assertions, and docs for using a separate browser/device.

Acceptance evidence: No device_code or tokens appear in stdout/stderr/logs; command exits distinctly on denial/expiry/interruption; docs match the flow.

## 8. Failure, cancellation, restart, contention semantics

Before grant completion no persistent state changes. Cancellation or expiry closes device polling and leaves any prior credential intact. After successful grant, atomic save is the commit point; a CLI disconnect after that point does not roll it back. A storage failure reports failure without token material. Concurrent device logins for one account obey M001's account lock/version contract; a stale flow cannot overwrite a newer accepted credential. Server restart during authorization stops the CLI flow; it must not publish an incomplete token set. Device polling never substitutes a different account.

## 9. Compatibility and migration

No HTTP API change. Existing static API-key connect/logout remains unchanged. Add only the explicit OAuth CLI path and its docs after policy acceptance. Reuse M001 storage; do not create a parallel auth file or token column. No provider profile is added without the accepted provider matrix.

## 10. Required tests

- CLI process tests for missing flags, invalid account/provider, denied grant, success, cancellation, expired code, and bounded diagnostics.
- Local OAuth stub tests for verification URL/user code, pending, slow_down, success, denial, network failure, short expiry, and cancellation.
- Invalid HTTP, untrusted host, userinfo, malformed, or provider-modified URLs are refused before browser open.
- No browser installed/open failure still permits remote-browser completion.
- Failed re-login leaves current credential unchanged; successful re-login atomically replaces only one account.
- Owner/grant isolation and no pooled consumer-subscription selection.
- Assert device_code, access/refresh/ID tokens, and raw provider responses are absent from all CLI output and diagnostics.
- Run M001's provider_oauth, CLI contract, account/routing, and full serial workspace tests.

## 11. Required verification commands

    cargo fmt --manifest-path rust/Cargo.toml --all -- --check
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
    cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
    cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
    cargo test --manifest-path rust/Cargo.toml --test provider_oauth -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
    cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
    cargo tree --manifest-path rust/Cargo.toml -e features -i eggfetch-core
    cargo tree --manifest-path rust/Cargo.toml --duplicates
    cargo deny --manifest-path rust/Cargo.toml check
    cargo build --manifest-path rust/Cargo.toml --locked --release
    git diff --check

If docs/providers.md or README.md changes, also run uv run python scripts/validate_release_docs.py. Compare audit output with baseline; add no new exception.

## 12. Documentation updates

- docs/providers.md: list only the approved provider/account/flow and preserve exclusion for everything else.
- CLI docs: explain no-browser usage, verification URL/code, expiry, cancellation, and separate-device completion.
- Backup/restore and account-status guidance: explain accepted token-store behavior without exposing tokens.

## 13. Acceptance criteria

- An authorized headless user completes an approved device flow without a local browser or callback listener.
- Only an approved account receives the credential; no pooled consumer account is implied.
- Failed/canceled login preserves the previous credential; successful save is atomic and survives restart.
- Device URLs are validated, polling rules followed, and secrets remain absent from output/persistence/diagnostics except the approved store.
- Static-key behavior and the minimal Eggfetch feature profile remain unchanged.

## 14. Stop conditions

Stop until ADR-0012 is accepted and M001 is closed. Stop if a provider lacks a reviewed device profile, verification-host allowlist, or account policy; if the crate requires unsupported feature expansion; if the flow needs a local callback despite being scoped headless; if successful login would enter a shared pool without explicit approval; or if failed/canceled login can overwrite the old credential. Do not improvise provider fallback.

## 15. Closure evidence required

Record the accepted policy and provider matrix, exact CLI output/exit evidence, local OAuth-stub scenarios, restart and failed-relogin behavior, owner isolation, secret absence, Cargo checks, docs changes, and registry promotion/blocked-work audit.

## 16. Handoff notes

The latest checked crate is eggpass-auth 0.1.1; Eggpass CLI 0.1.3 at commit 664207cd898432024f43dff4910ac3fd06c54b6e is a reference only. Re-read current provider profile and CLI owners before implementation. Device flow must work with no browser on the host. Keep tests serial and preserve unrelated working-tree changes/artifacts.
