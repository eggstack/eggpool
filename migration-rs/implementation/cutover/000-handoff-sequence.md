# M11 Rust Cutover Handoff Sequence

Status: active; K001 ready

Execute and accept in this order:

1. K001 — freeze cutover version, public release inventory, installable-version catalog, target matrix, package-manager authority, and rollback window (**ready**).
2. K002 — build the `eggpool` Rust binary as a Maturin `bin` PyPI wheel while preserving the root Python oracle package.
3. K003 — produce/qualify the Linux x86_64, Linux aarch64, and macOS arm64 wheel/raw artifact matrix and release manifest.
4. K004 — implement trusted install-provenance detection and package-manager-aware exact transition authority.
5. K005 — qualify Python -> Rust -> Python -> Rust exact transitions, failure rollback, concurrency, and data preservation.
6. K006 — cut the public quick installer over to package-channel Rust wheels and safely adopt existing installs.
7. K007 — qualify real deployed-service cross-era transition/recovery under disposable Linux/systemd.
8. K008 — establish pinned, least-privilege Trusted Publishing and GitHub release workflow with artifact provenance.
9. K009 — rehearse full release/install/update/rollback flow through local wheelhouse and TestPyPI/staging.
10. K010 — freeze public metadata/docs/default installer/update channel and final release candidate.
11. K011 — publish the first real Rust-backed PyPI/GitHub release and run immediate public rollback/re-upgrade drill.
12. K012 — aggregate public artifact/install/update/deploy/M10 evidence and close M11 if no high/medium blocker remains.

## Rules for every handoff

- `migration-rs/registry.md` is the only active implementation authority.
- The same PyPI project name `eggpool` is retained.
- Rust wheels use Maturin `bin`; do not add PyO3 simply for packaging.
- Keep the root Python package/reference usable through M11.
- Do not publish a Rust sdist as an unsupported-target fallback.
- Package-manager-owned installs are changed through their owner; O008 raw replacement is standalone-only.
- Exact requested versions are exact; do not silently choose a nearby compatible version.
- A downgrade may cross the Rust/Python boundary only when K001 marks the target installable and DB/config-compatible.
- No config/database reset, relocation, or destructive migration is allowed to make rollback easier.
- Failed post-install verification attempts exact rollback through the same owner and reports rollback failure explicitly.
- Build/test artifacts are immutable inputs to publish jobs; production publish does not rebuild.
- Production PyPI uses Trusted Publishing/OIDC; no long-lived PyPI token fallback.
- Supported release targets are inherited from M10 unless explicitly requalified.
- Production release is prohibited until K011.
- Python deletion/packaging retirement is prohibited until M12.

K001 is the sole dependency-ready handoff. K002-K012 are queued serially. K012 alone may close M11 and make M12 eligible for separate planning.