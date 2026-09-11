# M11 Rust Cutover Implementation Plans

Status: M11 closed; K001-K014 accepted/closed; M12 eligible for separate planning review

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Packaging authority: `migration-rs/adrs/ADR-0004-pypi-rust-wheel-and-install-authority.md`

These plans make the qualified Rust implementation the canonical public runtime while preserving the existing `eggpool` PyPI identity and a controlled Python-era rollback window. They do not remove Python source; M12 owns retirement.

## Sequence

1. [K001 — Cutover, package, and installable-version catalog contract freeze](001-cutover-package-and-version-catalog-contract-freeze.md) — **accepted/closed**.
2. [K002 — Rust PyPI binary-wheel packaging substrate](002-rust-pypi-binary-wheel-packaging-substrate.md) — **accepted/closed**.
3. [K003 — Supported wheel and raw release artifact matrix](003-supported-wheel-and-raw-artifact-matrix.md) — **accepted/closed**.
4. [K004 — Install provenance and package-manager transition engine](004-install-provenance-and-package-manager-transition-engine.md) — **accepted/closed**.
5. [K005 — Cross-era exact version transitions and rollback](005-cross-era-exact-version-transitions-and-rollback.md) — **accepted/closed**.
6. [K006 — Quick installer and existing-install adoption cutover](006-quick-installer-and-existing-install-adoption-cutover.md) — **accepted/closed**.
7. [K007 — Deployed-service cross-era transition and recovery](007-deployed-service-cross-era-transition-and-recovery.md) — **accepted/closed**.
8. [K008 — Trusted publishing, attestations, and release supply chain](008-trusted-publishing-attestations-and-release-supply-chain.md) — **accepted/closed**.
9. [K009 — Local wheelhouse and TestPyPI staged release rehearsal](009-wheelhouse-testpypi-staged-release-rehearsal.md) — **accepted/closed**.
10. [K010 — Public metadata, documentation, and release-candidate freeze](010-public-metadata-docs-and-release-candidate-freeze.md) — **accepted/closed**.
11. [K011 — First Rust-backed public release and immediate rollback drill](011-first-rust-public-release-and-rollback-drill.md) — **accepted/closed**.
12. [K013 — PyPI publication recovery workflow correction](013-pypi-publication-recovery-workflow-correction.md) — **accepted/closed**.
13. [K014 — PyPI Trusted Publisher configuration and recovery completion](014-pypi-trusted-publisher-configuration-and-recovery-completion.md) — **accepted/closed**.
14. [K012 — Aggregate M11 cutover qualification and closure](012-aggregate-m11-cutover-qualification-and-closure.md) — **accepted/closed**.

Only `migration-rs/registry.md` authorizes implementation. K007 is accepted/closed after real Linux evidence, K008-K010 are accepted/closed, and K011-K014 are accepted/closed with append-only recovery evidence. M11 is closed; M12 planning is now governed by the separate Python-retirement roadmap, and no Python removal is authorized by this cutover closure.

## Hard boundaries

- PyPI project name remains `eggpool`.
- Rust-backed PyPI releases use Maturin `bin` wheels, not PyO3 wrappers.
- M11 keeps `Requires-Python >=3.11` in the Rust wheel as a package-manager rollback compatibility floor; normal Rust runtime does not invoke Python.
- Rust M11 publication is wheel-only on qualified targets; no sdist fallback.
- Wheel-managed installs are changed by their manager, never by raw executable overwrite.
- Standalone Rust installs retain O008 verified GitHub raw-asset replacement.
- Exact version targets may upgrade or downgrade and may cross Python/Rust eras only when the K001 installable-release catalog says the target is compatible.
- Existing config/database/runtime/deployment paths are preserved.
- Linux x86_64, Linux aarch64, and macOS arm64 development/runtime are the only initial Rust-wheel targets unless a new qualification expands the matrix.
- Python source/oracle/fixtures remain through M11; no M12 removal work is authorized.
- Production PyPI publication occurs only in K011 after K001-K010 closure.

## Closure discipline

Each accepted plan writes `migration-rs/closure/cutover/<NNN>-status.md`. A failure or post-close finding creates a new K013+ corrective plan; historical closure records are not rewritten.

K012 alone may close M11 and make M12 eligible for a separate planning review.
