# Plan 214: Desktop bootstrap, release portability, and live remote-setup qualification

> **Status:** ready for implementation
>
> **Baseline:** EggPool `main` at `6997cdbb4cee4ec69c71cab15b53b039c7e3309a` (2026-09-16)
>
> **Parent:** Plan 209
>
> **Depends on:** Plans 210–213
>
> **Primary authority:** `.github/workflows/release.yml`, `scripts/build_release_artifacts.py`, `scripts/create_release_manifest.py`, `scripts/validate_release_artifacts.py`, `scripts/verify_published_release.py`, `scripts/install.sh`, `tests/tooling/`, `docs/releasing.md`, `docs/agent-configuration.md`
>
> **Priority:** P1 — distribution and closure qualification
>
> **Scope:** distribute `eggpool-connect` as a small verified desktop artifact, emit safe copy/paste bootstrap commands from `eggpool configremote`, and qualify the complete headless-proxy-to-desktop flow across supported desktop operating systems without expanding the EggPool proxy support matrix accidentally.

## Objective

Plans 210–213 provide the profile format, server export/API, desktop transaction engine, and client adapters. This plan makes the feature practical to deploy:

```text
SBC/server:
  eggpool configremote codex
            |
            v
  version-pinned POSIX + PowerShell bootstrap commands
            |
            v
Desktop:
  download eggpool-connect for this OS/arch
  verify release manifest/hash
  execute with epc1 token
  plan -> backup -> install -> client-native verify
```

The bootstrap path must be smaller and safer than asking each user to clone EggPool, install Rust, install Python, or manually merge config files.

The release design must keep a critical distinction:

> publishing an `eggpool-connect` Windows binary does **not** mean the EggPool proxy/server is supported on Windows.

The helper and proxy may have different release target matrices.

---

# Workstream 1 — Define helper artifact identity separately from proxy artifacts

The current release workflow builds the EggPool runtime wheel/raw pair for:

- Linux x86_64;
- Linux aarch64;
- macOS arm64.

Add explicit release-manifest artifact kinds rather than overloading existing raw runtime counts/naming.

Recommended identities:

```text
eggpool                 # proxy/server executable
 eggpool-connect         # desktop client configurator
```

Manifest entries should carry at least:

- artifact kind;
- version;
- target triple/class;
- filename;
- SHA-256;
- size;
- executable expectation;
- provenance/source commit already used by the release system.

Update validators so “three runtime raw artifacts” remains an independently testable invariant if that is still the proxy matrix. Do not make the helper's extra platform outputs look like extra server binaries.

---

# Workstream 2 — Desktop target matrix

Minimum intended helper matrix:

- Linux x86_64;
- Linux aarch64 where inexpensive/reused by current pipeline;
- macOS arm64;
- Windows x86_64.

Also evaluate macOS x86_64 because Intel Macs remain plausible desktop clients even though the current proxy runtime is only released for macOS arm64. Add it if CI/toolchain cost is modest and the resulting bootstrap story is materially cleaner.

Do not add a target merely to create a matrix. Each published helper binary must receive at least deterministic startup/profile/config fixture qualification for that target class.

### Windows boundary

The helper should be buildable/usable on Windows without requiring server-only Unix dependencies. If `eggpool-client-config` or helper code accidentally pulls in `nix`, Unix sockets, systemd, Eggress, or server lifecycle, fix the crate boundary rather than porting unrelated server code.

This target is an architectural test of Plan 210's extraction.

---

# Workstream 3 — Keep the helper dependency surface narrow

Build and inspect the standalone helper separately:

```bash
cargo build --manifest-path rust/Cargo.toml --bin eggpool-connect --release --locked
cargo tree --manifest-path rust/Cargo.toml -p eggpool-connect -e features
```

The helper should not link server-only dependencies without a clear need.

Expected functional categories are:

- Clap/argument parsing or similarly small CLI layer;
- serde/profile config parsing;
- bounded HTTPS client;
- platform path/filesystem/process helpers;
- format-preserving TOML/JSONC adapters if adopted;
- hashing/encoding.

Unexpected categories requiring review include:

- Axum server;
- SQLite;
- Eggress proxy transports;
- dashboard assets;
- routing/quota/health;
- provider request codecs;
- full Tokio server feature set.

Do not duplicate the entire EggPool binary under a new name just to save implementation time.

---

# Workstream 4 — POSIX bootstrap

Provide a small reviewed bootstrap for macOS/Linux. It may be a shell script because it only selects/downloads/verifies/executes the native helper; it must not implement client config mutation.

Responsibilities:

1. parse a profile token argument without eval;
2. detect OS and architecture;
3. determine the exact EggPool release version embedded in the command or script URL;
4. download the corresponding `eggpool-connect` artifact and release checksum/manifest over HTTPS;
5. verify SHA-256 before execution;
6. use a private temporary directory;
7. execute the verified helper with the profile token as a data argument/stdin;
8. clean temporary bytes on normal exit;
9. fail before execution on unsupported platform, missing checksum, mismatch, or ambiguous artifact.

Do not:

- pipe a mutable `main` branch script directly into a Python interpreter as the configuration engine;
- `eval` profile contents;
- disable TLS validation;
- silently fall back to `cargo install`/repo clone;
- print the API key;
- install the helper globally unless the user explicitly asks for a future permanent-install option.

A one-shot temp execution is sufficient for the first version. `eggpool-connect` can print instructions for permanent installation later.

### Command form

`eggpool configremote` should eventually emit a command pinned to the running EggPool release, for example semantically:

```text
curl .../releases/download/vX.Y.Z/eggpool-connect.sh | sh -s -- 'epc1....'
```

However, prefer a form that allows verifying the bootstrap itself rather than blindly executing mutable network content. A small release-pinned downloaded bootstrap plus published checksum is better if shell ergonomics remain acceptable.

Do not finalize the exact command until release artifacts and their immutable URLs are real.

---

# Workstream 5 — Windows PowerShell bootstrap

Provide an equivalent PowerShell entry point for Windows desktops.

Responsibilities match POSIX:

- release version pinned;
- OS/arch validation;
- HTTPS download;
- SHA-256 verification using built-in PowerShell/.NET facilities;
- private temp directory;
- no `Invoke-Expression`/eval of profile content;
- execute only the verified `.exe`;
- cleanup;
- bounded errors.

The profile token may be passed as a quoted data argument or via stdin if command-line length/history concerns make that preferable. Because the token is secret-free, shell history is acceptable from a credential standpoint, but quoting/length behavior still needs deterministic testing.

Do not require Python on Windows.

---

# Workstream 6 — Release workflow updates

Update `.github/workflows/release.yml` and supporting scripts carefully. The current pipeline intentionally builds exact bytes once, validates them, then publishes those bytes.

Preserve that property for helper artifacts:

1. build helper artifacts from the release commit;
2. upload as CI artifacts;
3. aggregate them into the exact release bundle;
4. include hashes/provenance in the release manifest;
5. publish the exact validated bytes;
6. let bootstrappers consume those exact published hashes.

Do not rebuild helper executables in the publish job.

### Wheel/PyPI boundary

The PyPI wheel primarily delivers the EggPool server/runtime. Do not force Windows/helper-only artifacts into a Python wheel design unless there is a strong reason. GitHub release assets are sufficient for `eggpool-connect` bootstrap.

Keep the package-manager compatibility story and helper distribution separable.

### Existing validators

Update:

- `scripts/build_release_artifacts.py` or add a narrowly named helper builder;
- `scripts/create_release_manifest.py`;
- `scripts/validate_release_artifacts.py`;
- `scripts/verify_published_release.py`;
- `scripts/validate_release_workflow.py` as needed;
- `tests/tooling/test_release_artifacts.py`;
- `tests/tooling/test_release_supply_chain.py`;
- related release/docs validators.

Avoid turning the current release workflow into a generic artifact framework larger than the project needs.

---

# Workstream 7 — `configremote` bootstrap rendering

Once release assets exist, finish Plan 211's default command output.

The running EggPool version should select matching helper/bootstrap assets. Do not point an EggPool 0.X server at `latest` helper code without a compatibility decision.

If helper/profile schemas are backward compatible, the command can still pin the server's release for reproducibility. A future explicit `--helper-version` override can be considered only if real compatibility needs arise.

Render both commands when target desktop OS is unknown:

```text
macOS/Linux: ...
Windows PowerShell: ...
```

Also support:

```text
--shell posix
--shell powershell
--shell all
--format token
```

Keep command length bounded and test quoting for realistic DNS/IPv6/base-URL/profile values.

---

# Workstream 8 — Deterministic cross-platform qualification

Add CI/fixture qualification that does not require real provider credentials.

For each published helper target where CI runners are available:

- `--help` and version startup;
- decode valid profile;
- reject invalid/oversize token;
- use a local fake EggPool integration-profile server or injectable fixture;
- configure isolated fake/current-shape Codex/OpenCode config trees;
- verify backup-before-write;
- inject post-write validation failure and prove rollback;
- restore selected backup;
- remove owned fields;
- preserve unrelated config/comments;
- confirm no secrets in output/state fixtures.

Use temporary homes and environment variables. Never point CI at runner-global Codex/OpenCode configuration.

Windows coverage must include path separators, drive paths, rename semantics, and user-state directory behavior.

---

# Workstream 9 — Real-client live qualification

Before closure, run a manual/opt-in qualification against current real clients in isolated homes. Record exact versions and source/date evidence in a closure plan/artifact.

## Codex matrix

On at least macOS/Linux and, where Codex supports it, Windows:

1. start/reuse an isolated reachable EggPool instance with non-production config;
2. generate `eggpool configremote codex` profile;
3. run helper `plan`;
4. install into isolated `CODEX_HOME`;
5. verify `codex debug models`;
6. verify `codex doctor --json`;
7. repeat install -> no-op;
8. mutate unrelated Codex setting -> sync preserves it;
9. mutate EggPool-owned field -> drift/refusal;
10. remove -> previous owned values restored;
11. restore from backup -> byte-exact expected state.

Live inference is optional and requires explicit provider credentials. The configuration feature does not require it to prove filesystem safety.

## OpenCode matrix

For every currently supported adapter variant:

1. use isolated config/home;
2. include a JSONC config with comments/trailing commas/unrelated provider;
3. install EggPool;
4. verify current `opencode models`/equivalent sees EggPool models;
5. confirm original comments/unrelated settings remain;
6. repeat install -> no-op;
7. sync a changed remote profile revision;
8. test owned-field drift refusal;
9. remove -> pre-existing provider value restored when seeded;
10. restore backup.

Where V1 and V2 clients are both intentionally supported, qualify both. If a client version cannot be obtained reproducibly, document it as unsupported rather than claiming coverage from fixtures alone.

---

# Workstream 10 — Network/failure qualification

The user-facing value depends heavily on clean failure modes. Exercise:

- desktop cannot route to advertised host;
- DNS failure;
- TLS verification failure;
- wrong API key/401;
- integration profile schema newer than helper;
- truncated/oversize response;
- server becomes unavailable between `plan` and `install`;
- client executable missing;
- client version unsupported;
- destination config read-only;
- backup directory unwritable;
- disk-full/short-write simulation where practical;
- client-native verifier timeout/non-zero exit;
- rollback write failure injection.

For every case classify expected result as:

- refusal before mutation;
- automatic rollback to verified prior state;
- explicit recovery-required state with retained backup ID.

There must be no ordinary failure that silently leaves a half-written client config.

---

# Workstream 11 — Security qualification

Verify:

- connection token contains no API key;
- bootstrappers never print credential values;
- credential is not passed on argv when avoidable;
- helper subprocess environment is limited to what client verification needs;
- downloaded executable hash mismatch blocks execution;
- bootstrap/profile strings are never evaluated as shell/PowerShell code;
- profile cannot request arbitrary write paths/commands;
- backup files are user-private;
- logs/JSON output do not contain backup contents or unrelated config contents;
- redirect behavior for downloads does not bypass the approved GitHub release host/integrity check;
- server profile endpoint requires normal EggPool auth.

No code-signing infrastructure is required solely for this feature unless the repository already adopts it. SHA-256 against the validated release manifest is the minimum integrity contract.

---

# Workstream 12 — Documentation and operator flow

Update after real qualification:

- `README.md` — concise remote-client setup entry point;
- `docs/agent-configuration.md` — local `configsetup` vs remote `configremote`/`eggpool-connect`;
- `docs/deployment.md` and `docs/raspberry-pi.md` — SBC workgroup topology;
- `docs/releasing.md` — helper release assets and target distinction;
- `architecture/deep-dive-integrations.md` — server/shared/helper ownership;
- `architecture/deep-dive-deployment.md` — helper release/bootstrap pipeline;
- `docs/filesystem-layout.md` — local helper state/backups.

Document explicitly:

- profile is shareable because it contains no credential;
- each desktop still needs an authorized EggPool key through its environment/prompt;
- `configremote` does not install clients;
- backup/restore commands;
- Windows helper support does not imply Windows EggPool server support;
- advertised URL must be reachable from the desktop.

---

# Required validation commands

Run repository invariants:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
```

Run Python release/tooling validators because this plan changes release infrastructure:

```bash
uv sync --dev
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_runtime_package_boundary.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
```

Add focused helper/bootstrap qualification commands to the repository documentation as they are implemented.

---

# Acceptance criteria

1. `eggpool-connect` is published as an integrity-verified release asset for the supported desktop matrix, including Windows x86_64.
2. Helper artifact identity is distinct from EggPool proxy/runtime artifact identity in release manifests and validators.
3. Publishing a Windows helper does not change/document Windows as a supported proxy target.
4. POSIX and PowerShell bootstraps are version-pinned, download exact release assets, verify SHA-256/manifest, and never execute unverified bytes.
5. Bootstrappers contain no client mutation logic beyond invoking the native helper.
6. `eggpool configremote` prints only commands that refer to real immutable release artifacts for its version.
7. Helper cross-platform deterministic tests cover decode, config mutation, backup, rollback, restore, remove, and failure injection.
8. Real current Codex qualification passes install/no-op/sync/drift/remove/restore plus current model/config diagnostics in isolated homes.
9. Real current OpenCode qualification passes for every claimed V1/V2 adapter variant, including JSONC preservation.
10. Network/auth/client/filesystem failure matrix demonstrates pre-mutation refusal or verified rollback; no half-written ordinary failure is accepted.
11. Release/tooling validation and the full Rust workspace suite are green.
12. No credential appears in profile tokens, release/bootstrap commands, logs, manifests, or generated client configs.
13. Helper release additions do not materially regress SBC proxy footprint by accidentally linking desktop-only dependencies into the server binary; before/after sizes and feature graphs are recorded.
14. Operator docs describe a complete headless SBC + multiple desktop clients workflow and backup/restore procedure.
15. A closure plan records exact tested EggPool/client/helper versions, OS matrix, skipped dimensions, and evidence without rewriting Plans 209–214.

## Handoff note

Keep release changes additive and typed. The project already has a deliberately strict “build exact bytes once, validate, then publish” supply-chain model; extend that model to the helper instead of creating a second download/update mechanism. The final user experience should be one copy/paste command, but the safety underneath must remain explicit: immutable artifact, verified hash, secret-free profile, transactional local mutation, and automatic rollback.
