---
name: documentation
description: Documentation maintenance for the EggPool project. Use when updating README.md, docs/, architecture/, AGENTS.md, or .opencode/skills/ — covers the doc map, accuracy verification against code, and pruning rules.
---

# Documentation Maintenance

## Doc Map (what lives where)

| Location | Audience | Content |
|----------|----------|---------|
| `README.md` | New users | Setup/deploy focus; CLI table; key config sections |
| `docs/` | Operators | Deployment, providers, transcoding, rehash, runbooks |
| `architecture/README.md` + `deep-dive-*.md` | Contributors | Current design index; describes shipped runtime only |
| `plans/` | Historical record | Numbered implementation plans; never a navigation chain |
| `AGENTS.md` | Agents | Fast index: workflow, gotchas, pointers to architecture |
| `.opencode/skills/` | Agents | Task-scoped skills (architecture, deployment, development, documentation) |

## Rules

- **CI ignores docs-only changes**: `paths-ignore` in
  `.github/workflows/ci.yml` skips `docs/**`, `architecture/**`,
  `plans/**`, `.opencode/skills/**`, `AGENTS.md`, and `CHANGELOG.md`.
  Docs-only PRs show no CI run; do not wait for one.
- **Plans are provenance, not navigation.** Never update completed plans
  to match current code and never chain through them for ordinary work.
- **Architecture describes today's runtime**, not history. When a change
  alters behavior described in a deep dive, update that deep dive in the
  same PR.
- **Prune aggressively but verify first**: confirm a claim is wrong in
  code before deleting it; confirm nothing links to a file before
  removing it.

## Accuracy Verification Checklist

Before committing doc changes, verify claims against the code:

1. **CLI commands** — run `uv run eggpool --help` (and subcommand help);
   compare against any command table.
2. **Config sections/fields** — top-level sections must exist in
   `config.example.toml` or the config model
   (`src/eggpool/config_validation.py` canonical section list); field
   names must appear in `config_reload_policy.py` when claiming
   live-reloadability.
3. **API endpoints** — registered routes live in `src/eggpool/api/*.py`
   (`app.add_api_route`) and `src/eggpool/dashboard/routes.py`; grep for
   `path="/api/...` / `path="/v1/...`.
4. **Exit codes** — `src/eggpool/cli_exit_codes.py`.
5. **Error hierarchy** — `src/eggpool/errors.py` plus
   `config_validation.py`, `transcoder/errors.py`,
   `transcoder/budget_resolver.py`, `catalog/protocols.py`.
6. **Env vars used by scripts** — read the script's own docstring
   (`scripts/check_database.py`, `scripts/smoke_test.py`,
   `scripts/verify_upstream_auth.py`).
7. **File references** — every `src/...`, `scripts/...`, `deploy/...`
   path mentioned must exist on disk.
8. **Relative links** — every `](...)` target must resolve from the
   linking file's directory.
9. **Counts decay fast** (themes, providers, migrations, tests, spans).
   Either omit counts or re-derive them at edit time.

## Update Triggers

Update affected docs whenever a change:

- adds/removes a CLI command or flag → README CLI table,
  `docs/deployment.md` deploy reference
- adds/removes an API endpoint → `docs/api-reference.md`,
  `architecture/deep-dive-dashboard.md`
- renames/removes a config field → `config.example.toml`,
  `docs/live-config-rehash.md` LIVE-field list, README sections table
- changes reload semantics → `docs/live-config-rehash.md`
- changes request lifecycle/finalization/routing invariants → matching
  `architecture/deep-dive-*.md` and the `architecture` skill
- changes CI/test gates → `AGENTS.md`, `development` skill,
  `docs/releasing.md`

## Style

- Keep operator docs procedural (commands + expected output); keep
  architecture declarative (invariants + ownership).
- No prompt/response/credential examples with real-looking keys; use
  obvious placeholders.
- Match existing heading structure per file rather than restructuring.
