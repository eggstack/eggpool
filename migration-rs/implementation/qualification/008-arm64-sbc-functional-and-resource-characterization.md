# Q008 — ARM64 SBC Functional and Resource Characterization

Status: accepted after Q011 corrective closure; closed 2026-09-10 (see [closure](../../closure/qualification/008-status.md))

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: planning baseline `00dd27fa103e3c663968ecd95d9289c60fca0601`; implement against current main after accepted Q007.

Primary class: invariant/polish

Hard dependency: accepted Q007.

Operational dependency: access to at least one representative Linux aarch64 SBC.

## Objective

Run the completed Rust candidate on real ARM64 SBC hardware representative of EggPool's documented deployment goal and characterize functional behavior plus resource use. This is not a synthetic benchmark contest; it is a deployment-readiness check for lightweight local/LAN operation.

## Hardware requirement

At least one physical Linux aarch64 SBC is mandatory for closure. Preferred examples include a Raspberry Pi-class board or Libre Computer/Le Potato-class hardware, but Q001 target support is authoritative.

Record:

- board/model;
- SoC/CPU core count/frequency policy where available;
- RAM size;
- storage medium/filesystem;
- OS/distribution/version;
- kernel;
- architecture;
- Rust toolchain/build profile;
- power/thermal mode where materially relevant.

Do not store serial numbers, hostnames, IP addresses, or other identifying machine data in closure.

## Candidate preparation

Until M11 publishes canonical release assets, either:

- build `--release` directly on the SBC; or
- copy a candidate binary produced by a Q005-qualified compatible aarch64 build environment and verify its SHA.

Record which path was used and build elapsed time if built on-device. Slow source compilation is a characterization fact, not itself a runtime failure.

## Functional acceptance

Using isolated config/data/runtime roots and a deterministic loopback provider, exercise:

- version/help/check-config;
- migration/startup/readiness;
- finite Chat/Responses/Messages request cells required by Q001;
- streaming request;
- model listing;
- rehash/runtime-status;
- account/model/operator stats read;
- background task registration/tick where practical;
- backup creation and validation/recover into isolated root;
- graceful shutdown/restart;
- startup reconciliation of reviewed fixture state;
- dashboard page/static fetch;
- Eggress connector construction and one deterministic local proxy path if Q001 assigns it to SBC qualification.

## Resource characterization

Collect bounded measurements at stable points:

### Binary/build

- release binary size;
- on-device release build time and peak build RSS if measured without extra intrusive tooling;
- startup-to-ready elapsed time.

### Idle

After a defined warmup window:

- process RSS and virtual size where available;
- CPU utilization over a fixed sample window;
- open file descriptor count;
- thread count;
- DB/WAL size;
- number of runtime tasks/generations/finalization jobs.

### Request workload

Run a small deterministic loopback workload, for example a fixed number of finite and streaming requests at low concurrency. Record:

- client-visible elapsed/TTFT distributions as characterization only;
- CPU/RSS range;
- fd/thread counts before/after;
- DB/WAL growth;
- request/attempt/reservation convergence;
- no leaked generations/jobs/wire flights/gates.

### Maintenance

Record:

- rehash elapsed time;
- backup elapsed time/archive size;
- checkpoint/vacuum or bounded maintenance elapsed time if safe;
- shutdown elapsed time.

## Python reference comparison

Where practical, run the final Python reference on the same board/config/loopback fixture and record the same coarse measurements. The purpose is context and detection of clearly impractical regressions, not a fixed requirement that Rust be faster or use a specific percentage less RAM.

If Python cannot reasonably run on the board, document why; Rust functional/resource qualification remains mandatory.

## Measurement discipline

Prefer standard OS tools/procfs and a small script over adding profiling dependencies. Samples must identify warmup/sample duration and avoid claiming precision beyond the tool.

Do not set an arbitrary RSS/latency pass threshold during implementation. Treat these as blockers if observed behavior demonstrates:

- crash/OOM;
- persistent CPU spin at idle;
- fd/thread/task/generation leak;
- unbounded DB/WAL/resource growth under bounded workload;
- clearly unusable startup/request behavior for the documented SBC role.

Borderline performance is reported for M11 rather than hidden.

## Repeated-run stability

Repeat the functional/resource scenario enough times to distinguish one-time cache allocation from monotonic growth. At minimum capture before warmup, after warmup, after request workload, after rehash/backup, and after a second request workload.

## Required harness

Create a small SBC characterization script that emits the Q001 evidence schema and resource samples. It must:

- use only bounded local workload by default;
- accept an existing candidate binary;
- refuse to run against non-isolated config/data roots unless explicitly overridden for a disposable host;
- avoid privileged system mutation;
- collect no host network identity or full environment dump;
- leave the server stopped at completion.

## Verification

On the SBC run the exact build/acceptance commands and Q008 script. After any source fix, rerun the corresponding local deterministic Rust/Python regression suites and Q005 target acceptance.

## Non-goals

Q008 does not perform maximum throughput/load benchmarking, thermal overclock testing, fleet deployment, release asset publication, or define a product SLA from one board.

## Closure evidence

Write `migration-rs/closure/qualification/008-status.md` with:

- sanitized board/environment metadata;
- candidate/build identity;
- functional matrix;
- resource sample table;
- Python contextual comparison if performed;
- repeated-run stability findings;
- any ARM64-specific fixes/regressions;
- limitations/measurement caveats;
- unresolved findings and registry transition.

## Acceptance criteria

Q008 closes only when at least one representative Linux aarch64 SBC completes all mandatory functional cells, no correctness/resource-stability blocker remains, and enough resource characterization is recorded for M11 to make informed release/support decisions.

Accepted Q008 promotes only Q009.

Implementation note: `scripts/qualification_sbc.py` and the
`q008-sbc.toml` loopback fixture implement the guarded bounded harness and
machine-readable `m10-q008.v1` evidence schema. The harness refuses to claim
physical SBC evidence unless Linux/aarch64 execution and a device-tree board
model are present. A real Raspberry Pi 5 run is recorded in the closure
evidence; the initial blocked disposition is retained historically and the
append-only re-acceptance records Q007's corrective dependency closure.
