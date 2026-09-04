# Foundation Corrective Milestone F006 — Side-by-Side Safety and Serve-Contract Closure

Status: closed; see [closure record](../../closure/foundation/006-status.md)

Repository baseline: `b0a987bcada7cb793b7eaec645243c12159fa60c`

Source roadmap: `migration-rs/subsystems/foundation-roadmap.md#F006`

Applicable ADRs: ADR-0001, ADR-0002.

Primary class: invariant

## 1. Objective

Close the remaining foundation defects discovered after F005 by making the side-by-side Rust candidate safe to start next to the Python implementation and by eliminating silent command/config behavior that could mislead migration testing.

The critical invariant is that a Rust startup rejected because the configured address is already occupied must not mutate EggPool durable state before the rejection. Secondary closure work makes the migration-stage `serve` contract explicit, makes the deferred `server.threads` meaning visible, and removes a format-sensitive build-time checksum parser.

This is a corrective pass over F005/F003/F004 integration. It is not a new feature milestone and must remain small enough to close before M4 provider HTTP/Eggress implementation begins.

## 2. Why a corrective plan is required

F005 correctly tested that a second Rust process could not bind an occupied listener, but the test only asserted process failure and the error message. `rust/src/server.rs::run` currently opens the configured SQLite database, applies migrations, and synchronizes configured accounts before `TcpListener::bind`. Therefore a port collision can still create or alter durable state before the server reports that it cannot start.

F003 correctly established the full CLI parser while commands were staged. F005 then made `serve` executable, but `runtime.rs` currently matches `Command::Serve(_)` and discards `ServeArgs`. Python assigns material semantics to `--verbose`, `--log-file`, `--quiet`, and `--as-root`; silently accepting those flags in an implemented Rust command is no longer an acceptable migration-stage contract.

The Rust binary also deliberately uses Tokio's current-thread runtime while the config model accepts `server.threads`. Keeping single-thread execution is desirable for the compatibility-first data-plane work, but the candidate must not imply that the field currently configures Tokio.

Finally, `rust/build.rs` parses `checksums.json` by splitting lines on quote characters. The canonical file currently matches that layout, but semantic JSON reformatting can unnecessarily break the Rust build. This is a small foundation robustness issue and can be removed without runtime architecture changes.

## 3. Dependencies

Hard: F001 through F005 closed.

No future provider, routing, transcoding, coordinator, runtime, or operations implementation is a dependency for F006.

F006 is a hard precondition for registering the M4 Provider HTTP + Eggress implementation handoff as dependency-ready. M4 roadmap drafting may proceed in parallel, but provider transport implementation should not rely on an unsafe dual-run startup boundary.

## 4. Python oracle evidence

Primary sources:

- `src/eggpool/cli_full.py` — authoritative `serve` option meaning, foreground/daemon distinction, duplicate-instance refusal, and root policy;
- `src/eggpool/runtime.py` and runtime-path helpers — current Python process/lifecycle ownership;
- `src/eggpool/db/connection.py`, migrations, and repositories — durable-state ownership and startup behavior;
- `tests/migration_rs/test_f005_server.py` — current Rust bind/lifecycle acceptance coverage that missed the durable-state side effect;
- F003/F004/F005 closure records and migration harness;
- `rust/src/server.rs`, `runtime.rs`, `cli.rs`, `main.rs`, and `build.rs` at the repository baseline.

Agents must inspect current Python and Rust code before editing because the plan records the defect boundary, not an instruction to mechanically copy Python process architecture.

## 5. Exact vs semantic parity

Exact requirements:

- an address/bind collision must produce a non-zero Rust startup result without creating, migrating, synchronizing, or otherwise changing the configured SQLite database;
- existing CLI option names, placement, parsing, help, and exit-class behavior remain stable;
- unsupported migration-stage `serve` behavior must fail explicitly rather than parse and silently do nothing;
- no credential, API key, proxy URL credential, prompt, or request body may appear in new diagnostics/tests;
- canonical migration checksum semantics remain unchanged.

Permitted migration-stage supported differences:

- Rust does not need to implement Python's daemon-detach supervisor, PID-file ownership, log redirection, systemd integration, or production lifecycle in F006; those belong to M9;
- Rust may require explicit foreground development invocation (`serve --verbose`) until daemon lifecycle is ported;
- Rust may continue using a current-thread Tokio runtime through the correctness-heavy migration milestones. `server.threads` may remain a compatibility field so long as the candidate explicitly reports that it is not yet active rather than silently claiming otherwise.

No normalization rule may hide a durable-state change, command exit-code difference, or silently ignored option.

## 6. Required changes

### A. Reserve the listener before durable startup mutation

Reorder Rust server startup so address ownership is proven before opening a writable database, applying migrations, or calling account synchronization.

Preferred order:

1. validate non-mutating config/server-key requirements;
2. resolve address;
3. bind/reserve the TCP listener;
4. open/configure SQLite;
5. validate/apply canonical migrations;
6. synchronize configured accounts and complete other startup state preparation;
7. publish the Axum router on the already-owned listener.

If any post-bind startup step fails, drop the listener, close the DB worker when opened, return a typed non-zero error, and leave the port reusable. Do not add a second probe/listener race or a custom cross-process lock when binding itself is the authoritative ownership operation.

The implementation must account for errors during DB open, migration, account sync, and router/startup preparation so resources acquired earlier are released deterministically.

### B. Prove bind rejection has zero durable side effects

Extend migration black-box coverage so an occupied target address is paired with a DB fixture whose pre-run state is observable. The failing Rust launch must preserve that state exactly.

At minimum cover:

- nonexistent DB path remains nonexistent after bind rejection;
- an existing DB fixture retains migration-ledger/account facts after bind rejection;
- the occupying process/listener remains unaffected;
- the failed Rust process exits boundedly and does not leave a worker/listener behind.

The test must inspect durable state, not merely rely on logs or process exit.

### C. Make the migration-stage `serve` contract explicit

Do not silently consume `ServeArgs` once `serve` is executable.

For F006, prefer the narrow development contract:

- `serve --verbose` is the supported Rust foreground execution path;
- plain `serve` represents Python daemon mode and should return a clear typed migration-stage unsupported error until M9 rather than unexpectedly running foreground;
- `--log-file` and `--quiet` are daemon-output options and must not be silently accepted as effective in foreground Rust; reject unsupported combinations before DB/listener side effects;
- `--as-root` must not be silently treated as implemented. Either implement the small foreground root gate without introducing disproportionate dependencies, or explicitly reject/defer the option with a stable migration-stage error. Do not add a process/lifecycle crate solely for this flag unless the current repository already justifies it.

Update the Rust migration README and harness invocations to use the supported explicit foreground form.

This plan does not require daemonization, PID files, detach/re-exec behavior, stop/restart, log-file routing, or systemd behavior.

### D. Make `server.threads` staging visible

Retain the current-thread Tokio runtime unless evidence from later data-plane work requires otherwise.

When the Rust server starts with `server.threads != 1`, emit a bounded operator-facing migration diagnostic stating that the field is accepted for config compatibility but the current Rust candidate remains single-threaded until the runtime milestone. Do not reject otherwise-valid Python configs solely because they specify another thread value.

Add a regression test or structured startup observation proving the field is not silently presented as active.

Do not implement multi-thread runtime generation semantics in F006.

### E. Make checksum-manifest parsing structural rather than layout-sensitive

Replace the line/quote splitting in `rust/build.rs` with semantic JSON parsing of `checksums.json`.

Requirements:

- preserve the existing exact checksum validation and SQL inventory checks;
- reject non-string/invalid SHA-256 entries and duplicate/impossible inventory states;
- accept semantically identical JSON with changed whitespace/line formatting;
- use build-time-only dependency wiring if needed; do not add a new runtime subsystem;
- preserve `cargo:rerun-if-changed` behavior for the checksum file and every canonical SQL migration.

A focused build/unit regression should prove a reformatted manifest is accepted while malformed checksum content is rejected.

## 7. Side-by-side writable-state policy

The differential migration environment must not casually run Python and Rust concurrently against the same writable SQLite file. That would couple observations and can create test interference even though SQLite serializes writes correctly.

Update migration documentation/harness helpers so the normal dual-run workflow uses separate temporary DBs or a copied snapshot of the same source fixture. Python remains the behavioral oracle; parity comes from equivalent inputs and post-run observations, not from two implementations racing on one writable database.

Do not add a production DB-path prohibition to Rust: final cutover must operate on the existing EggPool DB. This is a migration-harness/operator-safety rule, not a new product constraint.

## 8. Failure, cancellation, restart, and contention semantics

- Bind collision: fail before durable mutation.
- DB open/migration/account-sync failure after successful bind: close DB if opened, release listener, exit non-zero.
- Signal after admission: retain F005 graceful shutdown behavior.
- SQLite busy/locked behavior: preserve F004 typed/bounded semantics; do not add retry loops here.
- Unsupported `serve` mode/flag: reject before listener or DB mutation.
- Repeated failed starts must not require DB repair, PID cleanup, or process restart beyond rerunning the command.

## 9. Security requirements

- No secrets in startup diagnostics or new test fixture output.
- Do not weaken F005 API-key validation/auth behavior.
- Do not bypass account credential validation merely to simplify bind ordering; separate non-mutating validation from durable initialization where necessary.
- No unsafe code.
- No new network exposure or listener fallback behavior.

## 10. Scope exclusions

Explicitly out of scope:

- provider HTTP clients, TLS, Eggress, or proxy URI handling;
- routing, quota, health backoff, model-router implementation;
- inference bodies/codecs/SSE;
- request coordinator/retry/finalization;
- runtime generations/rehash/background schedulers;
- daemonization/PID-file/process supervisor parity;
- complete dashboard expansion;
- broad performance optimization;
- broad CI or browser infrastructure.

If fixing an item requires one of these systems, stop and record it for the owning later milestone instead of expanding F006.

## 11. Required regression tests

Add focused tests for:

1. occupied port + nonexistent DB path -> failure and DB still absent;
2. occupied port + existing Python-compatible DB -> migration/account ledger facts unchanged;
3. post-bind DB startup failure -> listener released and process exits;
4. `serve --verbose` continues to start the development server;
5. plain `serve` explicitly reports deferred daemon mode rather than starting foreground;
6. `--log-file`/`--quiet` unsupported-stage combinations fail before side effects;
7. `--as-root` behavior is explicit, never silently ignored;
8. non-default `server.threads` remains accepted with an explicit candidate-stage diagnostic;
9. reformatted checksum JSON is parsed correctly and malformed checksum data fails;
10. existing F003/F004/F005 differential tests remain green.

The bind/DB regression is mandatory closure evidence and may not be replaced by a unit-only test.

## 12. Verification commands

Narrow gate:

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
```

Targeted Python regression gate:

```text
uv run pytest tests/unit/test_config.py tests/unit/test_db.py tests/smoke/ -q --tb=short --maxfail=1
```

If the implementation touches additional Python migration-harness helpers, also run Ruff format/check and Pyright on the changed Python test/scripts surface. A full Python suite or broad CI matrix is not required unless a failure indicates wider coupling.

## 13. Documentation updates

Update at minimum:

- `rust/README.md` to show `serve --verbose` as the supported migration-stage server invocation and explain deferred daemon options;
- migration dual-run guidance to state that Python and Rust should use distinct writable DB paths/copies;
- any closure matrix that previously implied a bind failure occurred before startup mutation.

Do not rewrite historical F005 closure evidence. F006 should explain the newly discovered gap and the regression that now covers it.

## 14. Acceptance criteria

F006 closes only when all of the following are true:

- a bind collision provably causes no SQLite creation/migration/account synchronization;
- resources are released on every tested post-bind startup failure;
- no implemented Rust `serve` option is silently ignored;
- the supported Rust development invocation is explicit and deterministic;
- non-default `server.threads` is accepted without pretending to control Tokio;
- checksum JSON formatting cannot break an otherwise-valid canonical migration inventory;
- dual-run documentation/harness defaults avoid a shared writable DB;
- F003/F004/F005 regression gates remain green;
- no provider/routing/runtime-lifecycle scope was pulled forward.

## 15. Closure evidence

The closure record must include:

- implementation commit(s);
- before/after startup-order description;
- black-box DB-before-bind regression evidence;
- serve-mode/flag behavior matrix compared with Python and clearly marked supported differences;
- thread-field staging evidence;
- checksum parser/reformat regression evidence;
- dependency delta;
- Rust and targeted Python verification outputs;
- statement that M4 Provider HTTP + Eggress may now be registered dependency-ready, or explicit remaining blocker.

## 16. Handoff notes

Optimize for safety and explicitness, not for premature lifecycle parity. The highest-value fix is ordering listener ownership before any writable durable-state action. The next highest-value fix is removing silent semantics from an already-executable command. Keep the Rust server single-process/current-thread for now unless the corrective work itself proves that impossible.
