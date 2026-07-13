# Live Configuration Rehash (Milestone A)

Milestone A delivers the foundation for live configuration changes
without a service restart. It ships three components:

1. A reusable validation contract (`config_validation.py`)
2. A typed configuration diff and reload-policy layer (`config_reload_policy.py`)
3. A fail-closed `rehash` CLI command

No field is currently marked `LIVE`; every change still requires a
service restart. The foundation is designed so milestone B
(RuntimeManager) and milestone C (control-plane socket) can consume
the same types without API changes.

## CLI surface

Both `check-config` and `rehash` use the same shared validator
(`validate_config_file()` in `config_validation.py`):

```bash
eggpool check-config           # validate and exit
eggpool rehash                 # validate, report status, never restart
```

`eggpool rehash` does **not** implicitly restart the service. It exits
zero on success and reports:

> Live rehash control is not yet available; run `eggpool restart` to
> apply process-bound changes. Validation passed and the running
> configuration is currently unchanged.

On validation failure it exits nonzero with:

> Live configuration is unchanged. Refusing to apply an invalid config
> and never invoking restart.

## Digests and fingerprints

The validation result carries two distinct hashes:

| Hash | Purpose |
|------|---------|
| `content_digest` | SHA-256 of the exact config file bytes. Guards against time-of-check / time-of-use drift. |
| `runtime_fingerprint` | Deterministic, secret-safe canonical hash. Secret fields (API keys, tokens) are redacted to `"<redacted>"` before hashing. Used for no-op detection and diagnostics. |

Operators see the content digest in `check-config` and `rehash` output:

```
Content digest: a1b2c3d4e5f6...
```

## Reload policy

Every `AppConfig` field is classified in the `_FIELD_DISPOSITION` map
in `config_reload_policy.py`:

| Disposition | Meaning |
|-------------|---------|
| `LIVE` | Can be hot-swapped without a restart (none currently) |
| `RESTART_REQUIRED` | Changing the field requires a service restart |
| `IGNORED` | Field is ignored for reload purposes |

**Milestone A default: every field is `RESTART_REQUIRED`.** This is
fail-closed — any field not explicitly classified requires a restart.
When milestone B/C adds a live-reload path for a field, the
corresponding entry moves to `LIVE` in the same diff.

## Status

Milestone A foundation in place; `rehash` accepts and validates the
new file but does not apply changes. Milestone B introduces a
RuntimeManager; milestone C introduces the control-plane socket that
will hand off the validated result.

## See also

- `plans/2026-07-13-live-config-rehash-milestone-a-validation-and-diff.md`
  — the implementation plan
- `architecture/README.md` § Live Configuration Rehash — validation
  contract, diff shape, wire types, and future milestones
