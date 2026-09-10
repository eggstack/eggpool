# EggPool

EggPool is a lightweight proxy that aggregates multiple LLM provider accounts
behind an OpenAI Chat Completions-compatible endpoint.

This publication manifest builds the Rust `eggpool` executable as a
platform-specific Maturin binary wheel. The installed command is native code;
the package's Python requirement exists for package-manager compatibility
during the M11 rollback window and is not a runtime interpreter dependency.

Rust-backed M11 wheels are published only for the qualified Linux x86_64,
Linux aarch64, and macOS arm64 targets. The historical Python package remains
defined by the repository-root `pyproject.toml` through M11.
