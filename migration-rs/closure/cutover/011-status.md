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

## Acceptance addendum — 2026-09-11

The blocked attempt above is retained as historical evidence. K011's required
external prerequisite was subsequently completed and the frozen release was
recovered without rebuilding or replacing any public artifact.

- PyPI Trusted Publisher was configured for owner `eggstack`, repository
  `eggpool`, workflow `.github/workflows/release.yml`, environment `pypi`, and
  the production `main` ref.
- Recovery run `34597248849` used `pypi-resume` with source run
  `34570717210`. Identity, exact failed-run bundle provenance, manifest, wheel
  count, and `SHA256SUMS` validation passed; the PyPA publisher uploaded the
  exact three wheels and generated attestations.
- Public PyPI release `0.8.0` is visible with the three intended files only:
  Linux x86_64 (`cce9b86347664484078a858cd9676984a02bb86e9485fea5207e98b60a3d40df`,
  11,213,582 bytes), Linux aarch64
  (`534a7cd62a8dc7110ba5d64a0eb2a9c11f543d8d871e8a965200fbe4329f7df6`,
  10,624,177 bytes), and macOS arm64
  (`375c4ccac9299536adc4e27a16578a649993597ba20d11c915bc0dd6c2de9835`,
  11,554,892 bytes). Each is `Requires-Python >=3.11`, non-yanked, and has no
  Python application dependencies.
- The public GitHub `v0.8.0` raw assets and manifest remain unchanged and match
  the public manifest SHA-256 `7b967a29f0416034fd5c0272c717d5607c124c8611a43be4567d0cc4986f99a7`.

The public-index qualification run `34598462704` passed. It downloaded both
eras from public PyPI and completed the preserved-state Python `0.7.4` -> Rust
`0.8.0` -> Python `0.7.4` -> Rust `0.8.0` cycle: all three managers on Linux
x86_64 (`uv-tool`, `pipx`, `pip`), and the documented `uv-tool` path on Linux
aarch64 and macOS arm64. Each report preserved config, SQLite integrity, and
migration maximum 54. The same run passed native wheel `version`/`help`, config
validation, foreground health, and dashboard checks on all three target
classes.

The exact public verification commands were:

```text
uv run python scripts/verify_published_release.py /tmp/k012-public-final-manifest.json --pypi-json /tmp/k012-public-final-pypi.json --github-json /tmp/k012-public-final-github.json
gh run view 34597248849 --json status,conclusion,jobs,url
gh run view 34598462704 --json status,conclusion,jobs,url
```

The verifier returned `{"raw_assets": 3, "status": "pass", "version": "0.8.0", "wheels": 3}`. The release workflow and public qualification workflow completed successfully. The existing standalone raw-updater, unsupported-target, deployment, and Python-retention evidence remains source-fresh; no high/medium cutover, security, package-ownership, rollback, or data-loss finding remains open.

K011 is accepted and closed by this addendum. K013 and K014 have corresponding
append-only acceptance records. K012 is the M11 aggregate closure authority;
M12 is eligible for a separate planning review only after K012 acceptance.
