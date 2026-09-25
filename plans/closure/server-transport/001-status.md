# Server Transport Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/server-transport/001-eggserve-0.4.0-adoption-and-requalification.md`

Source subsystem roadmap:

- `plans/subsystems/server-transport-roadmap.md#milestone-001--eggserve-040-adoption-and-requalification`

Repository baseline reviewed: `777dcdd597b5606997df99ffe9191691ba6785fb`
(plan baseline; registration HEAD `9da73a66` added only the plan/roadmap
documents, no code delta)

Implementation commits or pull requests:

- `409491ea382cce4c81d254a9bebe0d161b07f467` — Adopt EggServe 0.4.0
  downstream transport and requalify (manifest/lockfile pin, focused
  transport guards, current-authority docs; statuses to `closing`)
- Closure commit (this record + statuses to `closed`; plans-only, no
  production delta over `409491ea`)

## 1. Executive finding

Milestone 001 is complete. EggPool consumes published `eggserve-server
0.4.0` (with transitive `eggserve-primitives 0.2.2`) from crates.io under
the exact same ownership/config/admission/lifecycle contract qualified by
Plan 250. The locked debug and release builds compile with zero production
source changes; the only Rust edits are a version-agnostic unit-test rename
and focused transport-fixture guards. The one behavioral intersection found
during qualification — upstream 0.4 retaining known-length framing for
exact-size bodies — was characterized as an improvement, pinned by a new
framing assertion, and accommodated in the keep-alive fixture without
touching production code. All focused, workspace, no-default, dependency,
and tooling gates are green; hosted CI and the dependency audit are green
on the implementation candidate.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Exact-pin `eggserve-server =0.4.0`, `default-features = false`, `features = ["tower"]` | `rust/Cargo.toml:71`; `rust/Cargo.lock` server `0.4.0` checksum `fb601019a2914ae99f264640b66c80496a67b7ea3315fd0809747d4f22327e40` | pass | Matches live-index values in plan §2 |
| Transitive `eggserve-primitives 0.2.2`, checksum match | `rust/Cargo.lock` primitives `0.2.2` checksum `78fd797e45a374bfa419bc75f7e497653ce25e24cde0e771cc332e267f18355c` | pass | Normal Cargo resolution, no git/path patch |
| No direct primitives dependency | `grep eggserve rust/Cargo.toml` shows only `eggserve-server` | pass | No existing-path need arose |
| No core/static/PHF ancestry | `cargo tree -i eggserve-core/-static/phf` all error (no match); `cargo tree -e no-dev` contains none | pass | Absence recorded as expected evidence |
| Same ownership shape (builder, `TowerToEggserve::with_policy`, `RequestBodyPolicy::Stream` 1 GiB, `into_parts`, quiesce → shutdown → await → close) | Locked debug + release builds compile with no `rust/src/server/mod.rs` production edit | pass | Zero production source changes |
| All `eggserve_runtime_config()` values unchanged | `git diff rust/src/server/mod.rs` shows only the unit-test rename | pass | 1024/1024, 1 GiB, 256 KiB, 256/128 KiB/16 KiB, disabled lifetime, 24 h/15 s/120 s/120 s/5 s intact |
| Generation-owned live admission authoritative | `coordinator_*`, compact tests green; no admission edit | pass | 150/150 focused application tests |
| Bodyless HTTP/1.1 keep-alive reuse green under Stream adapter | `production_listener_reuses_keep_alive_connection` passes on one connection, two 200s | pass | Fixture made framing-aware for 0.4 known-length framing (see §3) |
| Finite framing valid; SSE incremental/chunked without unsolicited trailers | New `eggserve_040_finite_response_framing_is_valid_without_trailers` + `eggserve_040_streaming_stays_incremental_without_trailers` | pass | Health now `Content-Length` framed; SSE chunked, no `Trailer` |
| HTTP/1.0 functional | `production_listener_serves_health_and_preserves_auth_boundary` (`HTTP/1.0 200`) | pass | Unchanged |
| Malformed/oversized/incomplete isolation; later healthy requests work | `parser_rejections_leave_the_listener_healthy`, body-limit + disconnect tests | pass | Unchanged |
| Compact + ordinary inference real-socket paths green | `compact_route_*` (2 tests), finite/streaming provider tests | pass | Unchanged |
| Child shutdown bounded, joined before database close | keep-alive drain, stalled-reader (≤10 s), `runtime_lifecycle_r009` (5/5) | pass | `report.database_closed` asserted |
| `tower-layer` attributed truthfully | Inverse tree: only axum/axum-core/tower parents; eggserve edge gone | pass | Retained via EggPool's own graph, not an EggServe regression |
| Before/after footprint recorded | Lock 380→380 packages; no-dev uniq nodes 416→416 (lines 959→958); release bytes 27248832→27264192 (+15 360, +0.06%) | pass | Same host/toolchain/profile |
| Default/no-default checks, locked builds, deny, trees, tooling, hosted CI | §4 commands; CI + audit green on `409491ea` | pass | Local strict-clippy drift is pre-existing (see §10, low) |
| Current-authority docs describe 0.4.0; canonical invariant de-versioned; history untouched | §9 file list; `git show --stat 409491ea` touches no `plans/244-*`…`250-*` | pass | `000-long-term-specification.md` now says “exact-pinned EggServe direct H1 runtime” |
| No medium+ unresolved integration defect | §10 | pass | One low residual (pre-existing local clippy drift) |

## 3. Production implementation evidence

Landed production delta is the manifest/lockfile upgrade only:

```toml
eggserve-server = { version = "=0.4.0", default-features = false, features = ["tower"] }
```

`rust/Cargo.lock`: `eggserve-server 0.3.0 → 0.4.0`,
`eggserve-primitives 0.2.1 → 0.2.2`, total package count unchanged at 380.

Non-production Rust edits (test guards, same files as the transport
authority):

- `rust/src/server/mod.rs`: renamed unit test
  `eggserve_03_policy_defaults_remain_eggserve_owned` →
  `eggserve_policy_defaults_remain_eggserve_owned` (version-agnostic; same
  OriginOnly/EggServe-owned assertions, still green). No production change.
- `rust/tests/server_transport.rs`:
  - new `eggserve_040_finite_response_framing_is_valid_without_trailers`:
    health carries exactly one framing declaration, JSON content type, no
    `Trailer` declaration, intact body;
  - new `eggserve_040_streaming_stays_incremental_without_trailers`: SSE
    head is `text/event-stream` + chunked, no `Trailer`, first
    `response.output_text.delta` arrives incrementally;
  - new `read_framed_response` helper + keep-alive reads follow the
    response's own framing instead of assuming chunked. Required because
    0.4.0 retains known-length (`Content-Length`) framing for the exact-size
    health body where 0.3.0 sent chunked; the pre-change fixture waited for
    a chunked terminator that no longer arrives and timed out. This is the
    upstream 0.4 known-length improvement from the roadmap, observed
    directly (probe: `content-length` present, `chunked` absent), not a
    regression.

What was deliberately NOT implemented: response-trailer adoption, new
EggServe ownership/policy APIs, direct primitives dependency, compatibility
facade, config/reload/schema changes, provider/coordinator/routing changes,
SBC campaign.

## 4. Verification executed

Toolchain (before and after, same host/profile): `rustc 1.89.0`,
`cargo 1.89.0`. Hosted CI uses stable via `dtolnay/rust-toolchain@stable`.

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test health -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -p eggpool -e no-dev
cargo tree --manifest-path rust/Cargo.toml -p eggpool -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggserve-core
cargo tree --manifest-path rust/Cargo.toml -i eggserve-static
cargo tree --manifest-path rust/Cargo.toml -i phf
cargo tree --manifest-path rust/Cargo.toml -p eggpool -i tower-layer
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

### Results

Local (exact implementation candidate `409491ea`):

- `server_transport`: 9/9 (7 pre-existing + 2 new 0.4.0 guards).
- `health`: 8/8. `runtime_lifecycle_r009`: 5/5.
- Application/wire: c008 29, c009 13, c011 17, boundaries 5,
  finalization 10, publication 6, wire_stream 18, wire_runtime 8,
  wire_qualification 16, codex_responses_compat 15,
  codex_compaction_compat 13 — 150/150, 0 failed.
- Full workspace default: 61 suites, 720 passed, 0 failed.
- Full workspace `--no-default-features`: 61 suites, 721 passed, 0 failed
  (one no-default-specific test; parity holds).
- `cargo fmt --check`: clean. `cargo check --no-default-features`: clean.
- Locked debug + release builds: clean.
- `cargo deny check`: `advisories ok, bans ok, licenses ok, sources ok`.
- Feature tree: `eggserve-server 0.4.0` `tower` = `http-interop +
  tower-service` (no `tower-layer` activation), primitives `0.2.2`.
- Tooling: ruff format (44 files) + ruff check clean; pyright 0 errors;
  pytest 108 passed, 1 skipped.
- Local strict clippy: red on two PRE-EXISTING lints only
  (`eggpool-connect/src/install.rs:660` `redundant_closure_call`,
  `src/operations/update.rs:955` `nonminimal_bool`), proven identical at
  the pre-change baseline via `git stash` re-run; zero findings in any file
  touched by this milestone. See §10.

Hosted (implementation candidate `409491ea`, branch `main`, push):

- CI run `36188183987`: success (fmt, default + no-default clippy/check,
  full workspace tests, uv/ruff/pyright/pytest per `ci.yml`).
- Dependency audit run `36188183887`: success (advisories/bans/licenses/
  sources).
- The closure commit adds only `plans/**` files, which `ci.yml`
  `paths-ignore` excludes and which do not match the audit workflow's
  `paths` filter; the validated Rust/tooling tree is byte-identical to
  `409491ea` outside `plans/`.

## 5. Invariant review

- Downstream-only EggServe transport: `rust/src/server/` diff contains no
  coordinator logic; server remains a thin adapter (unit + real-socket
  suites green).
- Axum router is application authority: untouched.
- Generation-owned live admission authoritative on all four inference
  routes: compact + body-ceiling suites green; ceilings unchanged.
- 1 GiB transport/Tower ceilings finite and unchanged: config constructor
  untouched; `TowerToEggserve::with_policy` + `Stream { max_bytes: 1 GiB }`
  compile as the same shape.
- Runtime-limit values and EggServe-owned policy/admission defaults
  unchanged: ownership unit test green as-is (renamed only).
- Origin-form request-target mode: asserted by the same unit test.
- No core/static/PHF return; no direct primitives dependency: graph
  evidence in §2.
- No accidental `Trailer` declaration on normal JSON/SSE responses: new
  guards assert absence on both paths.
- Child completion awaited before database close; unexpected completion
  stays typed `ServerError::EggServe`: shutdown tests + r009 green.
- Default and no-default parity: both full suites green (720 / 721).
- Secret-free diagnostics/evidence: raw-socket assertions inspect framing
  headers only; no credentials, prompts, bodies, or cache keys in evidence.

## 6. Failure and recovery review

- Malformed/oversized-target/oversized-header/incomplete-body rejections
  isolate per request and leave the listener healthy for subsequent
  requests (`parser_rejections_leave_the_listener_healthy`).
- Content-Length and chunked over-limit bodies return the EggPool 413
  contract without poisoning later traffic; incomplete uploads disconnect
  cleanly (`eggpool_live_body_limit_applies_to_content_length_and_chunked_bodies`).
- Compact over-limit (both framings) 413s; later compact/health requests
  still reach generation admission (`compact_route_enforces_live_generation_body_ceiling`).
- Stalled downstream reader: bounded forced close within the foreground
  deadline, database closes last, provider observes cancellation
  (`stalled_stream_reader_is_forced_closed_before_shared_resources`).
- Requested shutdown drains the EggServe child before process/database
  close; database close is idempotent across suites.
- No new runtime state machine was introduced, so no new
  failure/cancellation semantics required review; any observed change would
  have been treated as a migration defect. None was observed except the
  intended known-length framing improvement (§3).

## 7. Migration and compatibility review

- Dependency migration: `eggserve-server 0.3.0 / primitives 0.2.1` →
  `0.4.0 / 0.2.2`, exact-pinned, crates.io-sourced, checksums match the
  live index. No config/schema/data migration. No client HTTP/API change:
  HTTP/1.0 + HTTP/1.1, auth boundaries, compact semantics, and SSE shape
  all requalified green.
- New upstream declaration-aware trailer APIs not adopted; EggPool declares
  no response trailers, so trailer support stays dormant by construction.
- Rollback remains a normal exact-pin/lockfile revert to 0.3.0 (not
  needed). No upstream package was yanked or altered.
- Legacy Plans 244–250 untouched; Plan 250 remains the 0.3.0 migration
  evidence.

## 8. Security review

- Auth boundaries requalified over real sockets (unauthenticated inference
  rejected, authenticated status/health served, static-asset and dashboard
  exemptions intact).
- Parser ceilings (16 KiB target, 256 headers, 128 KiB aggregate) and
  body DoS ceilings (1 GiB transport/Tower + generation-owned live limit)
  unchanged and covered by rejection/isolation tests.
- `cargo deny` clean on the final graph (advisories/bans/licenses/sources).
- No new secrets, logs, metrics, or diagnostics; closure evidence is
  secret-free (framing headers, counts, sizes, checksums only).

## 9. Documentation and operations

Updated current-authority version statements (0.3.0 → 0.4.0):

- `README.md`, `architecture/README.md`, `architecture/overview.md`,
  `architecture/deep-dive-runtime.md`, `architecture/deep-dive-core.md`,
  `rust/README.md`, `AGENTS.md`,
  `.opencode/skills/architecture/SKILL.md`,
  `.opencode/skills/development/SKILL.md`.

Durability correction (plan-directed, no ownership change):

- `plans/000-long-term-specification.md` invariant 3 now reads
  “exact-pinned EggServe direct H1 runtime” instead of “EggServe 0.3”.

Untouched history: `plans/244-*`…`plans/250-*` flat files unchanged
(verified in `git show --stat 409491ea`); remaining `0.3.0` mentions live
only in the implementation plan (from→to narrative), the roadmap §4
baseline snapshot, and the registry's Plan 250 history rows — all correct
as written.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Local strict clippy (1.89.0/0.1.89) fails on two pre-existing lints in files outside this milestone (`eggpool-connect/src/install.rs:660`, `src/operations/update.rs:955`); identical failure proven at the pre-change baseline via stash re-run; hosted CI stable clippy is green. | None on this milestone; local-only toolchain drift. | Separate corrective/housekeeping plan if the local toolchain is adopted; do not bundle into transport milestones. |

No medium, high, or critical findings. No corrective pass required.

## 11. Roadmap disposition

Milestone 001 is closed. The server-transport roadmap (single-milestone)
is closed with it; no follow-up milestone is registered.

## 12. Registry updates

Applied in the closure commit:

- `plans/registry.md`: server-transport roadmap row removed from active
  table; M001 row removed from ready table; M001 added to recently closed
  as `closed`; unblock audit paragraph updated (see below).
- `plans/subsystems/server-transport-roadmap.md`: status `closing` →
  `closed`; milestone 001 `closing` → `closed` with closure-record link.
- `plans/implementation/server-transport/001-eggserve-0.4.0-adoption-and-requalification.md`:
  status `closing` → `closed`.

Unblock audit at close: no registered plan lists M001 as a hard or
interface dependency, so closing M001 promotes nothing. Future subsystem
roadmaps register only when ready to be reasoned about; no rows were
bulk-generated.
