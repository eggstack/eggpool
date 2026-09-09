# Q006 Closure — Disposable Rootful Linux Operational Acceptance

Status: implementation complete; acceptance blocked pending a disposable
Linux host with systemd as PID 1

Plan: [Q006 — disposable rootful Linux operational acceptance](../../implementation/qualification/006-rootful-linux-operational-acceptance.md)

## Outcome

The repository implementation for Q006 is complete. It adds the guarded,
reusable host runner at
[`scripts/qualification_rootful_linux.py`](../../../scripts/qualification_rootful_linux.py)
and deterministic contract coverage in
[`test_q006_rootful_linux.py`](../../../tests/migration_rs/test_q006_rootful_linux.py).
The runner is qualification-only: it does not change the public Python
installer or release authority.

Formal acceptance is not claimed from the current development host. The host
is macOS ARM64, has no systemd PID 1, and is not a permitted substitute for
the required disposable Linux environment. Q007 therefore remains queued.

## Implemented boundary corrections

The OS-boundary audit found and corrected two defects that fake command
runners could not expose:

1. Both `Type=simple` systemd units now execute `serve --verbose`, keeping
   the service process in the systemd cgroup instead of daemonizing behind
   systemd's tracked process.
2. Production runtime-path resolution follows `/etc/eggpool/config.toml`
   for the service data, state, runtime, PID, and log paths. Production
   uninstall now targets the production config/env directory and removes the
   explicit `/var/lib/eggpool`, `/var/log/eggpool`, and
   `/var/backups/eggpool` trees only under the existing keep-data policy.

Each correction has deterministic Rust regression coverage in the O002/O009
operation suites.

## Runner contract

The runner requires effective root, Linux, systemd as PID 1, `systemctl`,
`useradd`, `userdel`, and `runuser`. It refuses pre-existing managed paths and
an existing `eggpool` user, requires the explicit
`--i-understand-disposable-host` acknowledgement, uses only a loopback
provider, creates a temporary non-root test user, and records bounded
secret-free command/path/service evidence. `--cleanup` is guarded by a
runner-owned marker and removes only the reviewed managed paths.

Canonical command for the required external acceptance:

```text
sudo -E uv run python scripts/qualification_rootful_linux.py \
  --binary rust/target/release/eggpool \
  --output migration-rs/closure/qualification/006-run.json \
  --i-understand-disposable-host
```

## Verification performed here

The current host facts are macOS ARM64 (Darwin), so the runner's safe refusal
was exercised:

```text
uv run python scripts/qualification_rootful_linux.py \
  --binary rust/target/debug/eggpool \
  --output /tmp/q006-current-host.json \
  --i-understand-disposable-host
# exit 2; {"schema":"m10-q006.v1","status":"blocked",...
```

Deterministic verification passed:

```text
cargo test --manifest-path rust/Cargo.toml --test operations_o002 --test operations_o003 --test operations_o006 --test operations_o009 -- --test-threads=1
# 21 passed
uv run pytest tests/migration_rs/test_q006_rootful_linux.py -q --tb=short --maxfail=1
# 5 passed
uv run ruff check scripts/qualification_rootful_linux.py tests/migration_rs/test_q006_rootful_linux.py
# all checks passed
uv run pyright scripts/qualification_rootful_linux.py
# 0 errors, 0 warnings, 0 informations
git diff --check
# pass
```

## Findings and transition

No unresolved deterministic high/medium finding remains in the implemented
boundary. The required real-host acceptance remains an external blocker, not
a passed qualification. Q006 stays the dependency-ready plan; Q007–Q010
remain queued behind their direct predecessors. No future plan is unblocked
by this incomplete external acceptance, and M11 remains blocked on accepted
Q010 plus its separate planning review.
