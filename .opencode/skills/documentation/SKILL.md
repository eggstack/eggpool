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
| `migration-rs/` | Maintainers | Append-only plans, evidence, and closure records |
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
- Keep migration plans and closure records append-only. Only the registry
  authorizes implementation, and only accepted P006 closes M12.

## Checks

```bash
uv run python scripts/validate_cutover_docs.py
uv run python scripts/validate_m12_retirement.py
git diff --check
```
