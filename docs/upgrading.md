# Upgrade, rollback, and installation ownership

EggPool's current package is a native Rust executable distributed as a
platform-specific wheel. The package channel is PyPI; the supported wheel
targets are Linux x86_64, Linux aarch64, and macOS arm64. Windows and other
unqualified targets have no Rust wheel and are unsupported.

The wheel keeps `Requires-Python >=3.11` because package managers need a
compatible environment when an operator explicitly selects a supported
historical Python-era release. The running EggPool process does not import or
execute the package-management interpreter.

## Fresh installs

Use the public installer or choose a package manager directly:

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
uv tool install eggpool
pipx install eggpool
```

The installer preserves an existing `~/.config/eggpool/config.toml`, database,
and `.env`. It never clones the repository for a normal install. Check the
selected authority and current version with:

```bash
eggpool install-provenance
eggpool version
eggpool runtime-status
```

## Existing Python-era installs

No config or database relocation is needed. Run the ordinary update or the
public installer upgrade path:

```bash
eggpool update
eggpool check-config
eggpool runtime-status
```

The owning package manager replaces the Python wheel with the Rust wheel and
the service is restarted after the new executable passes its self-check. The
systemd unit keeps its manager-exposed executable path; it does not source a
user shell startup file as root.

## Exact upgrades and rollback

`eggpool update VERSION` accepts `VERSION` or `vVERSION` and resolves one exact
catalogued target. The same command intentionally supports exact upgrades and
downgrades:

```bash
eggpool update 0.8.0
eggpool update 0.7.4
```

The Python-era rollback window is limited to the catalogued schema-compatible
versions (currently 0.6.7 through 0.7.4). The owning environment must provide
Python 3.11 or newer for a Python-era target. An unsupported, unavailable, or
DB/config-incompatible target is rejected before mutation. Package-manager
provenance is retained: uv uses uv, pipx uses pipx, and a pip/venv install uses
that environment's Python interpreter.

The update never resets, relocates, or downgrades the database. Config, DB,
and `.env` paths remain in place. If a post-install self-check or service
restart fails, the updater attempts the previous exact target and reports the
manual command when recovery also fails. Follow that emitted command, for
example:

```bash
eggpool update 0.8.0
```

After a successful rollback, return to Rust with either the latest compatible
release or an exact cutover request:

```bash
eggpool update
eggpool update 0.8.0
```

## Install-aware update authority

| Installation | `eggpool update` authority |
|---|---|
| uv-managed wheel | the owning uv tool environment |
| pipx-managed wheel | the owning pipx environment |
| pip/venv wheel | that environment's Python interpreter and pip |
| standalone Rust binary | verified GitHub raw release asset |
| source checkout | explicit developer source workflow |
| ambiguous ownership | fail closed with recovery guidance |

The command refuses to guess when the executable and package metadata do not
identify one owner. Do not delete the database to resolve an ownership or
rollback error; install through the intended owner or use the manual command
printed by the updater.

## Standalone Rust binaries

Standalone Rust installs are distinct from package-managed PyPI installs. Their
latest/exact Rust update uses the verified raw executable and SHA-256 digest
from the GitHub release authority. They cannot downgrade directly to a
Python-era package. To adopt one into canonical wheel management, use:

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
```

The installer requires explicit standalone adoption when it detects the
existing binary, preserves a rollback copy, and restores it if package
installation or self-check fails.

## Source checkouts and unsupported targets

A source checkout is a developer/reference workflow, not a normal install.
Build and run the current Rust application with explicit Cargo paths:

```bash
cargo build --manifest-path rust/Cargo.toml
rust/target/debug/eggpool --help
rust/target/debug/eggpool --config ./config.toml check-config
```

Use the checkout's `packaging/pypi/pyproject.toml` only when building a local
Rust wheel for qualification. The repository root `pyproject.toml` contains
tooling configuration only; it is not an EggPool package and is never a
runtime fallback. Historical Python source is recoverable from the immutable
reference commit recorded in `migration-rs/fixtures/retirement/`, not from a
current source package.

The Rust wheel's `Requires-Python >=3.11` is a package-manager compatibility
floor for explicit historical transitions, not a runtime interpreter
dependency. On Windows or another unsupported target, installation fails
before any existing installation is changed; no source-build fallback exists.

On Windows or another unsupported target, no Rust wheel is selected and no
source-build fallback is allowed. Existing installations are not mutated by a
failed compatibility precheck.
