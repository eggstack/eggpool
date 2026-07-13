# Live Configuration Rehash

## Milestone A — Validation, Diffing, and Fail-Closed CLI (Complete)

Milestone A delivers the foundation for live configuration changes
without a service restart. It ships three components:

1. A reusable validation contract (`config_validation.py`)
2. A typed configuration diff and reload-policy layer (`config_reload_policy.py`)
3. A fail-closed `rehash` CLI command

The foundation is consumed by milestones B and C without API changes.

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

## Milestone C — Control Plane and Transactional Reload (Complete)

The live rehash path is fully operational:

- **Control socket**: Unix-domain socket at `~/.local/state/eggpool/eggpool.sock` with `0o600` permissions. Newline-delimited JSON protocol v1.
- **Reload flow**: CLI validates locally → sends validated digest to control socket → server re-validates → computes diff → rejects restart-required changes → builds candidate generation → reconciles persistence → atomic publication → retires old generation.
- **Transaction safety**: One reload at a time (serialized). Concurrent commands rejected. Content digest prevents TOCTOU races. All failures before publication are rollback/fail-closed.
- **Old generation retirement**: Active streams continue on their original generation. New requests use the new generation immediately after publication. Old resources close only after all leases drain.
- **Commands**: `eggpool rehash` applies live changes. `eggpool restart` remains available for disruptive changes (host, port, database path, etc.).

## See also

- `plans/2026-07-13-live-config-rehash-milestone-a-validation-and-diff.md`
  — the implementation plan
- `architecture/README.md` § Live Configuration Rehash — validation
  contract, diff shape, wire types, and future milestones
