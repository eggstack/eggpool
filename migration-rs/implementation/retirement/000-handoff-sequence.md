# M12 Python Retirement Handoff Sequence

Status: planning review complete; P001 ready for handoff; later plans blocked

Execute and accept in this order:

1. P001 — freeze the final Python reference identity, retained fixtures, and
   retain/archive/replace/remove inventory without deleting production paths.
2. P002 — accept ADR-0005 and retire Python production packaging, rollback
   authority, installer/update fallback, and release-path claims.
3. P003 — retire migration-only Python/Rust dual-run machinery while preserving
   selected evidence and Rust-native contract tests.
4. P004 — run Rust-only qualification and close M12 if all exit conditions pass.

## Rules for every handoff

- `migration-rs/registry.md` is the only active implementation authority.
- Do not delete `src/eggpool`, the root Python packaging definition, or
  migration-only scripts before P001 records their fate.
- Do not change Rust runtime behavior to compensate for missing Python tests;
  port or preserve the contract first.
- Do not call historical Python release artifacts a supported downgrade after
  P002 changes the public update contract.
- Do not remove database migrations, checksums, or retained fixtures needed to
  open existing Rust-owned state.
- A failed retirement gate creates a corrective P-plan; it does not rewrite a
  previous closure or silently broaden normalization.

P001 is the sole dependency-ready handoff after the M12 planning review.
