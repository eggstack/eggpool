# Q005 — Supported-Target Build and Non-Root Runtime Portability

Status: ready for handoff (2026-09-09)

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: planning baseline `00dd27fa103e3c663968ecd95d9289c60fca0601`; implement against current main after accepted Q004.

Primary class: invariant/polish

Hard dependency: accepted Q004.

## Objective

Prove that the Rust candidate builds and performs its ordinary non-root runtime contract on every target class Q001 marks supported for M11. Keep the evidence reproducible and small enough for local/disposable execution without converting normal CI into a large platform matrix.

## Target matrix

Q001 is authoritative. At minimum M10 expects:

- Linux x86_64 — mandatory;
- Linux aarch64 — mandatory, with physical SBC characterization deferred to Q008;
- macOS arm64 — mandatory if Q001 retains it as supported non-root development/runtime;
- any additional target Q001 marks supported — mandatory at the level Q001 assigns;
- unsupported/build-only targets — verify the documented explicit outcome rather than pretending full runtime parity.

## Build qualification

For each supported build target record:

- OS/distribution and architecture;
- Rust toolchain version;
- `cargo build --release` result;
- `cargo test --no-run` or target-appropriate compile result;
- binary size;
- dynamic library/runtime dependency inspection where practical;
- warnings/lints relevant to platform `cfg` behavior.

Do not cross-compile and call that runtime qualification. Cross compilation may supplement but not replace execution evidence.

## Non-root runtime scenario

Use an isolated temp HOME/XDG/config/data/state root and deterministic loopback provider. Exercise:

1. `eggpool version` and root help;
2. `check-config`;
3. foreground `serve --verbose` or equivalent non-detached process;
4. health and readiness;
5. `/v1/models`;
6. one finite Chat/Responses/Messages request as assigned by Q001 target matrix;
7. one streaming request;
8. `runtime-status`;
9. accepted live `rehash`;
10. backup and validation/recover into a separate temp root;
11. graceful stop/shutdown;
12. restart and startup reconciliation of clean state.

For Unix control-socket targets also exercise rehash/status through the real local control path.

## Platform-sensitive boundaries

Explicitly inspect/test:

- Unix-domain socket availability/path limits;
- PID/process identity operations;
- filesystem permission modes;
- path canonicalization and symlink refusal;
- signal handling;
- executable self-path/update preconditions without actually applying a public update;
- SQLite bundled behavior;
- TLS root store/provider transport initialization;
- Eggress supported proxy connector construction;
- archive/backup paths;
- dashboard/static serving.

If a supported target requires a semantic alternative to Unix-specific behavior, it must already be authorized by Q001/current product contract or receive an explicit planning correction; do not add ad hoc fallback behavior inside a qualification test.

## Reusable runner

Add a small target acceptance script that emits the Q001 environment/evidence schema. It should:

- accept candidate binary and config fixture paths;
- create only temporary/private roots;
- start/stop the process deterministically;
- use loopback HTTP providers only;
- collect bounded stdout/stderr and scalar resource metadata;
- leave no service/cron/root filesystem changes;
- return non-zero on mandatory failure.

## Manual workflow posture

If useful, add a `workflow_dispatch` workflow for hosted target classes such as Linux x86_64/macOS arm64. It must be manual or narrowly scoped unless Q005 proves the cost/runtime is appropriate for normal CI.

Do not put physical SBC qualification or live provider credentials into GitHub Actions.

## Required tests

- acceptance script self-test with a fake/unhealthy candidate;
- candidate crash is reported as failure, not timeout-only ambiguity;
- temp root cleanup and no real HOME/config mutation;
- unsupported platform result is explicit and matches Q001;
- control socket and process path collision fixtures;
- binary/runtime metadata evidence is bounded and secret-free.

## Verification

For each target, record the exact commands. Baseline local commands include:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo build --manifest-path rust/Cargo.toml --release
cargo test --manifest-path rust/Cargo.toml --all-targets --no-run
# Q005 target acceptance runner against target/release/eggpool
```

Also rerun Q002's deterministic aggregate on at least the primary development target after any portability fix.

## Non-goals

Q005 does not perform rootful systemd deployment (Q006), paid live-provider calls (Q007), physical SBC resource characterization (Q008), or publish release assets (M11).

## Closure evidence

Write `migration-rs/closure/qualification/005-status.md` containing:

- target matrix from Q001 with actual environment records;
- build/run results per target;
- binary sizes and notable platform dependencies;
- unsupported/build-only classifications;
- portability fixes and regression tests;
- workflow/CI decision;
- unresolved findings and registry transition.

## Acceptance criteria

Q005 closes only when every Q001 supported non-root target has required build/runtime evidence, unsupported targets fail explicitly rather than accidentally, no high/medium portability finding remains, and no M11 distribution claim has been made.

Accepted Q005 promotes only Q006.
