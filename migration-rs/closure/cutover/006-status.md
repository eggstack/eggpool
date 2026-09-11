# K006 Closure — Quick Installer and Existing-Install Adoption Cutover

Status: accepted; closed 2026-09-11

Plan: [K006 — Quick installer and existing-install adoption cutover](../../implementation/cutover/006-quick-installer-and-existing-install-adoption-cutover.md)

## Decision

K006 is accepted and closed. The public quick installer is now a package-channel
bootstrap for the native Rust wheel. It distinguishes package-manager ownership
from PATH order, adopts existing uv/pipx/pip installs, requires explicit
standalone-binary adoption, preserves operator state, and fails closed for
ambiguity, collisions, root personal installs, and failed transitions.

## Implementation

- `d8bddcb1f931bbdc0f2682213c300fe3e976d124` — package-channel installer,
  Rust provenance shell protocol, safe existing-install adoption, standalone
  rollback path, deterministic qualification harness, and deployment guidance.
- `aaad99b2f6e9f8f8ea67634a1fc2a54274e3b841` — require native provenance for
  the cutover version and all later exact versions, while retaining historical
  Python-era exact-target compatibility.

The Rust helper is a hidden `eggpool install-provenance --shell` command. It
emits bounded tab-separated fields only; the installer falls back to a narrow
stdlib metadata probe for older Python entry points. No new runtime dependency
or installer framework was introduced.

## Flow

Before:

```text
curl/install.sh
  -> clone or reuse $HOME/eggpool
  -> require system Python 3.11–3.14
  -> select pipx/uv and copy repository config
  -> run Python installer prompt
  -> expose eggpool
```

After:

```text
install.sh
  -> parse exact/latest/repair intent
  -> inspect existing eggpool provenance, if present
  -> adopt uv/pipx/pip owner, or require explicit standalone adoption
  -> otherwise choose uv tool, pipx, or documented HTTPS uv bootstrap
  -> install eggpool wheel through that manager
  -> verify manager ownership, native provenance, version, PATH, and CLI
  -> seed only a missing XDG config from Rust init-config
  -> restart a previously running standalone service and retain rollback copy
```

Source-checkout invocation is visibly separate: it installs
`packaging/pypi`, the pinned Maturin binary-wheel manifest, and validates an
exact requested version against Cargo. It never installs the repository-root
Python oracle package or silently resolves PyPI latest.

## Manager selection and adoption

| Current state | Selected authority | Behavior |
|---|---|---|
| No existing EggPool; uv available | uv tool | Install `eggpool` (or exact `eggpool==X.Y.Z`). |
| No existing EggPool; pipx available | pipx | Install through pipx. |
| No manager available | bootstrapped uv tool | Fetch only the documented HTTPS uv installer, then install through uv. |
| Existing uv-tool package | existing uv | Force/upgrade the same manager; no second environment. |
| Existing pipx package | existing pipx | Force/upgrade the same manager; no second environment. |
| Existing pip/venv package | owning Python + pip | Reinstall through that environment; no raw executable replacement. |
| Standalone Rust binary | uv or pipx after `--adopt-standalone` | Stage old binary under a deterministic rollback name and restore on failure. |
| Source checkout | local `packaging/pypi` manifest | Install the checkout candidate only. |
| Ambiguous or unrelated collision | none | Refuse with actionable recovery text. |

`--version X.Y.Z` and `--version vX.Y.Z` normalize to one exact target;
`--upgrade` means latest unless combined with an exact version, where the exact
version wins; `--force` requests repair/reinstallation under the selected
manager. Native provenance is required for `0.8.0` and later, while catalogued
Python-era exact targets remain eligible for rollback compatibility.

## State preservation and safety evidence

- Existing config bytes and a populated SQLite fixture remain unchanged in all
  three Python-adoption cases; the harness uses the canonical XDG config/data
  roots and does not create `$HOME/eggpool`.
- Existing `.env` and database paths are not touched by the installer. Config
  seeding calls the installed Rust `init-config` only when the resolved config
  file is absent.
- Standalone adoption stages the old executable outside PATH, verifies the new
  manager-owned command, and restores the old executable plus running state
  when package installation fails.
- A manager-bin collision, ambiguous provenance, and UID 0 personal invocation
  each refuse before unsafe mutation.
- Package-manager commands use fixed argument vectors and bounded subprocess
  output; no provider credentials are read or echoed.

## Deterministic qualification

`scripts/qualify_quick_installer.py` uses disposable HOME/XDG/PATH roots and
fake managers/interpreters; it never invokes a real package manager. The
12-case report returned `status: pass`:

```text
fresh uv, fresh pipx,
existing Python uv, existing Python pipx, existing Python pip,
standalone adoption, standalone rollback,
source-checkout local candidate,
ambiguous refusal, manager-path collision, root refusal, unknown-argument exit 2
```

## Real staged evidence

K005's accepted immutable native-arm64 staging matrix remains the real
package-manager evidence for the cross-era manager boundary: uv tool, pipx,
and isolated pip each completed Python `0.7.4` → Rust `0.8.0` → Python
`0.7.4` → Rust `0.8.0`, with config/DB preservation, migration max `54`, and
final CLI/metadata `0.8.0`. The recorded artifacts were the Python universal
wheel SHA-256 `47b61c1c9db3ee9fa8945bebed89c7294cb867240b2204b1bfc39e443d1a9581`
and the macOS arm64 Rust wheel SHA-256
`7dac275047b6af4ba99d90faf63af89729bf35c8c0aed2329ef42e9a79a51783`.

The current development host is macOS x86_64, outside K001's qualified Rust
wheel matrix. A real local transition attempt therefore returned the expected
bounded `unsupported_platform` result; it was not recorded as a false pass.
Fresh public-index staging remains owned by K009/K011.

## Verification

Passed:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1  # 42 passed
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
uv run python scripts/qualify_quick_installer.py                    # 12 pass
uv run pytest tests/migration_rs/test_k006_installer.py -q           # 1 passed
uv run pytest tests/migration_rs/ -q --tb=short --maxfail=1          # 192 passed, 3 skipped
uv run pytest tests/smoke/ -q --tb=short --maxfail=1                 # 14 passed
uv run python scripts/check_cutover_catalog.py                       # catalog valid
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
bash -n scripts/install.sh
git diff --check
```

The all-target Rust test invocation was attempted but abandoned after roughly
five minutes of unrelated shared-host Cargo cache contention; no test failure
was reported. The focused Rust library suite, clippy, migration suite, smoke
suite, installer qualification, and static gates are green.

## Complexity and findings

The legacy installer was 319 lines and the replacement is 498 lines. The
physical count increased because the replacement makes provenance fallback,
collision checks, standalone rollback, bounded verification, and source-path
separation explicit. Behavioral scope is narrower: repository cloning,
Python-runtime selection, prompt handling, and duplicated config-template
ownership were removed. The implementation remains one shell script with no
framework or runtime dependency; the added lines are safety state handling,
not a general installer abstraction.

No unresolved high/medium install, adoption, provenance, collision, rollback,
root-safety, or data-loss finding remains. Public README/release claims were
intentionally not flipped; K010/K011 own that public metadata and release
transition.

## Registry transition

K006 is accepted and closed. Per its explicit promotion rule, K007 is now the
sole dependency-ready M11 plan. K008–K012 remain queued behind their direct
predecessors, and M12 remains sequenced behind accepted K012. The plan and
registry status changes are recorded alongside this append-only closure.
