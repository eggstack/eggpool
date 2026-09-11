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
