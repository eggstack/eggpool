# M12 Python Retirement Handoff Sequence

Status: implementation planning complete; P001 ready; P002-P006 blocked by direct predecessors

Execute and accept in this order:

1. P001 — freeze final Python reference identity, fixtures, path dispositions and Rust replacement coverage.
2. P002 — make current package/catalog/update/release authority explicitly Rust while preserving compatible historical exact-version targets.
3. P003 — remove the historical Python application source after migrations/assets are independently owned.
4. P004 — retire live Python-oracle/differential machinery and reduce Python to bounded tooling/fixtures.
5. P005 — consolidate installer, updater, release workflow, repository metadata and documentation around the Rust-only current tree.
6. P006 — run Rust-only artifact/state/cross-era qualification and close M12 if all gates pass.

## Rules for every handoff

- `migration-rs/registry.md` is the only active implementation authority.
- Do not delete an application/test/tool path before P001 names its disposition and surviving authority.
- Do not weaken Rust behavior or broaden normalization to make Python removal easier.
- Historical Python wheels are immutable external releases, not a current source fallback.
- `eggpool update` latest/default remains Rust-only; explicit compatible historical exact-version transitions remain supported.
- Do not mutate/yank/delete/rebuild historical PyPI files as part of M12.
- Do not change schema 54 or reset user state for retirement.
- Retained Python tooling cannot be required by the installed Rust service.
- A failed destructive or closure gate gets a new corrective P-plan; do not rewrite an earlier closure.

P001 is the sole dependency-ready handoff at registration time. P006 is the only plan that may close M12.
