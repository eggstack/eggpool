# Deployment and Packaging Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/deployment-packaging/001-binary-first-quick-installer.md`

Source subsystem roadmap:

- `plans/subsystems/deployment-packaging-roadmap.md#milestone-1--binary-first-quick-installer-and-ownership-cleanup`

Repository baseline reviewed: `0560050437dc9cce80013a46d466167914f9d2d1`

Implementation commits or pull requests:

- `05600504` — Implement binary-first quick installer (installer authority dispatch, verified raw-binary path, owner delegation, deterministic qualification, docs/validators)

## 1. Executive finding

The capability is complete. Fresh current-native installs use the verified
GitHub raw executable without Python, uv, pipx, pip, Cargo, or a source
checkout. Existing owners are preserved via native `eggpool update`
delegation (standalone remains standalone; uv/pipx/pip retain their manager)
with legacy Python-era compatibility retained as an explicit bounded path.
Deterministic qualification covers the reported Linux/aarch64 stale-pipx
regression, checksum/size/version/collision/lock/rollback separation, and the
release sidecar contract. Public docs and static validators agree on the new
authority. No proxy runtime, release target, or publication channel was
removed. Disposition is `closed` with no medium-or-higher unresolved finding.
Physical Le Potato evidence is `not measured` (operational only; deterministic
host tests pass and the roadmap does not require physical evidence for this
milestone).

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| 1. Fresh supported current-native install succeeds without Python, uv, pipx, pip, Cargo, or source checkout | `qualify_quick_installer.py`: `fresh-linux-aarch64-no-python`, `fresh-linux-x86_64`, `fresh-macos-arm64`, `fresh-exact-rust` pass; `fresh-linux-aarch64-stale-pipx-ignored` proves pipx never invoked (`manager.log` absent); `scripts/install.sh` fresh-binary authority ignores manager discovery | pass | No manager/Python invoked on fresh native path by construction + fixtures |
| 2. Linux/aarch64 with stale/incompatible pipx cannot reproduce package-resolution failure | `fresh-linux-aarch64-stale-pipx-ignored`: fake pipx present, raw path selected, `manager.log` empty, `standalone-rust` committed, config seeded | pass | Regression case added per WP-A acceptance |
| 3. Fresh installs consume only the qualified raw artifact matching the detected target and verify SHA-256 before execution | `install.sh`: `select_raw_from_sidecar` (exactly-one match, platform suffix gate, wheel/connect rejected) + `verify_staged_candidate` (size/hash/chmod/self-check) + `hash_file_sha256` (`sha256sum`/`shasum -a 256`); fixtures: `checksum-mismatch` fails before mutation, `release-manifest-raw-contract` proves wheel/helper cannot satisfy selector | pass | Hash verified before any execution; `chmod` only after complete download |
| 4. Latest resolution is version-bound after checksum selection and cannot mix releases | `install.sh`: latest fetches `.../latest/download/SHA256SUMS`, parses version from selected filename, then fetches `.../download/v<version>/<file>` (pinned); `select_raw_from_sidecar` enforces exact-version filename/tag agreement for exact; `staged-wrong-version` + `wrong-target-filename` fixtures prove version mismatch fails | pass | Moving-latest pointer cannot cross versions by construction |
| 5. Exact Rust versions use the same raw authority | `fresh-exact-rust` (`--version v0.8.1` on linux-x86_64) passes via `release_sidecar_url_exact` + same `verify_staged_candidate`/atomic commit; `existing-uv-retained`/`existing-pipx-retained` prove exact `0.8.1` delegation | pass | Exact and latest share `install_fresh_raw_binary` / native updater paths |
| 6. Resulting fresh installs report `standalone-rust` and use native `eggpool update` thereafter | Fresh fixtures assert `install-provenance --shell` reports `standalone-rust`/`native true`/`0.8.1` and `version` matches; `existing-standalone-delegates` proves subsequent `eggpool update` delegation retains standalone | pass | Verified in `install_fresh_raw_binary` post-commit checks |
| 7. Existing uv/pipx/pip owners remain unchanged across update or repair | `existing-uv-retained`, `existing-pipx-retained`, `existing-pip-retained`, `existing-legacy-uv` (legacy manager path retains `uv-tool`), `manager-path-collision` guards silent replacement; post-delegation owner check refuses silent migration | pass | Delegation + `EXPECTED_OWNER` revalidation |
| 8. Existing standalone owners remain standalone unless explicit migration flag | `existing-standalone-delegates` (no flag → delegates, stays standalone, no `manager.log`); `standalone-adoption` (with `--adopt-standalone` → migrates to wheel with rollback); `standalone-historical-refusal` proves no silent downgrade | pass | `--adopt-standalone` meaning preserved as explicit advanced migration only |
| 9. Source-checkout, ambiguous, symlink/special-file, and unrelated collisions fail closed | `source-checkout-refusal`, `ambiguous-refusal`, `dest-symlink-collision`, `dest-special-collision`, `dest-unrelated-collision`, `manager-path-collision`, `unsupported-platform-refusal`, `root-refusal`, `unknown-argument-exit-2`, `uncatalogued-historical-refusal` all pass | pass | No mutation on refusal; config/database untouched |
| 10. Historical Python-era targets are never an implicit fallback | `historical-exact-compat` uses compatibility path only on explicit `--version 0.7.4`; `standalone-historical-refusal` rejects standalone→`0.7.4`; `historical-python-incompat` fails before mutation with explicit diagnostic; native download/checksum/GitHub failures never fall back (separate authorities, no fallback edge) | pass | Catalog gate + Python compat precheck preserved |
| 11. Config, database, `.env`, and deployment artifacts are bytewise preserved | `existing-config-preserved` (config + DB bytes unchanged across delegation); `historical-exact-compat` and failure fixtures assert no config mutation before commit; `seed_config_after_commit` runs only after verified commit | pass | Config seeded only when absent and only after commit |
| 12. Failed download, verification, self-check, replacement, or post-check leaves no unverified command active | `missing-SHA256SUMS`, `truncated-download`, `checksum-mismatch`, `staged-wrong-version`, `staged-not-native`, `oversized-artifact`, `target-race-refusal` all fail before final mutation or prove rollback; `dest-*` and `lock-contention` prove no partial commit; failed installs leave `config.toml` absent | pass | Private temp + same-filesystem atomic rename + traps; rollback copy for standalone repair |
| 13. Deterministic qualification covers old-pipx regression without real network/manager/user-state | `qualify_quick_installer.py` 42 cases pass via `file://` fixtures, fake `uname`/`curl`/`managers`, temp `HOME`/`XDG_*`; `test_installer.py` asserts 42 + regression names; no network, no real home, no public install | pass | See §4 for exact commands |
| 14. Public docs and static validators agree on binary-first authority | `validate_release_docs.py` passes (`docs_checked 7`, `published 0.8.1`); README/deployment/upgrading/raspberry-pi/releasing/rust-release-deployment/SKILL/architecture updated; validator now requires `GitHub raw` + `standalone-rust` + `SHA256SUMS` + origin guard and rejects package-manager-first wording | pass | See §9 |
| 15. No supported proxy target, API surface, runtime capability, or package publication removed | `release-manifest-raw-contract` asserts exactly three proxy raws (`linux-x86_64`, `linux-aarch64`, `macos-aarch64`); `check_release_catalog`, `validate_release_workflow`, `validate_runtime_package_boundary` pass; zero Rust diff; PyPI/crates.io publication untouched | pass | Raw/wheel/helper sets validated independently |

## 3. Production implementation evidence

Landed ownership/storage/protocol/runtime/operator changes (zero Rust diff;
all changes are installer/tooling/docs):

- `scripts/install.sh` (rewritten authority dispatch):
  - Explicit dispatch order: validate args/root/platform/origin guards →
    inspect existing `eggpool` → classify via `install-provenance --shell` /
    Python fallback → choose exactly one authority → transition → verify
    ownership/version → seed config only when absent → bounded next steps.
  - Authorities: `fresh-binary-latest`/`fresh-binary-exact` (verified raw),
    `fresh-historical-package` (catalogued Python-era with Python `>=3.11`
    precheck), `fresh-package-explicit` (`--package-manager` opt-in),
    `existing-native-update` (delegate to `eggpool update [VERSION]`),
    `existing-legacy-package` (owning-manager transition),
    `standalone-adoption` (explicit `--adopt-standalone` migration only),
    `source-local` (checkout developer flow), refuse otherwise.
  - Shell-native release resolution: platform normalization
    (`linux-x86_64`/`linux-aarch64`/`macos-arm64` → `linux-x86_64`,
    `linux-aarch64`, `macos-aarch64` raw suffixes), `SHA256SUMS` sidecar via
    `.../latest/download/SHA256SUMS` or `.../download/vX.Y.Z/SHA256SUMS`,
    exactly-one raw selection (wheel/`eggpool-connect`/manifest rejected),
    version extraction + exact agreement, 64-hex digest, 128 MiB bound
    (`MAX_ARTIFACT_BYTES=134217728`), `sha256sum`/`shasum -a 256`,
    `chmod` after complete, staged `version` + `install-provenance --shell`
    requiring `standalone-rust`/`native true`/version match, never execute
    after failure, HTTPS pinned in production, `file://`/loopback only with
    `EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_ORIGIN=1`.
  - Destination: canonical `${HOME}/.local/bin/eggpool`, safe `mkdir -p`,
    symlink/special-file refusal, private `mktemp -d` + same-filesystem
    staged file + atomic `mv`, `PATH` verification, temp/signal traps that
    never remove the installed command or user state, per-user
    `install.lock.d` (`mkdir` atomic) with revalidation before commit
    (DEST unchanged, no new `eggpool` on `PATH`).
  - Existing owners: native delegates to `eggpool update` (no shell
    duplication of locking/restart/rollback); `--force` repair retains owner
    (standalone verified-raw repair with rollback + service stop/restart;
    manager repair via owning reinstall); legacy Python-era uses bounded
    manager logic; `--adopt-standalone` preserved as explicit migration only.
  - Historical: catalog gate preserved, compatible Python checked before
    mutation, explicit diagnostic on failure, never a fallback after native
    failure.
- `scripts/qualify_quick_installer.py` (42 cases; see §4): file:// release
  fixtures, fake platform/manager/curl/python, existing-owner update
  simulation, negative/concurrency/ownership/config/origin coverage,
  `release-manifest-raw-contract` machine-checking the published manifest.
- `tests/tooling/test_installer.py`: asserts 42 cases + regression names.
- `scripts/validate_release_docs.py`: binary-first guards (README/deployment/
  upgrading require `GitHub raw` + `standalone-rust` + `binary-first`;
  raspberry-pi requires SBC no-Python note; installer requires `SHA256SUMS` +
  `standalone-rust` + origin guard + sidecar/identity/size contract).
- Docs: `README.md`, `docs/upgrading.md`, `docs/releasing.md`,
  `docs/rust-release-deployment.md`, `docs/deployment.md`,
  `docs/raspberry-pi.md`, `.opencode/skills/deployment/SKILL.md`,
  `architecture/deep-dive-deployment.md` updated to binary-first authority
  with explicit distinctions (curl raw default; uv/pipx explicit wheel;
  cargo source; owner retained; Python floor package/historical only;
  system/root production unchanged).

Planned but absent: none. No proxy/runtime request-path, target, Windows
proxy, PyPI/crates.io retirement, schema/config, `eggpool update`,
`eggpool-connect`, or root production ownership changes (all out of scope per
plan §5 and respected).

## 4. Verification executed

### Commands run

```bash
uv run python scripts/qualify_quick_installer.py
uv run pytest tests/tooling/test_installer.py -q
uv run pytest tests/tooling/test_release_artifacts.py tests/tooling/test_release_packaging.py tests/tooling/test_release_supply_chain.py tests/tooling/test_release_catalog.py tests/tooling/test_release_docs.py -q
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_release_identity.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
git diff --check
```

Rust: no `rust/` diff in this pass, so no `cargo fmt`/`clippy`/`test`/
`--no-default-features` matrix was required. Stated explicitly as justified
substitute (zero production Rust change; provenance/update seams used as-is).

### Results

Local (this host; CI is truth for hosted release gates, but no hosted run was
required for this tooling/docs pass):

- `qualify_quick_installer.py`: pass, 42 cases (`fresh-linux-aarch64-no-python`,
  `fresh-linux-aarch64-stale-pipx-ignored`, `fresh-linux-x86_64`,
  `fresh-macos-arm64`, `fresh-exact-rust`, `fresh-package-explicit`,
  `missing-SHA256SUMS`, `zero-matching-raw`, `multiple-matching-raw`,
  `malformed-digest`, `checksum-mismatch`, `oversized-artifact`,
  `truncated-download`, `wrong-target-filename`, `staged-wrong-version`,
  `staged-not-native`, `dest-symlink-collision`, `dest-special-collision`,
  `dest-unrelated-collision`, `lock-contention`, `target-race-refusal`,
  `existing-standalone-delegates`, `existing-uv-retained`,
  `existing-pipx-retained`, `existing-pip-retained`, `existing-legacy-uv`,
  `standalone-adoption`, `force-repair-standalone`,
  `existing-config-preserved`, `historical-exact-compat`,
  `historical-python-incompat`, `standalone-historical-refusal`,
  `source-checkout-local-candidate`, `release-manifest-raw-contract`,
  plus 8 legacy negative/misc including `ambiguous-refusal`,
  `manager-path-collision`, `root-refusal`, `unknown-argument-exit-2`,
  `uncatalogued-historical-refusal`, `unsupported-platform-refusal`,
  `source-checkout-refusal`, `nonprod-origin-optin`).
- `test_installer.py`: 1 passed (harness green, 42 asserted).
- Release focused: 30 passed, 1 skipped
  (`test_release_artifacts` + `test_release_packaging` +
  `test_release_supply_chain` + `test_release_catalog` + `test_release_docs`).
- Full tooling: 126 passed, 1 skipped.
- `check_release_catalog.py`: pass (58 releases; native `0.8.1` published;
  8 rollback-compatible).
- `validate_release_identity.py`: pass (`version 0.8.1`,
  `source_commit 0560050437dc9cce80013a46d466167914f9d2d1`).
- `validate_release_workflow.py`: pass (52 actions; targets
  `linux-x86_64`, `linux-aarch64`, `macos-arm64`; connect targets include
  Windows helper only).
- `validate_release_docs.py`: pass (`docs_checked 7`,
  `published 0.8.1`, `historical external artifacts`).
- `validate_runtime_package_boundary.py`: pass (`rust` runtime,
  `packaging/pypi/pyproject.toml`, historical `0.7.4`, native `0.8.1`).
- `ruff format --check`: pass (45 files formatted).
- `ruff check`: pass (all checks passed).
- `pyright scripts/`: 0 errors.
- `git diff --check`: clean.
- No concealed partial execution; all commands run to completion locally.

Fixture evidence highlights (all deterministic, disposable, no network):

- Stale pipx ignored: `fresh-linux-aarch64-stale-pipx-ignored` has fake `pipx`
  on `PATH` with `manager.log` empty after success.
- Checksum mismatch: `checksum-mismatch` fails with `checksum mismatch`, no
  `~/.local/bin/eggpool`, no `config.toml`.
- Moving-latest binding: latest sidecar parsed to `0.8.1`, asset fetched from
  version-pinned `.../download/v0.8.1/...` (not `latest`), enforced by
  `release_asset_url_pinned` + exact agreement + staged version check;
  `wrong-target-filename`/`staged-wrong-version` prove cross-version rejection.
- Collision: `dest-symlink-collision` (`symlink`), `dest-special-collision`
  (fifo), `dest-unrelated-collision` (regular file without `--force`),
  `manager-path-collision` (package path) all fail closed with bytes
  preserved.
- Ownership preservation: `existing-standalone-delegates` (`update.log`
  non-empty, `manager.log` empty), `existing-uv/pipx/pip-retained`
  (`update.log` contains `0.8.1`), `existing-legacy-uv` (manager `eggpool==0.8.0`).
- Rollback/cleanup: `standalone-adoption` retains
  `eggpool.eggpool-standalone-0.8.0.rollback`; failure fixtures leave no
  `DEST`/`config.toml`; `lock-contention` and `target-race-refusal` prove no
  silent overwrite; traps clean private temp without touching user state.
- Historical separation: `historical-exact-compat` invokes manager only
  (`eggpool==0.7.4`, no raw `DEST`); `historical-python-incompat` fails
  before mutation; `standalone-historical-refusal` rejects downgrade.

## 5. Invariant review

Per source-plan §4, each remains true:

- Supported proxy targets remain Linux x86_64, Linux aarch64, macOS arm64:
  `TARGET_CLASS` gate + `release-manifest-raw-contract` (exactly those three
  raws) + release validators pass.
- No Windows proxy/server implied: installer `unsupported platform` gate +
  docs/validators retain Windows-unsupported wording; helper Windows asset
  remains helper-only.
- Fresh native install never falls back to source compilation: no `git clone`
  in installer (validator enforced); fresh-binary path has no source edge;
  `source-local` only when run from checkout with no existing.
- Existing package-manager ownership remains unless explicit migration:
  delegation + `EXPECTED_OWNER` check + `standalone-adoption` requiring
  `--adopt-standalone`; fixtures prove retention.
- Existing standalone remains standalone on normal install/update:
  `existing-standalone-delegates` + post-update owner check.
- Source checkouts never overwritten: existing `source-checkout` refuses;
  `source-local` never resolves public package.
- Ambiguous provenance, unsafe links, collisions fail closed: fixtures above.
- Config/database/`.env`/backup/deployment preserved: `existing-config-preserved`
  + failure fixtures + seed-after-commit.
- Historical Python packages immutable: no rebuild/re-upload; catalog gate +
  `Requires-Python` wording preserved; `historical-exact-compat` uses
  `eggpool==` spec only.
- Exact historical transitions bounded by catalog/Python/DB-config: catalog
  gate + Python precheck + updater DB-config compatibility (unchanged Rust).
- No candidate executed before integrity/version verification:
  `verify_staged_candidate` order (hash → chmod → `version` →
  `install-provenance`) enforced; fixtures prove no execution after failure.
- Non-production origins require opt-in and do not persist:
  `EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_ORIGIN=1` guard + `nonprod-origin-optin`
  fixture; no persistence of override.
- Release publication still builds exact bytes once: workflow/manifest
  validators pass; no installer-specific alias or duplicate build added.

## 6. Failure and recovery review

- Download/checksum failure: no destination mutation (`missing-SHA256SUMS`,
  `truncated-download`, `checksum-mismatch`, `malformed-digest`,
  `zero/multiple-matching-raw`).
- Candidate self-check failure: no mutation (`staged-wrong-version`,
  `staged-not-native`, `oversized-artifact`).
- Fresh commit failure: no config mutation; temp removed; unrelated target
  untouched (`dest-*`, `target-race-refusal`).
- Existing standalone/package update: native updater lifecycle (locking,
  restart, rollback) via delegation; shell repair (standalone raw with
  rollback copy + service stop/restart) only on `--force` updater failure;
  legacy manager rollback preserved (`standalone-adoption` rollback file,
  `restore_standalone_fn`).
- Legacy manager transition: existing rollback behavior + ownership/version
  verification before config work.
- Signal/cancellation: `trap ... EXIT/INT/TERM/HUP` removes private temp +
  releases lock; never removes installed command or user state (code review +
  failure fixtures leaving `DEST` absent rather than half-written).
- Concurrent invocations: `install.lock.d` (`mkdir` atomic) fails the second
  with `already in progress`; revalidation before atomic commit refuses a
  target whose identity changed (`lock-contention`, `target-race-refusal`).
- Checksum ambiguity: fail closed, never first-match (`multiple-matching-raw`).
- Moving latest: version-bound pinned asset URL (see §2.4).

## 7. Migration and compatibility review

- Public one-shot command unchanged:
  `curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash`.
- Resulting ownership for fresh native changes from package-managed wheel to
  `standalone-rust` as authorized by ADR-0001; existing installations are not
  silently migrated (delegation + owner check + adoption flag).
- Direct package installs remain valid: `uv tool install eggpool`,
  `pipx install eggpool`, `cargo install eggpool` (README/docs + explicit
  `--package-manager` installer opt-in for fresh wheel).
- Historical exact targets compatibility-only: `0.6.7`–`0.7.4` catalog gate,
  Python `>=3.11` precheck, no DB/config migration.
- Config path precedence preserved; canonical config created only when
  absent and only after verified commit.
- No schema, API, wire, or protocol migration; zero Rust diff.

## 8. Security review

- Release URLs HTTPS in production (`require_production_https` +
  `curl --proto '=https'`); test origins require explicit opt-in and allow
  only `https://`, loopback `http://`, or `file://` (guard + fixture).
- Checksums validated before execution (64-hex syntax, exactly-one match,
  no path/whitespace ambiguity, 128 MiB bound, `sha256sum`/`shasum -a 256`,
  staged `version` + `install-provenance` native check).
- No credentials read or emitted (installer handles no secrets; reports are
  bounded `kind`/`version`/`native`/paths; `git diff` shows no secret
  handling).
- Unsafe target paths fail closed (symlink/special/collision + `same_path`
  `PATH` verification).
- Bounded output identifies target/version/ownership/command/config without
  leaking environment or credentials.
- Secret-free: closure and fixtures contain no credentials, prompts, raw
  bodies, or cache keys.

## 9. Documentation and operations

Updated (binary-first authority with explicit distinctions):

- `README.md`: raw binary one-shot default, uv/pipx explicit wheel (Python
  floor noted), cargo source, owner retained, Python floor package/historical
  only.
- `docs/upgrading.md`: fresh binary-first + explicit alternatives, owner
  delegation, standalone stays standalone, `--adopt-standalone` explicit
  migration syntax (`-- --adopt-standalone`).
- `docs/releasing.md`: `SHA256SUMS` shell discovery surface + selector
  contract (exactly-one raw, wheel/helper rejected, version-bound latest).
- `docs/rust-release-deployment.md`: raw binary normal, uv/pipx/cargo
  explicit.
- `docs/deployment.md`: binary-first personal install, delegation, `--force`
  same-owner repair, seed-after-commit.
- `docs/raspberry-pi.md`: curl default, pipx commented as alternative, SBC
  no-Python `>=3.11` note for current-native curl, uv setup commented as
  wheel-only.
- `.opencode/skills/deployment/SKILL.md`: binary-first + delegation +
  adoption + Python-floor scope.
- `architecture/deep-dive-deployment.md`: installer authority updated
  (`NATIVE_RELEASE_VERSION="0.8.0"` threshold retained; historical exact-only).
- Validators: `validate_release_docs.py` now machine-checks binary-first
  wording and installer raw/`SHA256SUMS`/origin/size/identity contract.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Physical Le Potato (or comparable Linux/aarch64 device) run `not measured` in this pass | Operational confidence only; deterministic host fixtures pass and roadmap states physical evidence is valuable but not required for M001 | Future operator may record a fresh-device run (platform/Python/pipx versions, artifact identity, version/provenance, config preservation, `check-config`) without code change; no corrective plan required |
| low | GitHub artifact attestations / immutable-release enforcement not added | Supply-chain hardening deferred per plan §5 out of scope; SHA-256 sidecar remains the authority | Separate hardening plan if desired; must not require `gh`/jq/Python in default installer |

No critical, high, or medium findings. No correctness or security gap remains.

## 11. Roadmap disposition

Milestone closed; no successor is eligible from this closure. The
deployment-packaging roadmap has no M002 defined; deferred hardening
(attestations, system/root production distribution, additional targets)
requires separate bounded plans and must not hitchhike on M001. Provider
M002 remains independently blocked on upstream Eggfetch and is unaffected.

## 12. Registry updates

Applied in the same commit as this closure:

- `plans/implementation/deployment-packaging/001-binary-first-quick-installer.md`:
  `active` → `closed`.
- `plans/subsystems/deployment-packaging-roadmap.md`: M001 `active` →
  `closed` with closure link `plans/closure/deployment-packaging/001-status.md`.
- `plans/registry.md`:
  - Active roadmaps: deployment-packaging `M001 active` → `M001 closed`.
  - Dependency-ready plans: remove M001 (no ready plans remain).
  - Recently closed: add deployment-packaging M001 (`closed`, implementation
    `05600504`, closure `plans/closure/deployment-packaging/001-status.md`).
  - Most-recently-closed line: provider M005 → deployment M001.
  - Unblock audit: record that M001 closure promotes no blocked work
    (provider M002 remains upstream-blocked; no deployment successor exists).
