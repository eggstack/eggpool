# K011 — Blocked Status Record

Status: **not accepted; blocked pending production PyPI Trusted Publisher configuration**

This append-only status record preserves the result of the first production
release attempt. K011 is not closed because its acceptance criteria require a
public PyPI release, public fresh installs, and a real package-managed
Python -> Rust -> Python -> Rust rollback drill.

## Evidence

- Production tag: `v0.8.0`, source commit
  `431cad4f46a2d4bbcbc8839c18b71f392c0616ca`.
- GitHub Actions production run: `34570717210`.
- The three supported Rust raw assets, release manifest, and `SHA256SUMS`
  were published to the stable GitHub release and verified against their
  recorded hashes.
- The PyPI publication step failed before upload because the original workflow
  referenced an invalid PyPA publisher action revision. No PyPI file was
  uploaded.
- The corrected exact-bundle recovery workflow was exercised through build,
  aggregate, provenance, and hash validation, but the publisher exchange then
  failed with PyPI `invalid-publisher`: no Trusted Publisher matched the
  workflow claims. No PyPI file was uploaded.
- Public PyPI metadata for `eggpool` version `0.8.0` remains absent.

## Planning transition

K013 records the workflow correction. K014 owns the remaining external PyPI
Trusted Publisher configuration and exact-bundle recovery. K012 remains queued
and blocked on accepted K011. No staged K009 evidence is relabeled as public
release evidence.
