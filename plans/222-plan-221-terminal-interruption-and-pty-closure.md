# Plan 222 — Plan 221 Terminal Interruption and PTY Closure Pass

Date: 2026-09-19  
Status: implementation handoff  
Planning baseline: `e93c4085a9a6121b03ff93e4947e618054b552ad`  
Closes: `plans/221-python-cli-lan-dashboard-parity-corrective-pass.md`  
Priority: P2 corrective polish / terminal lifecycle qualification  
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Close the small amount of residual work left after Plan 221.

Plan 221 successfully restored the Python-quality provider selector, canonical
`0.0.0.0` bind default, public read-only dashboard default, and the intended
authentication boundary. Its implementation record correctly left two items
for a final pass:

1. `Ctrl-C` is recognized inside the raw terminal selector but is currently
   converted into a generic configuration-read failure on the way to the
   top-level CLI.
2. Raw-terminal restoration is covered structurally/unit-wise but not by a
   PTY-level regression that proves the terminal is restored after confirm,
   cancel, interruption, and failure.

This plan fixes only those residuals and records final acceptance evidence. It
must not reopen the host/dashboard policy, selector design, provider connection
flow, or server authentication work completed by Plan 221.

## Current-state evidence

### Plan 221 implementation

Current `main` baseline:

```text
e93c4085a9a6121b03ff93e4947e618054b552ad
Implement Plan 221 Python CLI and LAN dashboard parity correction
```

Plan 221 is marked complete and its push CI `check` job passed.

The implementation added:

- `rust/src/operations/terminal.rs`
- interactive provider selection in
  `rust/src/operations/config_mutation.rs::connect_with_transition`
- shared selector use for multi-account logout
- canonical `server.host = "0.0.0.0"`
- canonical `dashboard.public = true`
- route-policy and router-level dashboard/auth tests
- docs/config/runtime-manifest updates

Those behaviors are not under reconsideration in this pass.

### Exact Ctrl-C defect

`rust/src/operations/terminal.rs` correctly recognizes raw byte `0x03`:

```rust
0x03 => KeyAction::Interrupted,
```

and returns:

```rust
Err(SelectError::Interrupted)
```

after the raw-mode guard has been established.

The problem is the next mapping layer in
`rust/src/operations/config_mutation.rs`:

```rust
terminal::SelectError::Interrupted => {
    MutationError::Read(io::Error::new(io::ErrorKind::Interrupted, "interrupted"))
}
```

`MutationError::Read` has the user-facing display:

```text
configuration file could not be read
```

and `rust/src/runtime.rs::mutation_error` currently converts every mutation
error into an ordinary command error using the caller's supplied validation
exit category.

The result is semantically wrong: pressing `Ctrl-C` in the provider/account
selector is a user interruption, not a configuration I/O failure.

### PTY capability already exists in the dependency graph

`rust/Cargo.toml` already includes:

```toml
nix = { version = "0.31.3", default-features = false, features = ["signal", "term", "user"] }
```

For nix 0.31.3, `nix::pty` and safe `nix::pty::openpty` are available under
the existing `term` feature. No `process` feature is needed for `openpty`.
Do not use `forkpty`; it is unnecessary and would conflict with the
repository's `#![forbid(unsafe_code)]` posture.

Reference used during planning:

- <https://docs.rs/nix/0.31.3/nix/pty/>

Therefore this corrective pass should require **no new direct dependency and no
new nix feature**.

## Required end state

After this pass:

1. pressing `Ctrl-C` in an interactive selector:
   - restores the original terminal settings;
   - exits the command as an interruption;
   - uses process exit status 130;
   - never prints `configuration file could not be read`;
   - never mutates the config or removes an account;
   - does not continue into API-key input, "Add another provider?", or other
     follow-on prompts.
2. Enter, q, standalone Esc, arrow keys, and j/k preserve Plan 221 behavior.
3. A safe PTY-level test proves termios restoration at least for:
   - successful selection;
   - q/Esc cancellation;
   - Ctrl-C interruption.
4. No new terminal/TUI/test dependency is added.
5. Plan 221's host/dashboard/auth behavior remains unchanged.
6. A final real-terminal smoke check is recorded when the implementation
   environment permits one.

## Workstream 1 — Preserve interruption as a first-class semantic

### Files

Primary:

- `rust/src/operations/terminal.rs`
- `rust/src/operations/config_mutation.rs`
- `rust/src/runtime.rs`
- `rust/src/error.rs`
- focused tests in the closest existing modules/targets

### Mutation error contract

Add an explicit interruption variant rather than disguising it as I/O. For
example:

```rust
#[error("interrupted")]
Interrupted,
```

in `MutationError`.

The exact spelling may differ, but the invariant is:

```text
SelectError::Interrupted
    -> MutationError::Interrupted
    -> top-level interruption semantics
```

It must never pass through `MutationError::Read`.

Keep genuine `nix`/stdio failures mapped to the existing I/O categories.

### Top-level exit behavior

Map the explicit mutation interruption to a stable top-level CLI result with
exit status 130, the conventional `128 + SIGINT` shell status.

Preferred approaches, in order:

1. add a narrow `BootstrapError::Interrupted` variant in
   `rust/src/error.rs` whose `exit_code()` is 130; or
2. map to the existing `BootstrapError::Command` only if doing so can preserve
   an unmistakable interruption message and status 130 without confusing it
   with validation failures.

Prefer the dedicated variant because interruption is not a validation failure.

A concise user-facing `Interrupted.` message is acceptable. Do not emit a
backtrace, config-read error, or multi-line diagnostic.

Do not globally reinterpret every `io::ErrorKind::Interrupted`; only the
explicit terminal-selection semantic should become the user-interruption path.
Ordinary I/O operations may legitimately retry or report errors according to
their existing owners.

### Runtime mapping

Update `rust/src/runtime.rs::mutation_error` (or the narrow selector callers)
so `MutationError::Interrupted` bypasses the normal command-specific
validation/control exit code.

All existing non-interruption mutation errors must preserve their current exit
codes and messages.

## Workstream 2 — Make raw-mode lifecycle testable without changing public API

### Goal

Test the actual termios guard and interactive loop rather than only the pure
key classifier.

Do not make `select_one` public outside its current crate/module boundary
merely for integration tests.

### Recommended internal seam

Refactor only enough of `rust/src/operations/terminal.rs` to allow the
interactive loop to operate on injected terminal input/output handles in tests.

A reasonable shape is conceptually:

```rust
run_interactive_on(
    terminal_input,
    terminal_output,
    title,
    options,
)
```

while public/internal production `select_one()` continues to:

- check `stdin.is_terminal() && stdout.is_terminal()`;
- pass real stdin/stdout to the same core;
- expose exactly the same `Result<Option<usize>, SelectError>` contract.

Do not create a generalized terminal abstraction, trait hierarchy, event loop,
or mock framework.

### Guard ownership

Keep one termios scope guard as the restoration authority.

The guard must restore the termios snapshot on all exits:

- Enter/confirm;
- q cancellation;
- standalone Esc cancellation;
- Ctrl-C interruption;
- EOF;
- read/write error;
- escape-parser error if one is later introduced.

Do not manually duplicate restore calls across branches.

If writing the final newline can fail after a selection has already been made,
the guard must still restore before the error escapes.

## Workstream 3 — Add a safe PTY regression

### Dependency constraint

Use the existing:

```rust
nix::pty::openpty
```

under the already-enabled `term` feature.

Do not add:

- `portable-pty`;
- `expectrl`;
- `rexpect`;
- `crossterm`;
- `dialoguer`;
- `inquire`;
- a new dev-dependency solely for PTY testing.

Do not use `forkpty` or unsafe code.

### Test design

Keep the test local to `rust/src/operations/terminal.rs` unless a separate
test target is demonstrably cleaner.

For each PTY case:

1. call `openpty(None, None)`;
2. capture the slave's initial termios with `tcgetattr`;
3. run the internal selector against the slave side;
4. drive key bytes through the master side from a bounded helper thread;
5. collect/ignore rendered output from the master as necessary so writes
   cannot block;
6. wait for selector completion with a bounded synchronization mechanism;
7. read the slave's termios again;
8. assert it equals the original settings for all fields relevant to the raw
   transition;
9. assert the semantic result.

Required cases:

#### Confirm

Input:

```text
j
Enter
```

Expected:

- selected index 1 when at least two options exist;
- termios restored.

#### Cancel

Input:

```text
q
```

and/or standalone Esc.

Expected:

- `Ok(None)`;
- termios restored.

At least one test should use standalone Esc so the VTIME-bounded path is
exercised.

#### Interrupt

Input byte:

```text
0x03
```

Expected:

- `Err(SelectError::Interrupted)`;
- termios restored.

Do not rely on wall-clock sleeps longer than needed. Use bounded channel/thread
coordination and the selector's existing short Esc timeout.

### Platform gating

PTY tests are Unix-only.

Use the same target restrictions as the current terminal implementation. Skip
or cfg-out the PTY test on platforms where `nix::pty::openpty` is not
available rather than broadening production dependencies.

CI on the supported Linux runner must execute the test.

## Workstream 4 — Verify command-level Ctrl-C behavior

Pure terminal tests are necessary but not sufficient: prove the error mapping
does not regress back to a config-read diagnostic.

Add focused deterministic coverage around the mapping layer.

Required assertions:

- `MutationError::Interrupted.to_string()` is not
  `configuration file could not be read`;
- mapping the interruption through the runtime/top-level error contract returns
  exit code 130;
- rendered top-level text contains an interruption indication only;
- the interruption path does not use `EXIT_VALIDATION`.

Avoid exposing private runtime helpers solely for an integration test if an
`error.rs` unit test plus a narrow mutation-mapping test is enough.

If a subprocess-style CLI test is straightforward without a new dependency,
it is acceptable, but it is not required if the typed mapping is completely
covered.

## Workstream 5 — Final Plan 221 acceptance check

### Automated behavior that must remain green

Re-run the Plan 221 focused contracts:

- default host remains `0.0.0.0`;
- root canonical config remains `0.0.0.0`;
- SBC profile remains deliberately `127.0.0.1`;
- dashboard default remains public;
- unauthenticated `GET /` returns dashboard HTML;
- ordinary public dashboard stats remain readable;
- `/v1/*`, `/api/integrations/*`, `/api/stats/runtime`,
  `/api/stats/update`, and `/api/status` remain authenticated;
- `dashboard public --off` restores dashboard authentication;
- non-TTY provider selection retains deterministic ID/number fallback.

No host/dashboard/auth code change should be necessary in this plan.

### Real-terminal smoke

When a real TTY is available, run one manual smoke from a temporary
config/home:

1. invoke `eggpool connect` or `eggpool onboard`;
2. verify j/k and Up/Down movement;
3. press q; verify normal shell echo/canonical input immediately afterward;
4. repeat and press standalone Esc; verify prompt return and terminal state;
5. repeat and press Ctrl-C; verify:
   - terminal is restored;
   - command terminates immediately;
   - no config-read error is printed;
   - shell status is 130;
6. complete one selection with Enter and verify terminal restoration.

Useful shell check after Ctrl-C:

```bash
printf 'exit=%s\n' "$?"
stty -a >/dev/null
printf 'terminal-ok\n'
```

Do not capture or commit real API keys.

### LAN/dashboard smoke

If a second LAN client is available, perform one final Plan 221 product smoke:

1. start a temporary/test EggPool instance using a generated server key;
2. confirm it listens on the configured `0.0.0.0:11300`;
3. from the LAN client, request `http://<host>:11300/` without a key and
   receive HTML;
4. request `/v1/models` without a key and receive 401;
5. request `/v1/models` with the test key and receive the normal authenticated
   response;
6. run `eggpool dashboard public --off` and confirm `/` now requires auth.

This LAN smoke is environment qualification, not a reason to add network-test
machinery to CI. If no second LAN client is available, record that the
router-level automated test remains the source-controlled proof and leave the
manual LAN item explicitly unperformed.

## Focused verification

Run from repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings

cargo test --manifest-path rust/Cargo.toml --lib operations::terminal -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
```

If the interruption mapping receives unit coverage in `error.rs` or
`runtime.rs`, include the exact corresponding `cargo test --lib <filter>`
command in the implementation record.

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

No dependency audit is required if `Cargo.toml` and `Cargo.lock` remain
unchanged. If either changes unexpectedly, stop and explain why before
continuing.

## Known unrelated test issue

Plan 221 recorded the existing timing-sensitive test:

```text
provider_transport::extended_encrypted_proxy_cancellation_recovers_through_same_client
```

with a 1 ms abort race.

Do not change that test, Eggfetch/Eggress behavior, cancellation semantics, or
provider transport in this closure pass. If it flakes, record the result
separately; do not mask a genuine new failure behind the known flake.

## Documentation / plan lifecycle

No operator documentation should need substantive changes because the desired
user-visible behavior is already documented by Plan 221.

At completion:

1. append an implementation record to this Plan 222 with:
   - commit SHA;
   - interruption error mapping chosen;
   - PTY cases executed;
   - focused/full test commands and outcomes;
   - real-TTY smoke result;
   - LAN smoke result or explicit environment-unavailable note;
2. set this plan to `Status: complete`;
3. do not rewrite Plan 221 again except for a genuinely incorrect historical
   statement.

## Out of scope

Do not use this pass to:

- change provider selection layout/colors/content;
- add new selector keys;
- change wrapping/clamping behavior;
- change API-key entry UX;
- change `server.host` or `dashboard.public` defaults;
- change authentication route classification;
- add a TUI framework;
- add a general PTY abstraction;
- add Windows interactive-terminal support;
- change provider transport or the known cancellation-flake test;
- refactor unrelated runtime/config code.

## Acceptance criteria

This closure pass is complete when:

1. `Ctrl-C` inside provider/account selection is represented as an explicit
   interruption, not `MutationError::Read`.
2. The top-level command exits with status 130 for that interruption.
3. User-facing output no longer says `configuration file could not be read`
   for selector Ctrl-C.
4. The selector raw-mode guard restores terminal settings after Enter.
5. The selector raw-mode guard restores terminal settings after q or Esc.
6. The selector raw-mode guard restores terminal settings after Ctrl-C.
7. At least one safe `openpty` test exercises the real termios transition
   using the existing nix `term` feature.
8. No new dependency, nix feature, unsafe code, or terminal framework is added.
9. Plan 221's host/dashboard/auth regression tests remain green.
10. The full repository validation baseline remains green apart from any
    separately documented occurrence of the pre-existing provider-transport
    timing flake.
11. Real-TTY acceptance is recorded when available, with explicit evidence for
    Ctrl-C exit 130 and post-command terminal usability.
12. The plan is marked complete with evidence and Plan 221 requires no further
    corrective implementation work.

## Handoff note

Keep this pass small.

The selector itself is already correctly designed. The defect is the loss of
semantic information at the `SelectError -> MutationError -> BootstrapError`
boundary plus the absence of a PTY-level proof for the raw-mode guard.

Fix those two things directly. Do not turn terminal handling into a subsystem
rewrite and do not reopen the LAN/dashboard policy that Plan 221 already
qualified.
