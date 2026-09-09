# Q001 Closure — Qualification Contract, Target Matrix, and Evidence Schema Freeze

Status: closed

Recommendation: accept Q001 closure; promote Q002 as the sole dependency-ready
M10 implementation plan.

Implementation candidate: `4b5093a30a56f78c14aef3ffef8c47863fc74070`

Plan: [Q001 — qualification contract, target matrix, and evidence schema freeze](../../implementation/qualification/001-qualification-contract-target-matrix-and-evidence-freeze.md)

## Outcome

Q001 freezes the M10 qualification contract in
[`m10-q001-manifest.json`](../../fixtures/qualification/m10-q001-manifest.json).
The artifact is machine-readable, versioned as `m10-q001.v1`, contains 101
mandatory cells, and adds no production capability, dependency, schema, CI job,
or runtime behavior. The six focused tests in
[`test_q001_manifest.py`](../../../tests/migration_rs/test_q001_manifest.py)
validate the contract itself.

Manifest SHA-256:

```text
a4b5d272c41b47bd30c39e5811ccc3a75545345148f6d78cea1c35bbf222bdb2
```

## Requirement-to-evidence matrix

| Q001 requirement | Evidence | Result |
|---|---|---|
| Every cell has the frozen required shape and a unique stable ID | `test_manifest_has_unique_cells_and_complete_required_shape` | Pass; 101/101 cells |
| All 63 O001 command paths are represented | `test_manifest_covers_every_frozen_o001_command_path`; O001 fixture | Pass; 63/63 exact paths |
| All public inference surfaces have finite and streaming cells | `test_manifest_covers_all_public_inference_surfaces_and_modes` | Pass; Chat Completions, Responses, and Messages, 6/6 modes |
| Mandatory cells have existing evidence or a later owner | Manifest cell validation | Pass; 93 reuse cells and 8 owner-only cells |
| Normalization references resolve and preserve semantic fields | Manifest normalization registry and validator | Pass; 8 explicit rules, `none` for exact comparisons |
| Evidence is bounded and secret-free | `test_manifest_contains_no_credential_bearing_fixture_fields`; evidence schema | Pass; no credential values, bodies, or full environment dumps authorized |
| Database, dashboard, platform, live, SBC, and stability categories exist | `test_manifest_has_all_environment_categories_and_future_owners` | Pass |

## Frozen cell inventory

The manifest contains 101 mandatory cells:

| Observation class | Count |
|---|---:|
| exact | 87 |
| semantic | 4 |
| characterization | 5 |
| manual-review | 5 |

The 63 O001 command cells reuse the frozen O001 fixture, Rust parser contract,
and O010 two-sided help corpus. The remaining cells cover configuration and
filesystem precedence, mutation/reload, operations, health/readiness/models,
all public inference surfaces, provider transformations, semantic routing,
retry/no-replay, durable state, generations/tasks, dashboard states/DOM/assets,
CI policy, and environment-specific evidence.

Future owner assignments are explicit. Counts include cells that retain focused
evidence while Q002 or a later plan owns aggregate/environment qualification:

| Owner | Assigned cells |
|---|---:|
| Q001 | 2 |
| Q002 | 18 |
| Q003 | 5 |
| Q004 | 5 |
| Q005 | 3 |
| Q006 | 1 |
| Q007 | 1 |
| Q008 | 1 |
| Q009 | 1 |

No Q010 cell is promoted by Q001. Q010 remains the aggregate M10 closure
plan.

## Target support matrix

| Target | Classification | Required evidence |
|---|---|---|
| Linux x86_64 | Supported | Q005 build/non-root/inference/control/backup/update; Q006 disposable rootful deployment |
| Linux aarch64 | Supported | Q005 build/non-root/runtime; Q008 physical SBC functional/resource characterization |
| macOS arm64 | Supported development | Q005 build and non-root runtime only; no service-deployment claim |
| Other Unix architectures | Not qualified | No M10 runtime or release-support claim |
| Windows | Unsupported | Unix control/process/deployment assumptions are explicit; no Cargo-only support inference |

Rootful Linux, live-provider, physical SBC, and sustained stability evidence are
separate environment classes. They are not normal-CI requirements and were not
run as part of Q001.

## Exact and semantic normalization contract

| Rule | Permitted normalization | Explicitly preserved |
|---|---|---|
| `none` | Nothing | Status, exit class, field presence, ordering, identity, errors, and durable effects |
| `isolated_path_root` | Test-created temporary root token | Suffixes, file contents, archive members, unrelated paths |
| `ephemeral_port` | Test-only loopback port number | Host, scheme, path, status, and upstream target |
| `process_identity` | PID value | Exit, signal, runtime state, and process count |
| `uuid_identity` | Generated UUID-like value where identity shape is the contract | Presence, field name, relationships, durable identity |
| `relative_timestamp` | Approved elapsed/relative scalar | Ordering, freshness, retention, durable timestamp presence |
| `json_object_order` | JSON object member ordering | Arrays, null/absent, zero/absent, numbers, and strings |
| `characterization_only` | Resource/latency scalar reporting | Semantic parity and any invented threshold |

No rule normalizes HTTP status, SSE terminal category/order, CLI exit class,
error/effect category, routing/account/model identity, durable row state/counts,
retry action/count, distinguished JSON presence, HTML text/escaping/controls, or
backup/archive semantics.

## Environment metadata and CI policy

The evidence record requires candidate SHA, implementation, environment,
manifest version, command ID, result, and bounded reason category. Optional
fields cover sanitized OS/distribution/kernel/architecture, SBC hardware,
toolchain/build profile, elapsed/resource scalars, and artifact/result hashes.
Only named EggPool configuration/API-key variable names are allowlisted; their
values are never evidence. Credentials are external to the repository.

The CI decision is:

- keep the existing `tests/smoke/` gate in normal CI;
- run the Q001 validator and deterministic migration suite locally and during
  later deliberate qualification closure;
- make Q002-Q005 aggregate/target runners eligible for explicit
  `workflow_dispatch` only after implementation;
- keep Q006/Q008/Q009 physical or disposable-host work out of normal CI;
- keep Q007 credentialed live-provider work opt-in and cost-bounded.

No large always-on qualification matrix is justified by Q001.

## Source-audit findings

No unresolved high- or medium-severity architecture, product, security,
compatibility, or portability finding blocks the next qualification plan.
Two informational findings are retained in the manifest:

- F005's historical placeholder wording is superseded for current qualification
  by the later C009/O010 closures and current Rust coordinator paths; the
  historical record is not rewritten.
- Unix control sockets, process signalling, permissions, and systemd deployment
  are explicit Unix boundaries. Windows is therefore classified unsupported and
  other Unix architectures not qualified rather than inferred from Cargo.

The current README/docs still keep Python as the public install/update path and
describe the Rust binary as a local qualification candidate. That agrees with
the M10 no-cutover boundary; no documentation contradiction was found.

## Verification commands actually run

```text
rtk cargo test --manifest-path rust/Cargo.toml --all-targets --no-run
rtk uv run pytest tests/migration_rs/test_q001_manifest.py -q --tb=short --maxfail=1
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk uv run pyright src/ scripts/
rtk git diff --check
```

Observed results:

- Rust all-target no-run compilation passed.
- Q001 manifest validator: 6 passed.
- Migration oracle: 108 passed, 3 skipped.
- Smoke suite: 14 passed.
- Ruff format: 734 files already formatted.
- Ruff check: all checks passed.
- Pyright: 0 errors, 0 warnings, 0 informations.
- Final diff check passed.

No live provider, rootful host, physical SBC, paid request, or release artifact
was used. Those are intentionally owned by Q006-Q009.

## Registry transition and future-plan audit

Q001 moves from the dependency-ready queue to the completed implementation
plans with candidate `4b5093a30a56f78c14aef3ffef8c47863fc74070`. Q002 is
promoted as the sole dependency-ready M10 plan because its hard dependency is
now accepted. Q003 remains queued behind Q002; Q004-Q010 remain queued behind
their direct predecessors. M10 remains active, and M11 remains blocked on
accepted Q010 plus its own separate planning review. M12 remains sequenced
behind M11. No other future plan is unblocked by Q001.

The next recommendation is to begin Q002 implementation and use this manifest
as its required coverage/normalization contract.
