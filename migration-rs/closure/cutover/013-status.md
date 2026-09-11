# K013 — Blocked Status Record

Status: **implementation complete; acceptance blocked pending PyPI Trusted Publisher configuration**

## Implemented correction

- Replaced the invalid PyPA publisher action reference with the verified
  `pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33`
  revision (v1.14.2).
- Added a maintainer-only `pypi-resume` dispatch requiring the exact failed
  source run ID.
- Added checkout/order handling, bundle-layout normalization, immutable source
  and tag checks, manifest/wheel-count validation, and `SHA256SUMS` checks.

## Recovery evidence

Recovery run `34574234702` passed identity, all three target builds, aggregate
validation, exact source-run provenance, and artifact/hash checks. The PyPI
publisher then rejected the GitHub OIDC exchange with `invalid-publisher`; no
PyPI file was uploaded. The claims identified by the failure are recorded in
K014 for exact Trusted Publisher configuration.

K013 therefore remains incomplete for its public-publication acceptance
criterion. K014 is the corrective follow-up; K011 remains unaccepted.

## Acceptance addendum — 2026-09-11

The historical publisher failure above is retained. After the PyPI Trusted
Publisher was configured for `eggstack/eggpool`, environment `pypi`, and
`.github/workflows/release.yml`, recovery run `34597248849` accepted the exact
failed-run bundle from source run `34570717210` and published the three
validated wheels through OIDC. No rebuild, token fallback, or mutable artifact
replacement was used.

The recovery path passed immutable tag/source checks, manifest and
`SHA256SUMS` validation, exact three-wheel validation, and PyPA attestation
generation. Public PyPI/GitHub verification passed against the frozen v0.8.0
manifest. The public-index install and rollback qualification was completed by
run `34598462704`; its Linux x86_64 uv/pipx/pip and Linux aarch64/macOS arm64
uv results are recorded in the K011/K012 acceptance addenda.

K013 is accepted and closed. Its corrected workflow remains the production
recovery path, and no unresolved high/medium release or provenance finding
remains.
