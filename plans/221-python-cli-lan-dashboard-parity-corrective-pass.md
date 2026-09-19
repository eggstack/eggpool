# Plan 221 — Python CLI / LAN Dashboard Parity Corrective Pass

Date: 2026-09-19  
Status: implementation handoff  
Planning baseline: `68ddd656f38fb8d461554415135d221b6c6722a6`  
Priority: P1 operator UX / configuration-default correction / auth-boundary regression prevention  
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Restore several operator-facing behaviors that were either lost or frozen at the wrong historical point during the Python-to-Rust migration:

1. `eggpool connect` and the provider-selection step inside `eggpool onboard` must regain the polished interactive selector used by the Python implementation: `j/k` and Up/Down navigation, Enter to select, `q`/Esc to cancel.
2. The canonical server bind default must be `0.0.0.0`, not loopback.
3. A default browser request to the dashboard must render the dashboard rather than return `{"detail":"Invalid or missing API key"}`.
4. These LAN/dashboard usability corrections must **not** weaken authentication on inference or sensitive EggPool control/integration endpoints.

This is a corrective parity pass, not a request to restore the Python runtime or to redesign the CLI/server architecture.

## Historical authority and migration context

Use Git history as the behavioral oracle where current Rust behavior is ambiguous.

### Python interactive selector

The original provider selector was introduced by:

- `eafe279c35d5c1d613f21996cd2b308c07d72751` — “Add interactive provider connect command and config reload”

Important follow-up polish:

- `2f5527020f` — proper Unicode `↑/↓` menu instructions;
- `93efc88e5c` — raw-fd consistency and raw-mode line rendering;
- `b3cb8e83c0` — silent cancellation / Esc handling / API-key input polish;
- `5ffcb640e9` — shared account-selection abstraction.

The final Python selector lived in `src/eggpool/providers/connect.py` and displayed:

```text
Select a provider to connect:

  Use j/k or ↑/↓ to navigate, Enter to select, q/Esc to quit
```

with a highlighted current item and `> ` cursor.

### Python retirement boundary

The Python application/runtime was retired in:

- `39aec1c992b473651557c71914488f183b0aa855` — “Retire Python application and runtime assets”

Its parent:

- `c23a70961f4b7858fdb0264cfb27b7ea26a8a334`

is the final pre-retirement Python tree and remains useful for exact UX behavior. Do not restore any Python application files.

### Host/dashboard default history

Earlier Python behavior used LAN/public-oriented defaults. Commit:

- `efbddf94a8b2deb8d349467f8ce8e4a5f674e966` — “migrate from uvicorn to granian and make dashboard public by default”

explicitly made `dashboard.public = true`.

Later commit:

- `0e10425909` — “Implement lean defaults and conditional subsystems”

changed:

- `server.host`: `0.0.0.0` → `127.0.0.1`;
- `dashboard.public`: `true` → `false`;
- onboarding to ask whether LAN binding should be enabled.

The Rust migration preserved the late-Python loopback/private defaults but lost the interactive LAN-bind choice and reduced provider selection to numeric input.

For this corrective pass, the required product behavior is explicit and overrides the late-Python lean-default policy:

- canonical bind default: `0.0.0.0`;
- canonical dashboard default: public/read-only browser dashboard;
- inference and sensitive control surfaces: still authenticated.

## Current defects at the planning baseline

### A. Provider picker regressed to numeric/ID input

Authority:

- `rust/src/operations/config_mutation.rs::connect_with_transition`

Current behavior prints every provider plus a `selection {index}` line and prompts:

```text
Select provider (id or number, Enter cancels):
```

It then uses `stdin.read_line()` and `parse::<usize>()`.

Both:

- `eggpool connect`;
- `eggpool onboard`

reuse this path, so both expose the degraded UX.

### B. Rust has enough terminal support already; do not add a TUI framework

`rust/Cargo.toml` already depends on:

```toml
nix = { version = "0.31.3", default-features = false, features = ["signal", "term", "user"] }
```

The `term` feature already provides safe terminal/termios support. Do **not** add `dialoguer`, `inquire`, `crossterm`, Ratatui, or another full interaction dependency merely for a single selector.

Prefer a small internal selector built on:

- `std::io::IsTerminal`;
- existing `nix::sys::termios` support on Unix;
- bounded byte/event parsing;
- a line-oriented fallback when stdin/stdout is not an interactive terminal.

The crate forbids unsafe code; do not introduce unsafe terminal syscalls.

### C. Typed server default is loopback

Authority:

- `rust/src/config.rs`

Current:

```rust
const DEFAULT_HOST: &str = "127.0.0.1";
```

and `Config::default()` tests assert that value.

### D. Onboarding forcibly writes loopback

Authority:

- `rust/src/runtime.rs::onboard`

Current onboarding unconditionally calls:

```rust
config_mutation::set_server_value(path, "host", "127.0.0.1")
```

This means fixing only `DEFAULT_HOST` is insufficient.

### E. Browser dashboard is intentionally auth-gated by current defaults

Authority:

- `rust/src/config.rs::DashboardConfig::default`
- `rust/src/server/middleware.rs::requires_auth`

Current dashboard default:

```text
enabled = true
public = false
```

The middleware then requires auth for `/`, ordinary dashboard pages, and ordinary `/api/*` dashboard data when `dashboard_public == false`.

A browser GET without `Authorization` or `x-api-key` therefore correctly reaches the current 401 response:

```json
{"detail":"Invalid or missing API key"}
```

The route itself is not broken; the default policy is wrong for the required product behavior.

### F. Dashboard default authority is internally inconsistent

Authority:

- `rust/src/config.rs::DashboardConfig::default`
- `rust/src/operations/config_mutation.rs::read_dashboard_public`
- `rust/tests/operations_o004.rs`

Typed runtime default is currently `false`, but `read_dashboard_public()` falls back with:

```rust
.unwrap_or(true)
```

and the O004 test expects the missing setting to mean public.

This disagreement must be eliminated. There must be one canonical default.

## Required end state

After implementation:

### Interactive selection

On an interactive terminal, `eggpool connect` and the provider-selection phase of `eggpool onboard` display a selection menu closely matching the Python UX:

```text
Select a provider to connect:

  Use j/k or ↑/↓ to navigate, Enter to select, q/Esc to quit

  > OpenCode Go ...
    MiniMax ...
    ...
```

Required controls:

- `j`: next item;
- `k`: previous item;
- Down arrow: next item;
- Up arrow: previous item;
- Enter: return current item;
- `q`: cancel cleanly;
- Esc: cancel cleanly;
- Ctrl-C: preserve normal interruption semantics.

Navigation should clamp at the first/last entry rather than wrap unless the historical implementation clearly proves wrapping was intended. The final Python implementation clamped.

The provider list must retain the useful current Rust metadata: display name, URL, status, recommendation marker/notes as appropriate. Do not regress provider-template information merely to reproduce old formatting byte-for-byte.

### Noninteractive selection

When stdin or stdout is not a TTY:

- do not emit cursor-control ANSI sequences;
- preserve a deterministic line-oriented selection path;
- accept provider ID and numeric index for backward/script compatibility;
- EOF/empty input cancels without corrupting config;
- invalid selections return the existing bounded validation error.

Interactive mode should no longer advertise numeric entry as the primary UX.

### Server bind default

The effective default must be:

```toml
[server]
host = "0.0.0.0"
```

This must be true for:

- `Config::default()`;
- the canonical root `config.example.toml`;
- `config.sbc.example.toml` unless that profile has a documented, deliberate reason to override it;
- newly initialized configs;
- onboarding-created/updated configs.

Onboarding must not silently rewrite an explicit operator host back to loopback.

For a config created by onboarding, either rely on the canonical `0.0.0.0` value already present or explicitly set the same value. Prefer avoiding a redundant mutation.

### Dashboard browser default

The effective dashboard default must be:

```toml
[dashboard]
enabled = true
public = true
```

A normal browser request to `GET /` on a default installation must receive the HTML dashboard without an API key.

Ordinary read-only dashboard pages and their non-sensitive JSON data may inherit the public-dashboard exemption, matching the existing `dashboard.public` mechanism.

### Authentication boundary that must remain intact

Changing the dashboard default must **not** make the data plane or sensitive control plane public.

At minimum, preserve authentication for:

- all `/v1/*` inference/model routes currently classified as authenticated;
- `/api/integrations/*`;
- `/api/stats/runtime`;
- `/api/stats/update`;
- `/api/status`;
- any other route already deliberately special-cased as always-authenticated.

Do not remove server API-key generation from onboarding. The key remains required for inference/client integrations even when the human-facing dashboard is public.

`dashboard.public = false` must continue to restore authentication on ordinary dashboard pages/data for operators who want it.

The existing CLI:

```text
eggpool dashboard public --on
eggpool dashboard public --off
```

must continue to work with intuitive semantics:

- `--on` => public dashboard;
- `--off` => API-key-required dashboard.

## Implementation workstream 1 — Extract a narrow terminal selector

### Files

Primary:

- `rust/src/operations/config_mutation.rs`
- new narrow module under `rust/src/operations/`, preferably `terminal.rs`
- `rust/src/operations/mod.rs`

Do not move unrelated configuration mutation logic.

### Contract

Implement a small reusable selector such as conceptually:

```rust
select_one(title, options) -> Result<Option<usize>, TerminalError>
```

The exact API can differ, but it should:

- own raw-mode lifecycle;
- always restore terminal settings on success, cancel, error, and Ctrl-C propagation;
- render only bounded known strings;
- map key input to small explicit actions;
- contain no config/provider semantics.

Keep provider-specific formatting in `config_mutation.rs`.

### Raw-mode handling

Use the existing `nix` `term` feature. Do not add unsafe code.

Requirements:

- capture original termios;
- enter noncanonical/no-echo mode only while reading the selector;
- restore original termios in a scope guard/Drop path;
- flush writes explicitly;
- parse standard CSI Up/Down sequences;
- distinguish standalone Esc from an arrow prefix with a short bounded read/poll strategy;
- never block indefinitely after receiving Esc;
- render with `\r\n` while in raw mode to avoid cursor-column drift;
- hide cursor only if restoration is guaranteed; otherwise leave it visible.

Avoid terminal-size-dependent layout. No paging is required for the current provider inventory.

### Testability

Keep key-to-action decoding separable enough for deterministic unit tests. Do not build a generalized terminal framework or introduce a dependency-injection hierarchy solely for tests.

## Implementation workstream 2 — Rewire provider/account selection

### Provider connect

Replace the interactive numeric prompt inside:

- `rust/src/operations/config_mutation.rs::connect_with_transition`

with the shared selector when terminal-capable.

The returned index maps directly to the sorted provider template ID vector; preserve deterministic sorting.

After selection, retain all current behavior:

- local-provider custom ID/base-URL prompts;
- auth-mode handling;
- masked/hidden secret input;
- provider/account naming;
- atomic config mutation;
- canonical transition classification;
- live apply/restart reporting.

Cancellation must return the current no-op mutation result rather than becoming an error.

### Onboard

`rust/src/runtime.rs::onboard` should automatically inherit the same selector by continuing to call the canonical connect operation. Do not fork a second provider-selection implementation into `runtime.rs`.

### Logout/account selection

The Python implementation eventually reused the selector for account selection. The current Rust logout path still uses a numbered prompt when multiple accounts match.

If the new selector can be reused cleanly without expanding scope, migrate this prompt too so operator selection behavior is internally consistent. If doing so materially complicates the pass, leave logout unchanged and document that as explicit residual work rather than introducing an overgeneralized abstraction.

Provider connect/onboard parity is mandatory; logout parity is secondary.

## Implementation workstream 3 — Restore canonical LAN bind default

### Files

- `rust/src/config.rs`
- `rust/src/runtime.rs`
- `config.example.toml`
- `config.sbc.example.toml` if applicable
- config/default tests in `rust/src/config.rs`
- mutation/onboarding tests

### Changes

1. Set `DEFAULT_HOST` to `0.0.0.0`.
2. Update the canonical root config example.
3. Remember that `rust/build.rs` embeds the repository-root `config.example.toml` as `DEFAULT_CONFIG`; do not create a second copy under `rust/assets/config/`.
4. Remove the hard-coded `127.0.0.1` write from onboarding.
5. Verify `init-config` and onboarding both produce/effectively retain `0.0.0.0`.
6. Preserve explicit operator values on existing configs.
7. Update tests currently claiming `127.0.0.1` is the “Python contract”; the target is now an explicit EggPool product decision, not the late-Python lean-default snapshot.

Do not overload `[integrations].advertise_base_url`; architecture explicitly separates listen address from advertised client URL.

## Implementation workstream 4 — Restore public dashboard default without public inference

### Files

- `rust/src/config.rs`
- `rust/src/operations/config_mutation.rs`
- `rust/src/server/middleware.rs`
- `rust/src/server/mod.rs` only if tests/visibility require it
- `config.example.toml`
- `config.sbc.example.toml` if applicable
- `rust/tests/operations_o004.rs`
- relevant server/auth unit or integration coverage

### Changes

1. Set `DashboardConfig::default().public = true`.
2. Make `read_dashboard_public()` use the exact same implicit default.
3. Ensure canonical config examples explicitly state `public = true`.
4. Preserve `dashboard public --on/--off`.
5. Do not weaken the route classifier's always-authenticated cases.
6. Add explicit tests showing:
   - public default: `/` does not require an API key;
   - public default: ordinary dashboard data such as `/api/stats/summary` does not require an API key;
   - public default: `/v1/models` still requires an API key when a server key is configured;
   - public default: inference endpoints still require an API key;
   - public default: `/api/integrations/v1/profile` remains authenticated;
   - public default: `/api/stats/runtime`, `/api/stats/update`, and `/api/status` remain authenticated;
   - `dashboard.public = false`: ordinary dashboard HTML/data become authenticated again;
   - static assets and health/readiness preserve their current intended exemptions.

Prefer testing `requires_auth` as a small policy matrix plus at least one router-level HTTP assertion for the visible browser behavior. Do not duplicate the entire Axum route table in tests.

## Implementation workstream 5 — Fix stale contract language and config truth

### Files to inspect/update

At minimum:

- `README.md`
- `docs/configuration.md`
- `architecture/deep-dive-dashboard.md`
- `architecture/deep-dive-deployment.md`
- `architecture/overview.md` only if it states the old defaults
- `.opencode/skills/architecture/SKILL.md` only if its behavioral wording becomes stale
- `tests/fixtures/cli/contract-matrix.json` only if command/options change

Correct active docs that still claim:

- loopback is the ordinary default;
- LAN binding requires the old Python onboarding prompt;
- the default dashboard requires API-key headers;
- numeric provider selection is the expected operator workflow.

Do not edit historical plans/changelog entries merely because they describe the old policy correctly for their date.

Rename misleading test prose such as “current Python contract” when the test now describes the Rust/current-product contract. The Python tree is historical evidence, not runtime authority.

## Implementation workstream 6 — Regression coverage

### Focused config/control tests

Extend:

- `rust/tests/operations_o004.rs`
- `rust/tests/cli_contract.rs`
- config unit tests in `rust/src/config.rs`

Required assertions:

1. `Config::default().server.host == "0.0.0.0"`.
2. `Config::default().dashboard.public == true`.
3. missing `[dashboard].public` is interpreted identically by typed config and mutation helper.
4. `init_config` writes the canonical `0.0.0.0` / public-dashboard values.
5. explicit `dashboard.public = false` remains false and is not rewritten unexpectedly.
6. server-host changes remain restart-required under `config_reload_policy::classify_transition`.
7. dashboard-public changes retain their existing transition semantics.
8. connect cancellation produces no config change.

### Terminal selector tests

Add deterministic coverage for:

- j/down => increment/clamp;
- k/up => decrement/clamp;
- Enter => selected index;
- q => cancel;
- Esc => cancel;
- malformed/incomplete escape sequence => bounded cancel/ignore behavior, never hang;
- empty option list => no selection;
- rendered instructions contain the documented controls.

Where practical, add one Unix PTY-level test around raw-mode restoration. Keep it narrow and skip cleanly where PTY facilities are unavailable; do not make the whole suite depend on a real interactive terminal.

### HTTP auth-policy tests

Add a compact route-policy matrix around `requires_auth`. If visibility prevents direct policy testing, place the tests in the module rather than widening public API only for tests.

Also add one Axum/router-level test proving `GET /` on default config no longer returns 401.

## Security invariants

This pass intentionally changes **dashboard exposure**, not API authentication.

Must remain true:

- server API key generation remains in onboarding;
- data-plane inference cannot be invoked unauthenticated merely because the dashboard is public;
- integration profile cannot be read unauthenticated;
- runtime/update/status control data remain protected where currently protected;
- no API key is inserted into dashboard HTML/JS/query parameters/cookies;
- do not invent browser sessions, basic auth, login forms, or URL-token schemes;
- `dashboard.public` remains the sole ordinary dashboard-public switch;
- secrets remain absent from diagnostics, plans, tests, and committed fixtures.

Do not solve browser access by embedding the server API key into the page. The intended solution is the existing public-dashboard policy boundary.

## Dependency and footprint constraint

The preferred implementation requires **no new direct dependency** because `nix` already has the `term` feature enabled.

If implementation nevertheless proposes a new terminal crate, stop and justify it with:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
```

and measure release-binary size before/after.

A new full TUI/dialog framework is out of scope.

## Focused verification sequence

Run from repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib config -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib server -- --test-threads=1
```

If exact lib filters differ after implementation, run the closest narrow module targets rather than adding artificial test binaries solely to preserve these command strings.

Then run the repository baseline:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

No release rehearsal is required unless implementation changes packaging/dependency inputs.

## Manual acceptance check

On a Unix TTY using a temporary config/home:

1. Run `eggpool onboard`.
2. Confirm provider selection uses the interactive highlighted menu.
3. Confirm `j/k` and arrow navigation work.
4. Confirm Esc/q cancels cleanly and restores normal terminal echo/input.
5. Complete one provider connection with a test credential or a no-auth local provider.
6. Inspect generated config and confirm `server.host = "0.0.0.0"`.
7. Confirm dashboard is enabled/public by default.
8. Start EggPool.
9. Open/request `http://<LAN-IP>:11300/` without auth and receive HTML.
10. Request an authenticated-only route without a key and confirm 401.
11. Repeat with the correct API key and confirm success.
12. Run `eggpool dashboard public --off`; confirm `/` now requires auth.
13. Run `eggpool dashboard public --on`; confirm ordinary dashboard access is public again.

Do not use production credentials in captured evidence.

## Out of scope

Do not use this pass to:

- restore the Python runtime;
- redesign provider templates;
- change routing/retry/finalization behavior;
- alter `configsetup`/remote-client advertisement semantics;
- implement TLS termination;
- add RBAC, user accounts, dashboard login sessions, or CSRF machinery;
- expose sensitive integration/runtime/update/status APIs publicly;
- build a general TUI library;
- refactor unrelated `runtime.rs` or `config_mutation.rs` code for style;
- change the server port;
- change update/rollback behavior.

## Acceptance criteria

The plan is complete when all of the following are true:

1. Interactive `eggpool connect` presents a highlighted provider menu with j/k, Up/Down, Enter, q/Esc controls.
2. `eggpool onboard` uses the same provider selector without a duplicated implementation.
3. Terminal settings are restored on success, cancellation, and errors.
4. Non-TTY use retains a simple deterministic ID/number fallback without ANSI/raw-mode behavior.
5. No new TUI dependency is introduced unless separately justified by dependency/size evidence.
6. The canonical server bind default is `0.0.0.0` in typed config and generated config.
7. Onboarding no longer forces `127.0.0.1`.
8. The canonical dashboard default is `public = true`.
9. A default unauthenticated browser GET to `/` renders the dashboard rather than returning “Invalid or missing API key”.
10. `/v1/*` inference/model routes remain authenticated where they are currently intended to be authenticated.
11. `/api/integrations/*`, `/api/stats/runtime`, `/api/stats/update`, and `/api/status` remain authenticated with a public dashboard.
12. `dashboard public --off` still makes ordinary dashboard routes require the API key, and `--on` restores public access.
13. `DashboardConfig::default()` and `read_dashboard_public()` agree on the implicit default.
14. Canonical config examples, active operator docs, and tests agree on host/dashboard defaults.
15. Focused tests and the full serial workspace/tooling baseline pass.

## Handoff note

Treat this as a behavior correction with a deliberately narrow blast radius.

The key architectural point is that EggPool already has the right separation for the dashboard fix: `dashboard.public` controls ordinary dashboard visibility while `requires_auth` contains explicit always-authenticated API classes. Preserve that separation.

For the CLI, recover the Python selector's ergonomics without recovering Python implementation architecture. The existing `nix` term support is enough to build a small Rust-native selector; keep it small, synchronous, and reusable by the provider-connect path.

Do not “fix” the dashboard by teaching browsers to carry the server API key. Do not “fix” LAN access by overloading the advertised integration URL. Correct the defaults and preserve the existing security boundaries.
