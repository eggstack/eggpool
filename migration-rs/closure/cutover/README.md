# M11 Rust Cutover Closure Records

This directory stores append-only closure evidence for the Rust public-distribution cutover, PyPI binary-wheel packaging, cross-era exact version transitions, release provenance, installer adoption, deployed rollback, and M11 aggregate acceptance.

Expected records:

- `001-status.md` — K001 cutover/package/version-catalog contract freeze
- `002-status.md` — K002 Rust PyPI binary-wheel packaging substrate
- `003-status.md` — K003 supported wheel/raw artifact matrix
- `004-status.md` — K004 install provenance/package-manager transition engine
- `005-status.md` — K005 cross-era exact transitions and rollback
- `006-status.md` — K006 quick installer/existing-install adoption
- `007-status.md` — K007 deployed-service cross-era transition/recovery
- `008-status.md` — K008 Trusted Publishing/release supply chain
- `009-status.md` — K009 local wheelhouse/TestPyPI rehearsal
- `010-status.md` — K010 public metadata/docs/release-candidate freeze
- `011-status.md` — K011 first public Rust-backed release/rollback drill
- `012-status.md` — K012 aggregate M11 cutover closure

Every record must identify implementation commit(s), release/artifact identity where applicable, exact verification commands/results, unresolved findings, and the registry transition it authorizes.

External release records must remain secret-free. Do not store PyPI/GitHub OIDC tokens, package-index credentials, provider credentials, private host identity, `.env` contents, or raw user config.

A failed closure or later defect receives a new K013+ corrective plan and closure record. Historical records are never rewritten to hide a blocked/failed release.

Only an accepted `012-status.md` may close M11 and make M12 eligible for a separate planning review. M11 closure does not itself authorize Python source removal.