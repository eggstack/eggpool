# ADR-0004 — PyPI remains the canonical package channel; Rust ships as binary wheels

Status: accepted for M11 planning

Date: 2026-09-10

Decision scope: M11 Rust cutover and the M12 rollback window

## Context

EggPool's public package identity is already `eggpool` on PyPI. Existing users install it primarily through `pipx` or `uv tool`, and the current quick installer ultimately installs the PyPI project. Historical releases through at least 0.5.6 are pure-Python `py3-none-any` wheels. The repository and GitHub release history have subsequently advanced beyond the newest release currently visible on PyPI.

M10 has qualified the Rust runtime on Linux x86_64, Linux aarch64, and macOS arm64 development/runtime paths. M11 must make Rust canonical without forcing existing users to abandon the `pip install eggpool`/`pipx install eggpool`/`uv tool install eggpool` package identity or lose the ability to return to a compatible Python release.

The M9/O008 updater already supports verified raw GitHub executable replacement. That mechanism is correct for a standalone managed executable, but it must not overwrite an executable installed from a wheel: doing so would make the package manager's installed-file metadata and the actual executable diverge.

Current packaging standards provide a simpler cutover boundary. Maturin supports `bin` bindings that package a Rust binary into a wheel's scripts area, where installers expose it directly on `PATH`. PyPI/PyPA installers already understand platform-tagged wheels and exact version requirements. The same project may therefore have historical pure-Python wheels and later platform-specific Rust-binary wheels under one monotonically increasing version history.

## Decision

1. The canonical public package name remains **`eggpool`** on PyPI.
2. The first Rust-backed PyPI release and later M11 releases use **Maturin `bin` wheels**. The wheel installs the native Rust `eggpool` executable directly as a script/binary. It is not a PyO3 extension and does not run a Python wrapper in normal execution.
3. Rust-backed PyPI releases are **wheel-only** for supported targets during M11. Do not publish an sdist that invites unsupported hosts to compile the production binary from source with an arbitrary Rust toolchain.
4. The root Hatchling `pyproject.toml` and Python source remain available as the final Python oracle/reference until M12. M11 uses a dedicated Rust-wheel packaging manifest rather than replacing the historical Python build definition in place.
5. Supported Rust wheel targets are exactly the target classes accepted by M10 unless a later qualification plan expands them: Linux x86_64, Linux aarch64, and macOS arm64 development/runtime. Windows and other unqualified targets do not receive a wheel and must fail with a normal "no matching distribution"/unsupported-target result rather than silently falling back to an unqualified build.
6. The Rust wheel retains `Requires-Python >=3.11` through M11 unless a later ADR supersedes this decision. The native process does not require Python at runtime, but retaining the Python compatibility floor keeps package-managed environments capable of downgrading to the final supported Python-era releases during the rollback window. M12 may reconsider this metadata after Python retirement.
7. Package-manager-owned installs remain package-manager-owned. `eggpool update [VERSION]` detects trusted install provenance and delegates an exact `eggpool==VERSION` transition to the owning manager/environment for `uv tool`, `pipx`, or ordinary pip/venv installs. It must not overwrite a wheel-managed executable in place.
8. The raw O008 GitHub self-updater remains available only for a clearly detected standalone Rust-binary install. It continues to require verified target-specific release assets and rollback-safe replacement.
9. Exact-version transitions are **direction-neutral**: a target may be newer, older, Python-backed, or Rust-backed. A transition succeeds only when the target is in the frozen installable release catalog and is compatible with the current platform/package-manager environment.
10. M11 freezes an **installable release catalog** that maps official EggPool versions to distribution era, immutable release identity, available installation artifacts, supported targets/Python constraints, and rollback status. M11 must not claim arbitrary historical versions that cannot actually be reproduced or installed.
11. Because PyPI currently lacks some GitHub-tagged releases after the newest historical PyPI wheel, M11 must explicitly resolve those gaps before claiming seamless exact switching. Preferred order: reproducibly build and publish missing historical Python wheels to the existing PyPI release when PyPI permits adding that new filename and the immutable tag can be reproduced; otherwise record an immutable commit/archive package-manager fallback for that version. Never use a mutable branch or silently retarget a historical version.
12. PyPI publication uses Trusted Publishing/OIDC from a dedicated release workflow and a protected GitHub `pypi` environment. Release artifacts are built once per target, checked before upload, and published with provenance/attestation support supplied by the standard PyPI publishing path. GitHub raw binaries may be attached in parallel for standalone installations.
13. M11 changes the default public installation/update authority but does **not** remove Python source, historical package metadata, or oracle tests. Python retirement remains M12.

## Consequences

### Positive

- Existing `pip`, `pipx`, and `uv tool` users keep the same package name and familiar installation workflow.
- A PyPI wheel can carry a native Rust executable without adding PyO3 or a Python runtime call to EggPool's steady-state process.
- Exact package requirements naturally support upgrade and downgrade across the Python/Rust boundary.
- Package metadata stays coherent because package managers replace their own wheel contents rather than EggPool mutating a managed script behind their back.
- Unsupported targets fail before installation instead of compiling an unqualified runtime from source.
- Standalone raw-binary users retain O008's verified atomic updater.

### Costs

- Rust-backed releases require one wheel per supported OS/architecture rather than one universal Python wheel.
- Package-managed downgrade to Python-era releases still needs a usable Python >=3.11 interpreter in that tool environment.
- The repository must retain two packaging definitions until M12: the Python oracle package and the Rust-wheel publication package.
- Missing historical PyPI releases need an explicit compatibility/catalog decision before M11 can promise exact switching for them.

## Rejected alternatives

### Replace PyPI with GitHub raw binaries

Rejected. It breaks the established package identity and makes clean pipx/uv/pip upgrade/downgrade substantially harder.

### Publish a tiny Python wrapper that downloads the Rust binary at first run

Rejected. It introduces a second installation/update protocol, runtime network behavior, cache/security ownership, and a Python execution dependency with no benefit over a binary wheel.

### Package Rust as a PyO3 extension and retain a Python CLI wrapper

Rejected. EggPool is a standalone server/CLI, not a Python library API. PyO3 would add an unnecessary runtime boundary and contradict the pure-Rust process goal.

### Let O008 overwrite every `eggpool` executable regardless of provenance

Rejected. Directly replacing a wheel-managed script/binary violates package-manager ownership and can leave `RECORD`, uninstall, repair, and later version resolution inconsistent.

### Publish an sdist for Rust-backed releases as an unsupported-target fallback

Rejected for M11. A source fallback would make install success depend on a local Rust toolchain/system libraries on targets M10 did not qualify. Source builds remain developer workflows, not canonical public distribution.

### Delete Python packaging at cutover

Rejected. M12 owns Python retirement; M11 requires a controlled rollback window.

## Verification requirement

M11 closure must prove at least these paths on the supported target classes:

- fresh PyPI-style Rust wheel install;
- Python wheel -> Rust wheel upgrade without config/DB relocation;
- Rust wheel -> historical Python wheel downgrade;
- Python -> Rust again after downgrade;
- exact current-version no-op/repair behavior;
- uv-tool, pipx, and ordinary virtualenv/pip provenance handling as applicable;
- standalone Rust raw-binary update remains isolated from package-managed paths;
- deployed service restart/health and rollback around a cross-era transition;
- unsupported platform/no-wheel failure is explicit and non-destructive.

Only accepted M11 closure may make the Rust distribution canonical. Python source remains until M12.