# Python reference packaging during M11

The Python implementation remains deliberately side by side with the Rust
runtime until M12.

- src/eggpool is the final Python behavioral reference for rollback and
  differential qualification through M11.
- Root Python tooling and tests remain available for oracle comparisons; they
  are not the public Rust runtime.
- The canonical production package is built from
  packaging/pypi/pyproject.toml, using Maturin's bin backend and the Rust
  Cargo manifest.
- Historical Python wheels are installed only through exact catalogued
  rollback/version tests or an explicitly selected reference environment.
- The root Hatchling project must not be built or uploaded under the Rust
  cutover version. The cutover release validators and release workflow guard
  this boundary.

M11 does not delete Python source, change the root reference version to
0.8.0, or publish a Python sdist as a Rust-era fallback. Python retirement and
any packaging simplification require a separately planned M12 handoff.
