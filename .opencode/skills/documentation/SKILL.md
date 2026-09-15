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
- When documenting an optional native capability, describe both its enabled
  behavior and its disabled-feature behavior. Eggress 1.0.6 SSH is the
  compatibility exception: the default fallback supports it, while
  no-default builds reject SSH configuration before dialing and retain
  non-SSH proxy support.
- Describe configuration transitions through the typed policy in
  `rust/src/config_reload_policy.rs`: mutation paths classify before atomic
  replacement, and server-side rehash reclassifies before generation
  publication. Do not preserve stale caller-specific restart/reload lists.
- Keep historical plans append-only. Git history is the archive for retired
  migration scaffolding; current docs must describe the shipped Rust runtime.
- Document Responses as two bounded paths: canonical semantic adaptation and
  source-native same-surface preservation. State the stateless policy from
  admission (`store` omitted/false is accepted; stateful continuation and
  background features are rejected) and never imply native-only items/tools
  are translated losslessly. For streams, document native Responses
  observe-and-forward behavior separately from bounded cross-surface lifecycle
  synthesis, including authoritative completed output items and strict
  terminal evidence.

## Checks

```bash
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```
