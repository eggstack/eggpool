# Q008 Closure — ARM64 SBC Functional and Resource Characterization

Status: blocked; closure attempted 2026-09-09

Plan: [Q008 — ARM64 SBC functional and resource characterization](../../implementation/qualification/008-arm64-sbc-functional-and-resource-characterization.md)

Implementation: `scripts/qualification_sbc.py`,
`tests/migration_rs/fixtures/config/q008-sbc.toml`, and
`tests/migration_rs/test_q008_sbc.py`

Machine-readable blocked evidence: [`008-run.json`](008-run.json)

Evidence SHA-256: `1d2fa670e138c7179c39f94af2f70ba042c71aa0d24cfceac7cae42b5a104458`

Local release candidate prepared for future hardware execution: SHA-256
`d14da6d963efd9d8ddaa6bc3b58d158bd7503c4a52e5aa8293ffaace805eb2c2`;
macOS arm64/Rosetta build elapsed 206 seconds. It was not presented as SBC
evidence.

## Outcome

The guarded Q008 harness is implemented and passed a full synthetic local
loopback run, but Q008 cannot be accepted. The workstation is macOS/x86_64
under a translated shell, and the harness correctly stopped before candidate
execution because Q008 requires Linux/aarch64 plus a device-tree board model.
No physical SBC claim is made.

The hard dependency is also unresolved: Q007 remains formally blocked on its
live-provider evidence. Q008 therefore remains implemented but blocked rather
than promoted or accepted.

## Harness and safety contract

[`scripts/qualification_sbc.py`](../../../scripts/qualification_sbc.py) now:

- requires Linux/aarch64 and a device-tree board model before running;
- accepts an existing candidate and records its SHA-256, with an optional
  expected-hash check;
- creates all config, database, runtime, state, backup, and recovery paths
  below a private temporary root;
- uses a deterministic loopback provider and a fixed finite/streaming matrix;
- records only bounded command, procfs, SQLite, runtime, and archive scalars;
- omits credentials, host identity, network identity, raw request bodies, and
  full child-process output; and
- always attempts to leave the candidate stopped.

The fixture enables the real background task registration/tick path, dashboard
and static resources, WAL, low-wear metrics, and bounded automatic backups. It
does not add a production dependency or privileged host mutation.

## Functional and resource validation

The synthetic local validation used the existing Rust debug candidate with a
test-only board metadata override; it is harness validation, not Q008 hardware
evidence:

| Observation | Result |
|---|---|
| CLI version/help/check-config/migrate | pass |
| Startup, health/readiness, model listing | pass |
| Finite Chat/Responses/Messages | pass |
| Streaming Chat/Responses/Messages with native terminal markers | pass |
| Account, model-info, operator, transcoding, and runtime stats | pass |
| Dashboard page and CSS/JS/chart/favicon/theme static fetches | pass |
| Rehash and bounded vacuum/checkpoint maintenance | pass |
| Backup archive and isolated recovery | pass |
| Graceful shutdown, restart, and startup reconciliation | pass |
| Repeated workload/resource sampling | pass; 5 samples, no logical leak |
| Durable convergence | pass; 8 requests, 8 attempts, 8 reservations, 8 completed, 0 pending, 0 active |

No ARM64 hardware resource values were collected. The real run will populate
board/SoC/RAM/storage/OS/kernel/toolchain metadata, binary/build timing,
startup, idle/workload CPU/RSS/fd/thread/DB/WAL samples, task/generation
counts, backup/shutdown timing, and any optional Python comparison.

## Python comparison and findings

The Python reference comparison was not run because no common physical SBC was
available. This is permitted as a contextual comparison, not as a waiver of
Rust's mandatory physical qualification.

No implementation correctness, security, data-loss, or resource-stability
finding remains from the deterministic harness validation. The open blockers
are environmental/dependency blockers:

1. Q007 must first be accepted, or its separately reviewed provider-transport
   decision must explicitly unblock Q008.
2. A representative physical Linux aarch64 SBC must run the harness and
   produce accepted evidence.

## Exact verification

```text
uv run ruff format scripts/qualification_sbc.py tests/migration_rs/test_q008_sbc.py
uv run ruff check scripts/qualification_sbc.py tests/migration_rs/test_q008_sbc.py
uv run pyright scripts/qualification_sbc.py
uv run pytest tests/migration_rs/test_q008_sbc.py -q --tb=short --maxfail=1
uv run python -c '<test-only board metadata override>; run_qualification(debug candidate)'
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --output migration-rs/closure/qualification/008-run.json
git diff --check
```

Results: formatting, Ruff, Pyright, and all five Q008 contract tests passed;
the synthetic run passed 24 functional observations and five resource
snapshots; the real workstation invocation returned `blocked` before mutation;
the release build passed; and the diff check passed. The release candidate was
hashed above but was not executed or claimed as physical SBC evidence.

During the required Q005 regression check, the target detector was corrected
to recognize Apple Silicon when the shell reports x86_64 under Rosetta; the
explicit `Darwin/x86_64` mapping remains `other-unix`, and the Q005 test now
accepts its documented `supported-development` classification.

## Registry transition

Q008 is formally recorded as blocked, not accepted. Q009 remains queued behind
Q008, Q010 remains queued behind Q009, and M11 remains blocked on accepted Q010
plus its separate planning review. No future plan is unblocked by this
attempt. A later Q008 run may update the next append-only evidence record after
Q007 acceptance and physical SBC access are both available.
