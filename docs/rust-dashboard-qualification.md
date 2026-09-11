# Rust dashboard qualification

Dashboard qualification uses an isolated, deterministic native Rust build. It
does not add a browser or screenshot dependency to the runtime.

Run the machine-checked qualification from the repository root:

```text
cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1
```

The command writes `migration-rs/closure/qualification/004-run.json` and
`004-run.md`. It checks every current dashboard page, public/private auth,
HTML escaping, navigation and form semantics, duplicate IDs, internal links,
static bytes/content types, theme inventory, and referenced assets. Use
`--skip-build` when `rust/target/debug/eggpool` already represents the source
under review.

## Browser review procedure

Use an existing local Chrome, Firefox, or equivalent browser against the
isolated server fixture. The `screenshots` section of `004-run.json` is the
coverage manifest: capture each page for `desktop` (`1440x900`) and `mobile`
(`390x844`) at the `default`, `Cyber Red`, `Catppuccin Latte`, and `Cyberpunk`
theme query values. Review the overview, models, runtime, cache, and long or
empty states first, then confirm the remaining pages are structurally stable.

Keep captures outside the repository when they are large. Record their
filenames and SHA-256 hashes in the Q004 closure record; never capture a live
personal installation or include credentials, request bodies, or private
provider data in evidence.

The review is bounded to desktop/mobile layout, overflow/wrapping, theme
contrast and consistency, navigation state, chart/table containment, and
obvious escaping or accessibility regressions. It is not a redesign pass.
