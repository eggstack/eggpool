# Phase 9: Deployment Hardening

## Overview

Production deployment infrastructure: systemd service, dedicated user, filesystem layout, configuration reload, graceful shutdown, and operational documentation.

## Components

### Systemd Service (`deploy/gorouter.service`)
- Dedicated `gorouter` system user
- Security hardening (NoNewPrivileges, ProtectSystem, etc.)
- Graceful shutdown via SIGTERM (30s timeout)
- Configuration reload via SIGHUP
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

### Configuration Reload (SIGHUP)
Reloads without restart:
- Account list and API keys
- Model exposure mode
- Dashboard enable/disable
- Log level

Requires full restart:
- Database path
- Upstream URL
- Server bind address

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
4. **SIGHUP reload**: Zero-downtime configuration changes
5. **Raspberry Pi optimized**: Reduced connection pools, less frequent catalog refresh
