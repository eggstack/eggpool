Vendored `tokio-rusqlite` 0.8.0 sources with `rusqlite` bumped to 0.41.1 so the bundled SQLite engine includes the fix for RUSTSEC-2026-0459 / CVE-2026-119375. Upstream `tokio-rusqlite` 0.8.0 on crates.io still requires `rusqlite` ^0.40.1; drop this directory and the `[patch.crates-io]` entry once a published `tokio-rusqlite` release tracks `rusqlite` 0.41.

License: MIT (see `LICENSE`).
