# K014 — PyPI Trusted Publisher Configuration and Recovery Completion

Status: blocked pending maintainer PyPI account access

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: infrastructure/capability

Hard dependency: K013 workflow correction is implemented, but its recovery run
stopped at the external PyPI Trusted Publishing exchange.

## Objective

Configure the PyPI Trusted Publisher for the `eggstack/eggpool` production
workflow, then resume the exact K011 release bundle without rebuilding or
mutating the already-public GitHub release. This plan is the external recovery
needed before K011 can be accepted and K012 can become actionable.

## External configuration

The PyPI project owner must configure a Trusted Publisher matching the claims
emitted by `.github/workflows/release.yml`:

- owner: `eggstack`;
- repository: `eggpool`;
- workflow: `.github/workflows/release.yml`;
- environment: `pypi`;
- production ref: `main`.

The configuration must be completed by an authorized PyPI project maintainer;
credentials and long-lived upload tokens are out of scope.

## Recovery procedure

1. Re-run `pypi-resume` with the failed K011 source run ID
   `34570717210`.
2. Require the workflow to validate the immutable tag, source commit,
   release manifest, exact three-wheel target set, and `SHA256SUMS` before
   invoking PyPI Trusted Publishing.
3. Verify public PyPI metadata, wheel hashes, attestations, and the existing
   GitHub release against the frozen manifest.
4. Only after those checks pass, update the catalog/changelog/docs to the
   published state and complete K011's public fresh-install and rollback
   evidence.

## Acceptance criteria

- the configured publisher accepts the workflow's exact OIDC claims;
- the exact K011 wheels are public on PyPI with no rebuild or replacement;
- PyPI and GitHub public hashes match the frozen manifest;
- K011's supported fresh-install and Python -> Rust -> Python -> Rust rollback
  drill completes on preserved state;
- K011 and K013 receive truthful closure records, and K012 is unblocked only
  after K011 is accepted.

## Stop conditions

Stop without retrying upload when the publisher claims, source run, tag,
manifest, wheel set, or hashes do not match. Do not create a second release,
delete the existing GitHub release, reuse a filename, or introduce a long-lived
PyPI credential.

## Closure evidence

Record the PyPI publisher configuration result, recovery run, public metadata,
hash verification, install/rollback evidence, and registry transition in
`migration-rs/closure/cutover/014-status.md`. Until that evidence exists, K011,
K013, and K014 remain incomplete/blocked.
