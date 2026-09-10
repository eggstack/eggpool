# K003 — Supported Wheel and Raw Release Artifact Matrix

Status: queued; blocked on accepted K002

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: infrastructure/invariant

Hard dependency: accepted K002.

## Objective

Produce and qualify the complete target-specific artifact set that M11 will eventually publish from one source revision:

- PyPI-compatible Rust binary wheels for all M10-supported public targets;
- matching raw Rust executables for standalone installations/O008;
- one deterministic release manifest linking version, source commit, target identity, wheel identity and raw executable identity.

K003 builds and verifies release-candidate artifacts but does not upload them to production PyPI or create the canonical public release.

## Target matrix

Use the K001-frozen target table. Starting authority inherited from M10:

| Product target | Rust target | Wheel requirement | Raw asset requirement |
|---|---|---|---|
| Linux x86_64 | `x86_64-unknown-linux-gnu` | manylinux-compatible | required |
| Linux aarch64 | `aarch64-unknown-linux-gnu` | manylinux-compatible | required |
| macOS arm64 | `aarch64-apple-darwin` | macOS platform wheel | required |
| Windows | none | prohibited | prohibited |
| other Unix | none | prohibited unless separately qualified | prohibited unless separately qualified |

Linux must use an explicit manylinux baseline no newer than the compatibility floor accepted in M10. `manylinux2014`/glibc 2.17 is the default planning expectation; K003 must verify the actual generated tag and binary dependency floor rather than assuming it.

## Build strategy

Use pinned Maturin/maturin-action release tooling selected by K002. Prefer native or official/canonical cross-target build containers rather than custom infrastructure.

Requirements:

- Cargo `--locked`;
- release profile;
- explicit target triple;
- explicit Linux manylinux setting;
- PyPI compatibility validation;
- no hidden target feature differences unless required and recorded;
- reproducible source commit identity embedded in the release manifest, not necessarily the executable;
- no provider credentials in build jobs.

Do not add a large always-on PR matrix. This is a release/qualification matrix.

## Raw GitHub asset contract

Preserve O008's established naming unless a corrective plan explicitly updates both producer and consumer:

```text
eggpool-{version}-{os}-{arch}
```

where `os`/`arch` match Rust runtime identity (`linux`/`macos`, `x86_64`/`aarch64`) and the normalized version excludes an optional leading `v`.

Raw assets must be byte-identical to, or mechanically traceable to, the executable packaged inside the corresponding wheel. Prefer asserting the SHA-256 of the wheel-contained executable equals the raw asset hash when packaging does not transform the binary.

If Maturin changes modes/permissions without altering bytes, record and test that distinction.

## Release manifest

Create a bounded versioned manifest format, e.g. `m11-release-manifest.v1`, containing:

- release version;
- source commit SHA;
- Cargo lock hash;
- packaging manifest hash;
- target product ID;
- Rust target triple;
- wheel filename;
- wheel compatibility tags;
- wheel SHA-256 and size;
- embedded `eggpool` executable SHA-256 and size;
- raw asset filename/SHA-256/size;
- `Requires-Python`;
- build tool/Maturin/Rust versions;
- optional SBOM/attestation identifiers when available;
- qualification result.

Never include absolute runner paths, environment dumps, credentials, signing/OIDC tokens or temporary download URLs.

## Artifact validation

For every wheel:

1. inspect metadata and wheel tags;
2. extract/check the native executable;
3. verify executable format/architecture;
4. install into an isolated compatible environment;
5. run `eggpool version`, `help`, `check-config`;
6. run bounded foreground serve/health and local loopback inference;
7. exercise dashboard static/read path;
8. uninstall/reinstall once;
9. prove the raw executable and wheel payload correspond.

For every raw asset:

- executable mode after installation/staging is correct;
- `version` matches candidate;
- O008 staged self-check accepts it;
- direct binary startup works on its target;
- hash/size exactly match manifest.

## Unsupported-target behavior

Add deterministic validation that the release artifact set does not contain a Windows/other-unqualified wheel or raw asset.

Where practical, ask a package resolver/wheel tag checker to prove an incompatible host would not select the supported-target wheel. Do not publish a universal wheel or source fallback that masks absence of support.

## Linux portability check

K003 must inspect runtime linkage/dependencies for both Linux artifacts. Verify:

- generated wheel carries the expected manylinux tag;
- no accidental dependency on a newer glibc symbol than the accepted baseline;
- no unexpected dynamic library dependency unavailable in the manylinux contract;
- bundled/static dependencies comply with licensing and packaging policy.

Maturin's auditwheel compatibility check is necessary but not the only evidence; include an isolated container install/run smoke at the minimum supported baseline or an equivalent accepted Q005 environment.

## macOS deployment target

Freeze and validate the minimum macOS deployment target compatible with the M10-supported-development claim. Do not silently inherit a runner's latest SDK minimum. Record the target in K001/K003 artifact metadata and prove installation on the oldest environment available for qualification, or explicitly state the supported floor.

## Historical Python wheel artifacts

If K001 chooses PyPI backfill for missing Python-era releases, K003 may produce those historical wheels in separate isolated jobs from immutable historical commits/tags. They are **not** rebuilt from current source with an old version string.

For each backfill candidate:

- source commit/tag and version metadata must agree;
- wheel filename must not conflict with an existing PyPI file;
- build should reproduce historical packaging semantics as closely as feasible;
- install + `eggpool version/help/check-config` smoke must pass;
- hash/source identity enters the installable release catalog.

Do not publish them yet unless K001 explicitly sequences a reviewed backfill publication before K011; default is stage only.

## Failure semantics

Fail the plan if:

- any supported target artifact is missing;
- wheel tag is not PyPI-compatible;
- target architecture does not match filename/manifest;
- wheel/raw executable versions differ;
- wheel executable and raw asset cannot be correlated;
- unsupported target artifact is accidentally emitted;
- release candidate depends on mutable source state;
- an artifact exceeds PyPI limits without a reviewed decision;
- a build requires provider or user credentials.

## Dependencies

No Rust runtime dependency should be added. Build/release tooling changes are acceptable and must be pinned.

## Tests and verification

Add focused artifact-manifest/build validation tests. Run all K002 checks plus:

- Linux x86_64 candidate build/install/run;
- Linux aarch64 candidate build/install/run (native/QEMU/cross + accepted runtime environment as appropriate);
- macOS arm64 candidate build/install/run;
- raw O008 self-check for each available target artifact;
- manifest hash/content validation;
- unsupported-target absence test.

Record exact commands and environment class in closure.

## Closure evidence

Write `migration-rs/closure/cutover/003-status.md` containing:

- implementation commit(s);
- source/candidate version;
- complete target/artifact table;
- wheel/raw hashes and sizes;
- manylinux/macOS deployment compatibility evidence;
- historical backfill candidate table if applicable;
- manifest schema/hash;
- verification results;
- unresolved findings;
- registry transition.

## Acceptance criteria

K003 closes only when:

- every M11-supported target has a verified Rust wheel and matching raw executable candidate;
- generated platform tags match the frozen support matrix;
- no unsupported fallback artifact exists;
- one bounded release manifest ties artifacts to one source revision/version;
- local install/runtime smoke passes for every required target class;
- historical backfill candidates, if required, are reproducible and explicit;
- no unresolved high/medium artifact portability or integrity finding remains.

Accepted K003 promotes only K004.