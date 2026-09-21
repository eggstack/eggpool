---
name: deployment
description: Deployment and operations for the native Rust EggPool runtime.
---

# Deployment and Operations

The supported production artifact is the native Rust wheel or verified raw
Rust executable. Use `scripts/install.sh` for personal package-manager
installation and `eggpool deploy systemd --install` for a managed service.

The quick installer recognizes uv, pipx, pip/venv, standalone, and source
ownership. It refuses ambiguous ownership, collisions, unsupported targets,
and standalone replacement without `--adopt-standalone`. It preserves config,
database, and `.env` files and never clones the repository for a normal install.

Supported release targets are Linux x86_64, Linux aarch64, and macOS arm64.
The `eggpool-connect` desktop helper additionally ships for Windows x86_64;
a Windows helper never implies Windows proxy support. The native executable
owns update, deployment, backup, restore, recovery,
uninstall, runtime paths, and control-socket behavior. Historical Python
packages remain immutable external artifacts selected only by explicit,
catalogued compatible exact version.

Ownership: `rust/src/operations/deploy.rs` renders systemd/logrotate/cron,
`operations/backup.rs` owns staged atomic ZIP backup/restore,
`operations/update.rs` owns GitHub release discovery + SHA-256 verified
transition, `operations/paths.rs` owns config/data/state/log/PID/socket
resolution, and `operations/lifecycle.rs` owns start/stop/restart/ensure-running
composition. Start at `architecture/deep-dive-deployment.md` and
`architecture/deep-dive-lifecycle.md` for design; use `docs/deployment.md`,
`docs/backup-restore.md`, `docs/upgrading.md`, and `docs/raspberry-pi.md` for
operator procedures. Config examples are `config.example.toml` (full) and
`config.sbc.example.toml` (low-wear SBC profile).

The reviewed Maturin 1.14.1 release build passes `--strip false` and the
packaging manifest sets `strip = false`. Stripping or ThinLTO may be adopted
only after equivalent executable/wheel/runtime and every supported-target
qualification; a local macOS host is not SBC or Linux target evidence.

## Safe checks

```bash
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_runtime_package_boundary.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/qualify_quick_installer.py
```

Use disposable hosts and paths for rootful/systemd qualification. Never run
qualification scripts against a production installation.
