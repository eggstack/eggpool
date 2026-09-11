# K009 Closure — Local Wheelhouse and TestPyPI Staged Release Rehearsal

Status: accepted; closed 2026-09-11

Plan: [K009 — Local wheelhouse and TestPyPI staged release rehearsal](../../implementation/cutover/009-wheelhouse-testpypi-staged-release-rehearsal.md)

## Decision

K009 is accepted and closed. The repository now has a deterministic, bounded
wheelhouse/index rehearsal coordinator, a guarded staged source hook for the
public installer, fail-closed artifact mutation checks, and machine-readable
closure evidence. K010 is promoted to dependency-ready. K011 remains the owner
of the first real public publication and public rollback proof.

The rehearsal did not publish to TestPyPI or PyPI. The repository has no
configured TestPyPI environment or publisher credential, so the allowed
workflow-level validation path was used and the limitation is recorded below.

## Implementation

- `7c196e6` — K009 wheelhouse/index coordinator, failure-injection evidence,
  installer staging hook, tests, and workflow integration.
- `e5fb9a5` — align the release workflow and Cargo MSRV with the Rust source;
  this corrected a real remote-runner failure caused by the stale 1.85.1 pin.
- The closure commit adds the uv-managed-interpreter pip fallback, explicit
  target-smoke control for cross-host evidence, and the K009 planning/status
  transition.

The coordinator is `scripts/qualification_cutover_rehearsal.py`; its report
schema is `k009-cutover-rehearsal.v1`. It delegates artifact inspection,
transition logic, installer behavior, and release-workflow validation to the
existing K003-K008 authorities.

## Candidate and remote workflow evidence

The exact staged candidate was version `0.8.0`, source commit
`e5fb9a52b8dcce1e02eddf406e7b0d29779738bf`, with release-manifest SHA-256
`3dcd16bc11674872a57cc42af04123684a4cd9602f0a40abac5cc9fd63c3cc4c`.

GitHub Actions run `34567007019`, manually dispatched with
`destination=validate`, passed validation, Linux aarch64, Linux x86_64,
macOS arm64, and exact aggregation. The TestPyPI and production publication
jobs were skipped. The preceding run `34566568666` exposed the stale Rust
1.85.1 release pin; the source uses let-chain syntax requiring the corrected
1.88.0 workflow/Cargo MSRV, and the corrected run passed.

## Exact artifact/hash matrix

The downloaded aggregate bundle, local loopback index, and manifest were
compared without rebuilding. Every downloaded hash matched the manifest and
`SHA256SUMS`.

| Target | Wheel | Wheel SHA-256 | Raw asset | Raw SHA-256 |
|---|---|---|---|---|
| Linux x86_64 | `eggpool-0.8.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl` | `aed054784a1619987457f332d18b354ab4c693f45a0b6dd6b70ad9d4edabd2d6` | `eggpool-0.8.0-linux-x86_64` | `8664513c1634824d089a9b852f9e24792ae7743536a4b03f0aeec684fe5c3487` |
| Linux aarch64 | `eggpool-0.8.0-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl` | `b9b1827f442f1abf3c84116aa1a955770755ee5a4ac324ab11c5dee7065c260e` | `eggpool-0.8.0-linux-aarch64` | `91eec9ab29556a30971ad94da7b42049c6c9f4b5fa1b041d1628527ec22dd9c8` |
| macOS arm64 | `eggpool-0.8.0-py3-none-macosx_11_0_arm64.whl` | `ca51e31dcd56096e6cb08eca869c69c3bf3053f0f9ae38dfe17e3bc16151dd28` | `eggpool-0.8.0-macos-aarch64` | `65deceda482f4dd9d8dc7e53fdab9846d0afe366a6966c6d6c2ff3d03df858db` |

The raw standalone smoke and wheel smoke passed in each corresponding remote
build job. The local K009 report additionally verified the macOS arm64 wheel
metadata and exact hash through a loopback PEP 503-style index. The committed
machine-readable form is [`009-run.json`](009-run.json).

## Package managers and installer

The existing K005 transition runner remains the authority for exact
Python `0.7.4` → Rust `0.8.0` → Python `0.7.4` → Rust `0.8.0` transitions;
its accepted native-arm64 matrix passed for uv tool, pipx, and isolated pip,
including rollback and config/database preservation. K007's accepted Linux
aarch64 evidence covers the deployed manager-owned service path. K009 added
the staged local-authority invocation and does not create a second transition
implementation.

On the current Darwin x86_64 host, K009 correctly marked installer and manager
execution as skipped because no selected artifact is runnable on that host;
it did not turn cross-architecture inspection into a false install pass. The
K006 installer tests and the coordinator's target-host path cover the actual
installer invocation. Staged use requires explicit
`EGGPOOL_INSTALL_FIND_LINKS` or `EGGPOOL_INSTALL_INDEX_URL` plus
`EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX=1`; the hook is not persisted in
generated user configuration.

## Negative and failure-injection evidence

The K009 report passed missing-artifact aggregation, corrupted-wheel hash
rejection, unsupported-extra rejection, and the unsupported `win_amd64`
resolver case. It also records bounded categories for index/download timeout,
partial manager install, target self-check, service restart, and production
publish activation, delegating those paths to K003, K006, K007, and K008.
The workflow validator passed the nine-job target/gate contract: rehearsal is
manual TestPyPI input only, while production requires the tag/repository and
environment gates.

## TestPyPI, backfill, and freshness disposition

No GitHub repository TestPyPI environment or publisher credential is
configured, and no upload was attempted. Run `34567007019` therefore used the
non-publishing `validate` mode; no production authority was touched. K011
must perform the first real public-index and OIDC proof and must use a
candidate identity that is safe for the destination.

The K001 historical rollback target remains the immutable PyPI
`eggpool-0.7.4-py3-none-any.whl` (SHA-256
`47b61c1c9db3ee9fa8945bebed89c7294cb867240b2204b1bfc39e443d1a9581`). No
historical backfill was needed for this rehearsal. Packaging and installer
orchestration did not change runtime-visible M10 behavior, so the deterministic
M10 freshness basis remains valid; full provider/SBC/rootful reruns remain
owned by their existing closures unless K011 changes those surfaces.

## Residual risk and registry transition

The only explicit residual is the unperformed live TestPyPI upload/index/OIDC
proof. It is an intentional K011 production-release prerequisite, not a hidden
K009 success claim. No unresolved high/medium release-mechanics, artifact
integrity, unsupported-target, manager-ownership, or data-loss finding remains.

K009 is accepted/closed. Per the promotion rule, K010 is now the sole
dependency-ready M11 plan; K011 remains queued behind K010, K012 remains
queued behind K011, and M12 remains blocked on accepted K012.
