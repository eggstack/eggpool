# Q005 Supported-Target Build and Non-Root Runtime Portability

Status: accepted; closed 2026-09-09

Plan: [`005-supported-target-build-and-runtime-portability.md`](../../implementation/qualification/005-supported-target-build-and-runtime-portability.md)

Implementation candidate: `76d329f3aa908b852b4206c97d7b686a54d41953`

## Outcome

Q005 is accepted. The declared Q001 supported target classes have reproducible
release-build, target-appropriate test-compilation, dependency-inspection, and
ordinary non-root runtime evidence. Linux x86_64 and Linux aarch64 are
qualified as supported; macOS arm64 is qualified as supported-development.
Windows remains explicitly unsupported and other Unix remains not-qualified by
the Q001 matrix. This closure makes no M11 cutover claim and does not replace
the required physical SBC evidence owned by Q008.

The final hosted qualification was workflow run
[`34341001472`](https://github.com/eggstack/eggpool/actions/runs/34341001472),
with all three target jobs passing. The workflow is manual (`workflow_dispatch`)
and does not enlarge normal CI.

| Target | Environment | Classification | Execution | Binary / dependencies | Runtime |
|---|---|---|---|---|---|
| `linux-x86_64` | disposable GitHub Linux x86_64 | supported | x86_64 | 33,721,224 bytes; `ldd`, 5 dependencies, 0 unresolved | pass |
| `linux-aarch64` | disposable hosted Linux ARM64 | supported | aarch64 | 30,845,272 bytes; `ldd`, 5 dependencies, 0 unresolved | pass |
| `macos-arm64` | hosted macOS 14 arm64 | supported-development | arm64 | 27,647,904 bytes; `otool`, 3 dependencies, 0 unresolved | pass |

Each runtime record passed health, readiness, models, finite chat, streaming
chat, runtime-status, rehash, backup/recover, graceful stop, and
restart/reconcile. Each used a private temporary root and a loopback-only
synthetic provider; the reports contain no credentials or raw response bodies.

Machine-readable evidence uses schema `m10-q005.v1`:

- [`005-run-linux-x86_64.json`](005-run-linux-x86_64.json) — SHA-256 `40de7d8905bdfef43b18ea0d8d7bd4e85670744402f0d05896221c1faa0eb3ec`
- [`005-run-linux-aarch64.json`](005-run-linux-aarch64.json) — SHA-256 `b4e5e447d60ec8d8906d030267a5af9bf91c1a916da2485f8a16b7d48bd44c00`
- [`005-run-macos-arm64.json`](005-run-macos-arm64.json) — SHA-256 `93737cca1f9656989bcce9aa5218bea23eb89d4e1095c36d4e8a189affd7537d`

## Verification record

The required focused qualification passed:

```text
uv run pytest tests/migration_rs/test_q005_portability.py -q --tb=short --maxfail=1
5 passed
```

The required post-portability Q002 aggregate was rerun on the primary
development target:

```text
uv run python scripts/qualification_runner.py --skip-build
{"block": 0, "fail": 0, "infrastructure-error": 0, "pass": 18, "skip": 0}
```

The Q002 machine artifacts were refreshed to candidate
`76d329f3aa908b852b4206c97d7b686a54d41953` and remain part of the accepted
qualification record.

The hosted workflow ran these gates for every declared target:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets --target <target> -- -D warnings
cargo build --manifest-path rust/Cargo.toml --release --target <target>
cargo test --manifest-path rust/Cargo.toml --all-targets --no-run --target <target>
uv run python scripts/qualification_portability.py --binary <release-binary> --config-fixture tests/migration_rs/fixtures/config/q005-portability.toml --target-id <target-id> --output migration-rs/closure/qualification/005-run-<target-id>.json
```

Local macOS arm64 development verification also passed the focused Q005 tests,
native arm64 release build, native arm64 all-target test compilation, native
clippy, and the acceptance runner. Hosted native execution is the canonical
macOS evidence for this closure.

## Findings and corrective work

Q005 found and corrected the following portability/runtime issues before
acceptance:

1. Rust now exposes the `/v1/models` catalog projection required by the
   runtime acceptance path.
2. Startup catalog refresh persists model/support rows before the first
   inference, avoiding a first-request foreign-key failure in a clean fixture.
3. Stop/restart waits for both process exit and the private PID file to clear,
   covering macOS child/zombie observation and reconciliation behavior.
4. The runner isolates HOME, XDG, runtime, data, and backup roots, bounds every
   operation, uses a synthetic loopback provider, and records only scalar
   evidence.
5. Rust clippy warnings in the touched server path were removed so all hosted
   target gates run with `-D warnings`.

No unresolved Q005 high/medium finding remains. Q005 does not claim rootful
systemd/process-user/filesystem behavior, physical ARM64 SBC characterization,
live-provider interoperability, sustained resource stability, or M11 release
readiness; those remain owned by Q006-Q010.

## Registry transition

Q005 is closed and accepted in the implementation plan, qualification README,
roadmap, and registry. Q006 — disposable rootful Linux operational acceptance
— is promoted to **ready for handoff** as the sole dependency-ready M10 plan.
Q007-Q010 remain queued behind their direct predecessors. M11 remains blocked on
accepted Q010 plus its separate planning review.
