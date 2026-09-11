# K014 Closure — PyPI Trusted Publisher Configuration and Recovery

Status: **accepted/closed 2026-09-11**

Plan: [K014 — PyPI Trusted Publisher configuration and recovery completion](../../implementation/cutover/014-pypi-trusted-publisher-configuration-and-recovery-completion.md)

## External configuration

The production PyPI Trusted Publisher was configured manually for the exact
GitHub Actions claims emitted by the release workflow:

| Claim | Value |
|---|---|
| Owner | `eggstack` |
| Repository | `eggpool` |
| Workflow | `.github/workflows/release.yml` |
| Environment | `pypi` |
| Production ref | `main` |

A distinct TestPyPI Trusted Publisher was also configured for the same owner,
repository, and workflow with environment `testpypi`. The recovery below used
the production `pypi` environment; no TestPyPI rerun was required to complete
the already-failed production bundle.

No PyPI API token or repository secret was added.

## Exact-bundle recovery

Recovery run `34597248849` was dispatched with `destination=pypi-resume` and
`source_run_id=34570717210`. It passed the immutable tag/source identity gate,
downloaded the exact failed-run `k008-release-bundle`, normalized its layout,
validated its manifest and `SHA256SUMS`, and published the exact three wheels
through `pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33`.
The publish log records successful uploads and digital attestation generation.

The recovery did not rebuild the release, attach replacement GitHub assets, or
use a long-lived credential. The existing public GitHub release `v0.8.0` is
unchanged.

## Public verification

The public PyPI release contains exactly these non-yanked wheels:

| Target | Filename | Size | SHA-256 |
|---|---|---:|---|
| Linux x86_64 | `eggpool-0.8.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl` | 11,213,582 | `cce9b86347664484078a858cd9676984a02bb86e9485fea5207e98b60a3d40df` |
| Linux aarch64 | `eggpool-0.8.0-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl` | 10,624,177 | `534a7cd62a8dc7110ba5d64a0eb2a9c11f543d8d871e8a965200fbe4329f7df6` |
| macOS arm64 | `eggpool-0.8.0-py3-none-macosx_11_0_arm64.whl` | 11,554,892 | `375c4ccac9299536adc4e27a16578a649993597ba20d11c915bc0dd6c2de9835` |

`verify_published_release.py` returned pass for the public PyPI JSON, the
public GitHub release API response, and the downloaded public release
manifest. The verifier result was `raw_assets=3`, `wheels=3`, `version=0.8.0`.
The manifest SHA-256 was
`7b967a29f0416034fd5c0272c717d5607c124c8611a43be4567d0cc4986f99a7`.

Hosted public qualification run `34598462704` passed on all supported target
classes. Linux x86_64 passed `uv-tool`, `pipx`, and isolated `pip`; Linux
aarch64 and macOS arm64 passed the documented `uv-tool` path. The run also
passed native Rust wheel version/help/config/health/dashboard checks and the
preserved-state `0.7.4 -> 0.8.0 -> 0.7.4 -> 0.8.0` cycle, with SQLite integrity
`ok` and migration maximum `54` at every manager result.

## Closure decision

K014's external configuration and exact-bundle recovery objective is complete.
K011 and K013 receive append-only acceptance addenda, and K012 receives the
M11 aggregate acceptance addendum. No future plan is automatically promoted;
M12 is eligible for a separate planning review after K012 acceptance.
