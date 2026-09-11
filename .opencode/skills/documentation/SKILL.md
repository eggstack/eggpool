---
name: documentation
description: Documentation maintenance for the native Rust EggPool runtime and its tooling boundary.
---

# Documentation Maintenance

## Doc map

| Location | Audience | Content |
|---|---|---|
| `README.md` | New users | Current install, CLI, and development flow |
| `docs/` | Operators | Deployment, providers, API, runbooks, release/rollback |
| `architecture/` | Contributors | Current Rust design and ownership |
| `plans/` | Maintainers | Historical plans and current maintenance records |
| `AGENTS.md`, `.opencode/skills/` | Agents | Repository workflow and task guidance |

## Rules

- Architecture and operational docs describe the shipped Rust runtime, not a
  duplicated historical application.
- Explain that `Requires-Python >=3.11` is package-manager compatibility for
  explicit historical targets, not a production interpreter dependency.
- Historical Python source is recoverable from immutable Git history; it is not
  an active source package.
- Verify every command, path, route, environment variable, and relative link
  against the current tree before documenting it.
- Treat `rust/Cargo.toml` as the authority for native dependency and feature
  claims. Do not document proxy, TLS, SQLite, or archive capabilities that are
  not present in the resolved Cargo feature graph.
- Keep historical plans append-only. Git history is the archive for retired
  migration scaffolding; current docs must describe the shipped Rust runtime.

## Checks

```bash
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```
