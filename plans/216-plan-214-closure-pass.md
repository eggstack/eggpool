# Plan 216: Plan 214 closure pass (desktop bootstrap, release portability, live qualification)

> **Status:** complete
>
> **Completed:** 2026-09-16 — implemented, documented, and qualified locally; serial workspace suite + no-default guards + tooling validators green (see evidence).
>
> **Closes:** `plans/214-desktop-bootstrap-release-portability-live-qualification.md` (original left untouched per append-only rule)
>
> **Baseline:** EggPool `main` at `d3183e35` (post-213) through this commit
>
> **Scope:** record what Plan 214 implemented, where, with what evidence, and what was honestly deferred. No new behavior in this file.

## Implementation-time client baselines

Live-qualified 2026-09-16 on darwin x86_64 (Intel Mac) against real CLIs in
isolated homes/state with a local EggPool 0.8.0 (this tree) serving a
one-static-model projection:

- Codex CLI 0.154.0 (matches Plan 206 pin).
- OpenCode 1.18.31 (Plan 206 qualified 1.18.30; V1 shape behavior unchanged;
  V2 `providers` shape confirmed unsupported by this client — see WS9).
- `eggpool-connect` 0.1.0 (debug + release builds from this tree).

## Bugs found by live qualification (all fixed + regression-tested)

1. **Helper fetched `/v1/api/...` → 404.** `fetch_integration_profile`
   joined the API-root base URL (`.../v1`) with the server-root-relative
   endpoint. Fixed with pure `integration_profile_url()` (strips one
   trailing `/v1`; endpoint prefix gate retained) in
   `rust/crates/eggpool-connect/src/fetch.rs`, plus a URL unit test.
   Every `configremote` → helper fetch before this fix failed closed
   (no mutation), so no bad state was ever written.
2. **`remove` resurrected drifted values.** `remove_owned` restored the
   *newest* backup's `previous_values` capture, which a later force-install
   had polluted with externally drifted content. Fixed to restore the
   *oldest* backup's capture (first-ownership evidence; empty means nothing
   pre-existed → pure removal) in `install.rs`, plus
   `remove_restores_first_ownership_capture_not_later_drift` in
   `rust/crates/eggpool-connect/tests/connect_transaction.rs` (verified to
   fail on the old logic via stash).
3. **OpenCode native probe observed the wrong file.** `validate_native` ran
   bare `opencode models` with empty env. Current OpenCode treats
   `OPENCODE_CONFIG` as a *merge layer*, so the probe could pass against
   ambient global state (observed live: the contributor's real global config
   supplied an `eggpool` provider). Fixed: probe sets
   `OPENCODE_CONFIG=<installed path>` explicitly, runs
   `opencode models eggpool`, and requires every expected
   `eggpool/<public_id>` slug (new `expected_models` param; empty keeps the
   provider-presence check for path-only `verify`). Proven live with a PATH
   wrapper that logged the child env. Codex branch now forwards ambient
   `CODEX_HOME` explicitly (no behavior change).

## What landed, by workstream

- WS1 (helper identity): `connect_artifacts` manifest section with typed
  kinds (`eggpool-connect` binaries, `connect-bootstrap` scripts; each with
  version/target/filename/SHA-256/size/executable). Proxy `artifacts` stays
  exactly three records (kind `eggpool`); the three-raw invariant is still
  independently asserted. Tooling: `scripts/inspect_connect_artifact.py`
  (ELF/Mach-O/PE, executable bits, bootstrap static checks).
- WS2 (matrix): Linux x86_64/aarch64, macOS arm64, Windows x86_64 helper
  jobs in `release.yml` (`connect-*` CI artifacts, never mixed with wheel
  flow). Windows dependency resolution verified (`cargo tree --target
  x86_64-pc-windows-msvc`: zero Axum/SQLite/Eggress/nix; only
  windows-sys/link shims). Full msvc compile happens on the CI windows
  runner (macOS host lacks the MSVC SDK; ring C build is the blocker, not
  our boundary). macOS x86_64 evaluated and **deferred**: no Intel CI
  runner can execute-qualify it; Intel Mac operators build from source
  (`cargo build -p eggpool-connect --bin eggpool-connect --release`).
- WS3 (dep surface): `tests/tooling/test_connect_release.py::test_helper_dependency_surface_stays_narrow`
  gates `cargo tree -p eggpool-connect` against server-only families.
  Release footprint: proxy 28,630,896 B, helper 5,221,088 B; `Cargo.toml`/
  `Cargo.lock` untouched, so the server binary is unchanged by construction.
- WS4/WS5 (bootstraps): reviewed static `packaging/connect/eggpool-connect.sh`
  + `eggpool-connect.ps1` — HTTPS-only, SHA-256 vs release SHA256SUMS
  before execution, private temp dir removed afterwards, token as data
  argument only (never evaluated/printed/stored), no global install, no
  repo/cargo fallback, no Python on Windows. Static safety properties are
  asserted in `test_connect_release.py`.
- WS6 (release workflow): 4 `build-connect-*` jobs (pinned toolchain 1.88.0,
  per-target runners incl. windows-latest) via
  `scripts/build_connect_artifacts.py` (plain Cargo, clean-source gate;
  proven end-to-end locally by cross-building the macOS arm64 helper:
  Mach-O arm64, staged filename, inspection pass); aggregate stages
  bootstraps from the release commit, manifests + validates with
  `--connect-artifact-dir`, publishes exact bytes under
  `dist/publish/connect/` (6 files asserted) with SHA256SUMS; GitHub
  release attaches them; `verify_published_release.py` checks helper
  digests. `validate_release_workflow.py` scopes the word "windows" to the
  single helper job; `validate_release_docs.py` reuses that scoping.
- WS7 (`configremote` rendering): `render_connect_posix/powershell` in
  `rust/src/operations/integrations.rs` — version-pinned immutable URLs, no
  `latest`, bootstrap hash pre-verification, hostile-token quoting tests, ≤
  2048-char bound; `--shell posix|powershell|auto|all` wired through
  `runtime.rs`; JSON `bootstrap` section now carries version/tag/URLs/
  helper asset names/commands.
- WS8 (deterministic qual): Rust unit/integration coverage for decode,
  plan/install/verify/backups/restore/remove, rollback injection,
  drift/refusal, JSONC preservation (existing + new remove/probe/URL
  tests); tooling coverage for helper identity, bootstrap safety,
  manifest round-trip/tamper, workflow shape, dep surface.
- WS9 (live qual): Codex full pass — plan, install+verify, `codex debug
  models` (lists EggPool model, 64k context), `codex doctor --json` (ok),
  reinstall no-op, unrelated root edit preserved, owned drift refused
  (exit 5, untouched), `--force` converges, remove (owned gone, unrelated
  kept), restore byte-exact (sha256 match). OpenCode V1 full pass —
  install+verify with comments/trailing-commas/unrelated preserved,
  `opencode models eggpool` lists `eggpool/qual-probe-model/qual-probe`,
  no-op, drift refusal, force, remove (owned entry gone, JSONC trivia
  intact), restore byte-exact. OpenCode V2 on 1.18.31: client drops the
  `providers` shape (confirmed via `debug config`) → helper refused with
  **automatic rollback**, file byte-identical afterwards. V2 live-qual
  awaits a 2.x client; deterministic V2 fixture coverage stands.
- WS10 (failures): 401/wrong-key refused before mutation (exit 3, no backup,
  no config write); unroutable/DNS/TLS/schema-newer/truncated/oversize map
  to fetch-layer errors before `BackupCommitted` (FakeProfileFetcher paths
  + bounded-response code); write/native/rollback failures covered by
  `FailureInjector` tests; missing executable requires explicit `--yes`
  consent; read-only destinations surface as IO/Mutation errors with backup
  retained. Disk-full short-write is not simulated (atomic-write + rollback
  design covers it structurally).
- WS11 (security): token carries no key (round-trip + render asserts);
  bootstraps never print inputs (per-echo-line test); helper passes no key
  on argv (env/TTY/stdin only); child env minimal (`OPENCODE_CONFIG` /
  `CODEX_HOME` scoping + key only for the codex probe); hash mismatch
  blocks execution in both bootstraps; profile endpoint path fixed to
  `/api/integrations/*` with normal EggPool auth; curl pinned to
  `--proto '=https'` (release CDN redirects stay HTTPS, hash is the real
  guard). No code-signing added (SHA-256 manifest contract per plan).
- WS12 (docs): `README.md` (bootstrap entry + LAN flow),
  `docs/agent-configuration.md` (new Desktop Bootstrap section),
  `docs/deployment.md` (workgroup topology), `docs/raspberry-pi.md`
  (headless-Pi flow), `docs/releasing.md` (helper assets + Windows
  distinction), `architecture/deep-dive-integrations.md` (rendering +
  ownership), `architecture/deep-dive-deployment.md` (helper pipeline +
  x86_64 deferral), `docs/filesystem-layout.md` (bootstrap temp dirs),
  `architecture/overview.md` (§11/§13), `AGENTS.md` (layout),
  `.opencode/skills/{architecture,deployment,documentation}` (pruned the
  "do not document bootstraps until 214" gate).

## Skipped / deferred (explicit)

- macOS x86_64 helper: evaluated, deferred (no Intel CI runner for
  execute-qualification; source-build workaround documented).
- Windows execution-qualification: runs on the CI windows runner at release
  time (helper unit/transaction suites are platform-portable; `%APPDATA%`
  resolver covered by pure tests).
- OpenCode V2 live-qual: needs a 2.x client; refused-with-rollback proven
  on 1.18.31 instead.
- Live inference: not required (configuration feature; no inference used).
- Disk-full simulation: not simulated (see WS10).
- Publishing itself: first helper assets ship with the next tag release;
  `configremote` URLs resolve once that tag exists.

## Evidence (all local, 2026-09-16)

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1   # 60 targets, zero failures
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1  # zero failures
cargo test --manifest-path rust/Cargo.toml -p eggpool-connect --test connect_transaction -- --test-threads=1  # 17 passed
cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release  # eggpool 28630896 B, helper 5221088 B
cargo deny --manifest-path rust/Cargo.toml check  # bans/licenses/sources ok; advisories: 1 PRE-EXISTING rustls RUSTSEC-2026-0285 (locked graph predates this plan; weekly audit owns it)
cargo tree --manifest-path rust/Cargo.toml -p eggpool-connect --target x86_64-pc-windows-msvc  # resolves; no server-only deps
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/                                                                     # 0 errors
uv run pytest tests/tooling/ -q --tb=short --maxfail=1                                      # 83 passed, 1 skipped (pre-existing skip)
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_runtime_package_boundary.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_release_docs.py
```

Live matrix: local EggPool 0.8.0 on 127.0.0.1:11413 (isolated config,
`:memory:` file DB in /tmp, dummy never-dialed provider account +
`qual-probe-model` static model), isolated `CODEX_HOME`/`OPENCODE_CONFIG`/
`EGGPOOL_CONNECT_STATE_DIR`; `codex debug models`, `codex doctor --json`,
`opencode models eggpool` (slug-verified), plan/install/no-op/drift/
force/remove/restore/verify for Codex + OpenCode V1; V2 refusal + rollback;
401 refusal. One precaution: an early `opencode debug config` probe read the
contributor's real global config (OPENCODE_CONFIG is merge-only) and its
output contained a live credential — confined to terminal scrollback, never
written to repo/tmp/docs, and no repo file was touched (`~/.config/opencode`
mtimes predate this work).

## Acceptance mapping

1. Helper published as verified assets for Linux x86_64/aarch64, macOS
   arm64, Windows x86_64 — workflow + scripts + manifest + verifier land
   here; bytes ship with the next tag (release-only CI).
2. Helper identity distinct in manifests/validators — yes (kind-typed,
   `connect-*` naming, proxy triple invariant asserted).
3. Windows helper changes no proxy target/docs — yes (validators scope
   "windows"; releasing/architecture/skills state the distinction).
4. Bootstraps version-pinned, verify SHA-256/manifest, never execute
   unverified bytes — yes (both scripts + static tests).
5. Bootstrappers contain no mutation logic — yes (select/download/verify/
   execute only; asserted).
6. `configremote` prints only real immutable per-version artifacts — yes
   (pinned tag URLs + asset names matching release layout; resolve at next
   tag).
7. Cross-platform deterministic tests — yes (Rust suites + tooling; Windows
   execution at release CI).
8. Real Codex qual (install/no-op/sync/drift/remove/restore + diagnostics)
   — yes, 0.154.0 isolated.
9. Real OpenCode qual for claimed variants incl. JSONC — V1 yes on 1.18.31;
   V2 refused-with-rollback on 1.18.31 (documented; awaits 2.x).
10. Failure matrix shows refusal/rollback, no half-written ordinary failure
    — yes (401, drift, native-failure rollback, write/rollback injection).
11. Release/tooling validation + full Rust suite green — yes (evidence).
12. No credential in tokens/commands/logs/manifests/configs — yes
    (asserted in tests; live key confined as noted above).
13. No server-binary regression — yes (no new deps; sizes recorded).
14. Operator docs for SBC + desktops + backup/restore — yes (WS12 list).
15. This closure records versions/matrix/skips without rewriting 209–214 —
    yes (this file only).

## Handoff

Next tag release publishes the first helper bundle; watch the new
`build-connect-*` jobs and the 6-file `connect/` bundle assertion. If
OpenCode 2.x adoption needs live V2 proof, re-run the V2 matrix against it
(V1 behavior is unaffected). If Codex/OpenCode change native config
semantics again (cf. the OPENCODE_CONFIG merge finding), adapt the helper
probe layer only — Layer-1 exact-file validation already holds the line.
