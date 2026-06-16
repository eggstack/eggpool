# Phase 9: Deployment Hardening

## Overview

Production deployment infrastructure: systemd service, dedicated user, filesystem layout, graceful shutdown, and operational documentation.

> **Update (Phase 17):** Live configuration reload via SIGHUP was
> never implemented as a tested, semantically correct feature. The
> systemd unit intentionally omits `ExecReload` so a stray
> `systemctl reload gorouter` fails loudly instead of silently
> doing nothing. **All configuration changes require
> `sudo systemctl restart gorouter`.**

## Components

### Systemd Service (`deploy/gorouter.service`)
- Dedicated `gorouter` system user
- Security hardening (NoNewPrivileges, ProtectSystem, etc.)
- Graceful shutdown via SIGTERM (30s timeout)
- No live reload action (restart required for any config change)
- Automatic restart on failure

### Filesystem Layout

```
/etc/gorouter/
├── config.toml          # Main configuration
└── env                  # Environment variables (API keys)

/var/lib/gorouter/
├── usage.sqlite3        # SQLite database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory

/var/log/gorouter/
└── gorouter.log         # Application log (if file logging)

/opt/gorouter/
├── .venv/               # Python virtual environment
└── src/                 # Application source code
```

### Configuration Changes

Every configuration change requires a full service restart:

```bash
sudo systemctl restart gorouter
sudo systemctl status gorouter
sudo journalctl -u gorouter -n 100 --no-pager
```

This applies to every section of `config.toml` and to the
`/etc/gorouter/env` file (which holds the upstream API keys):

- Account list, weights, and offsets
- API keys (the env file)
- Upstream URL and timeouts
- Quota windows and routing strategy
- Log level
- Database path
- Bind address and port

The earlier intent of a partial SIGHUP reload was replaced by an
explicit restart-only policy in Phase 17 because no tested,
semantically correct reload path exists. The dashboard already
covers the operational use case that partial reload would have
served (live inspection of state).

### Graceful Shutdown
1. Stop accepting new connections
2. Wait for in-flight requests (up to 30s)
3. Close HTTPX client connections
4. Disconnect from SQLite
5. Exit cleanly

### Log Rotation (`deploy/logrotate.conf`)
- Daily rotation
- 30-day retention
- Compression enabled

## Key Decisions

1. **Single-worker**: Sufficient for personal use, avoids multi-process complexity
2. **WAL mode**: Allows concurrent reads during writes
3. **Security hardening**: Defense-in-depth for LAN deployment
4. **Restart-only configuration changes**: Replaces the earlier SIGHUP reload intent
5. **Raspberry Pi optimized**: Reduced connection pools, less frequent catalog refresh
