# Deep Dive: External Integrations

Back to [Overview](overview.md)

## Purpose

Generates configuration files for external coding tools (OpenCode, Claude Code, Aider, Codex, etc.) so they can use EggPool as their LLM backend.

## Supported Tools

| Tool | Module | Config Format |
|------|--------|---------------|
| OpenCode | `opencode.py` | JSON |
| Claude Code | (via `cli_full.py`) | — |
| Aider | `aider.py` | ENV |
| Cline | `cline.py` | JSON |
| Codex | `codex.py` | TOML |
| Qwen Code | `qwen_code.py` | JSON |
| Kilo | `kilo.py` | JSON |
| Continue | `continue_dev.py` | YAML |
| Roo Code | `roo_code.py` | JSON |
| Goose | `goose.py` | ENV |
| OpenHands | `openhands.py` | ENV |

## Key Module

### `integrations/common.py`

Shared helpers:
- Config setup context construction
- Catalog-backed default model resolution
- Format-safe scalar/key rendering (JSON, TOML, YAML, shell, model ID)

New integration targets reuse these helpers instead of hand-quoting values.

## CLI Integration

`eggpool configsetup <tool>` generates configuration for the specified tool:
- Reads EggPool config and catalog
- Generates tool-specific config format
- Writes to appropriate config location
- Handles model ID formatting per tool conventions

## Key Invariants

- All source-provided text is format-safe rendered
- Catalog-backed defaults ensure model IDs exist
- Config generation never overwrites without consent
- Each tool's config format is native (TOML, JSON, YAML as appropriate)
