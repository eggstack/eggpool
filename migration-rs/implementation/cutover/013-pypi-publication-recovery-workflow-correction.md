# K013 — PyPI Publication Recovery Workflow Correction

Status: dependency-ready after failed K011 publication attempt

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: infrastructure/capability

Hard dependency: K011 production attempt recorded as incomplete; K008 release
workflow correction required before K011 can close.

## Objective

Correct the invalid immutable PyPA publisher reference discovered during the
K011 `v0.8.0` production run and provide a narrowly gated recovery path that
can publish the exact validated wheels from that failed tag run without
rebuilding or mutating the already-public GitHub raw assets.

## Scope

- replace the nonexistent publisher commit with the verified v1.14.2 commit;
- retain tag-only normal production publication and OIDC/attestation behavior;
- add a maintainer-only `workflow_dispatch` `pypi-resume` path requiring an
  explicit failed source run ID;
- verify source run, immutable tag/source commit, release manifest, exact wheel
  count/target shape, and `SHA256SUMS` before PyPI upload;
- add deterministic workflow-validator/test coverage and operator guidance;
- do not overwrite GitHub assets, delete the stable release, reuse a filename,
  or add a long-lived package credential.

## Acceptance criteria

- corrected workflow passes static supply-chain validation and all focused
  migration tests;
- recovery publication consumes only the failed K011 run's exact bundle;
- PyPI public metadata contains the three expected Rust wheels with manifest
  hashes and attestations, and the existing GitHub release remains exact;
- the K011 closure can proceed to public install and rollback evidence;
- no high/medium release-integrity or credential-boundary finding remains.

## Closure evidence

Record the corrected action revision, source run ID, exact public PyPI/GitHub
metadata, validation commands, and registry transition in
`migration-rs/closure/cutover/013-status.md`. K013 is a corrective pass and
does not rewrite K008's historical closure record.
