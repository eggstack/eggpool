# Q008 Closure — ARM64 SBC Functional and Resource Characterization

Status: blocked; physical run complete; closure attempted 2026-09-09

Plan: [Q008 — ARM64 SBC functional and resource characterization](../../implementation/qualification/008-arm64-sbc-functional-and-resource-characterization.md)

Implementation commits: `ed82ed2`, `64ff9b4d` —
`scripts/qualification_sbc.py`, `tests/migration_rs/fixtures/config/q008-sbc.toml`,
and `tests/migration_rs/test_q008_sbc.py`

Machine-readable blocked evidence: [`008-run.json`](008-run.json)

Evidence SHA-256: `1d2fa670e138c7179c39f94af2f70ba042c71aa0d24cfceac7cae42b5a104458`

Physical follow-up evidence: [`008-run-linux-aarch64-pi5.json`](008-run-linux-aarch64-pi5.json)

Physical evidence SHA-256: `fc44f80b5ec1085768449c2972d8ed0048b83bb7c78f178f62b13ee3a0110f68`

Local release candidate prepared for future hardware execution: SHA-256
`d14da6d963efd9d8ddaa6bc3b58d158bd7503c4a52e5aa8293ffaace805eb2c2`;
macOS arm64/Rosetta build elapsed 206 seconds. It was not presented as SBC
evidence.

## Outcome

The follow-up run completed on a Raspberry Pi 5 Model B under Ubuntu 24.04.4
LTS/Linux 6.8.0 on native aarch64. This satisfies Q008's mandatory physical
SBC evidence requirement. The candidate was built on-device in the release
profile in 210 seconds; the resulting 30,824,272-byte binary has SHA-256
`4df57358a04f71e3549d31ddf35b32fc31089d1354ae8005cc2c4eb664f1f2b4`.

All functional cells passed, including the three finite and three streaming
wire surfaces, dashboard/static fetches, rehash/runtime status, backup and
isolated recovery, maintenance, restart/reconciliation, and graceful stop.
The bounded workload converged to 8 completed requests, 8 terminal attempts,
8 completed reservations, zero pending requests, and zero active reservations.
The five resource snapshots held at 14 file descriptors and 2 threads, with
RSS from 15.8 MiB to 18.6 MiB and zero logical leaks. Client-observed finite
elapsed/first-byte timings were 3–4 ms; streaming timings were 3–4 ms. These
are characterization values, not an SLA.

Q008 is still not accepted because its declared hard dependency Q007 remains
formally blocked on live-provider evidence. The physical requirement is no
longer an open Q008 finding.

| Environment fact | Observed value |
|---|---|
| Board / architecture | Raspberry Pi 5 Model B Rev 1.0 / aarch64 |
| CPU | 4 cores; 2.4 GHz policy; `ondemand` governor |
| RAM | 7.75 GiB |
| Storage / filesystem | non-rotational MMC / ext4 |
| OS / kernel | Ubuntu 24.04.4 LTS / 6.8.0-1064-raspi |
| Thermal observation | 46.9°C at metadata collection |
| Rust toolchain / profile | rustc 1.98.1 / release |

No serial number, hostname, address, or full environment dump is retained.

### Initial closure attempt

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

The physical run populated board/SoC/RAM/storage/OS/kernel/toolchain metadata,
binary/build timing, startup, idle/workload CPU/RSS/fd/thread/DB/WAL samples,
task counts, backup timing, and shutdown evidence. Generation and wire-flight
fields were unavailable from the exposed runtime-status projection and are
recorded as null in the machine-readable samples.

| Point | RSS | CPU | FDs | Threads | DB | WAL | Runtime tasks | Requests | Active reservations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| before warmup | 15.1 MiB | 0.00% | 14 | 2 | 548 KiB | 80 KiB | 5 | 0 | 0 |
| after warmup | 17.1 MiB | 0.00% | 14 | 2 | 548 KiB | 80 KiB | 5 | 0 | 0 |
| after first workload | 17.7 MiB | 0.00% | 14 | 2 | 548 KiB | 1.27 MiB | 5 | 6 | 0 |
| after rehash/backup | 17.8 MiB | 0.00% | 14 | 2 | 548 KiB | 1.28 MiB | 5 | 6 | 0 |
| after second workload | 15.4 MiB | 0.00% | 14 | 2 | 548 KiB | 410 KiB | 5 | 8 | 0 |

## Python comparison and findings

The Python reference comparison was not run; the optional same-board reference
was not supplied. This is permitted as a contextual comparison, not as a
waiver of Rust's mandatory physical qualification.

No implementation correctness, security, data-loss, or resource-stability
finding remains from the deterministic harness validation. The open blockers
are environmental/dependency blockers:

1. Q007 must first be accepted, or its separately reviewed provider-transport
   decision must explicitly unblock Q008.

## Exact verification

```text
uv run ruff format scripts/qualification_sbc.py tests/migration_rs/test_q008_sbc.py
uv run ruff check scripts/qualification_sbc.py tests/migration_rs/test_q008_sbc.py
uv run pyright scripts/qualification_sbc.py
uv run pytest tests/migration_rs/test_q008_sbc.py -q --tb=short --maxfail=1
cargo build --manifest-path rust/Cargo.toml --release
uv run python scripts/qualification_sbc.py --binary rust/target/release/eggpool --candidate-origin on-device-release-build --build-elapsed-ms 210000 --output migration-rs/closure/qualification/008-run-linux-aarch64-pi5.json
git diff --check
```

Results: formatting, Ruff, Pyright, and all six Q008 contract tests passed; the
native release build passed in 210 seconds; the physical run passed 24
functional observations and five resource snapshots; and the candidate was
stopped with isolated temporary roots cleaned up. The evidence hash is recorded
above. No Python or live-provider traffic was used.

The follow-up implementation correction now classifies the storage backing `/`
instead of aggregating unrelated block devices, and records bounded client
elapsed/first-byte timings for the finite and streaming workload. The focused
Q008 regression covers the storage classification helpers.

## Registry transition

Q008 is formally recorded as blocked, not accepted. Q009 remains queued behind
Q008, Q010 remains queued behind Q009, and M11 remains blocked on accepted Q010
plus its separate planning review. No future plan is unblocked by this
attempt. The physical run removes the operational SBC blocker, but a later
Q007 acceptance is still required before Q008 can promote Q009. The new
physical evidence is append-only; the original blocked artifact remains
unchanged.

## Append-only re-acceptance — 2026-09-10

Q011 accepted the missing Q001 live-provider evidence using the bounded,
secret-free GeneralCompute + MiniMax matrix. Q008's physical Raspberry Pi 5
evidence remains valid: the corrective implementation changed only the
qualification harness, fixtures, and deterministic regression tests, not the
Rust runtime or deployment surfaces. Q008 is therefore re-accepted and
promotes Q009. The original blocked disposition above is retained as
historical evidence.
