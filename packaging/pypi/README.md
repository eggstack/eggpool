# EggPool

EggPool is a lightweight proxy that aggregates multiple LLM provider accounts
behind an OpenAI Chat Completions-compatible endpoint.

This publication manifest builds the Rust `eggpool` executable as a
platform-specific Maturin binary wheel. The installed command is native code;
the package's Python requirement exists for package-manager compatibility
during the M12 historical-transition window and is not a runtime interpreter dependency.

Rust-backed current wheels are published only for the qualified Linux x86_64,
Linux aarch64, and macOS arm64 targets. The historical Python package remains
catalogued as immutable external PyPI history; the repository-root
`pyproject.toml` is development-only and is not a current publication
authority.

The package channel keeps `Requires-Python >=3.11` during the M12 transition:
that requirement belongs to the packaging environment, not to the native
runtime. Exact-version updates can return to a catalogued Python-era release
without moving the configuration or database. `latest` is Rust-only, and no
Rust download/install failure silently falls back to Python.
