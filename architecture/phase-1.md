# Phase 1: Configuration, Database, and Application Lifecycle

## Overview

Core infrastructure for loading configuration, managing SQLite connections, running migrations, and serving HTTP with proper startup/shutdown lifecycle.

## Components

### Configuration (`models/config.py`)
- Pydantic v2 models with strict validation (`extra="forbid"`)
- TOML file loading via stdlib `tomllib`
- Environment variable resolution for API keys
- Validation: duplicate accounts, missing env vars, invalid weights

### Database (`db/connection.py`)
- Async SQLite via `aiosqlite`
- Configurable pragmas: WAL mode, foreign keys, busy timeout, synchronous mode
- Connection pool: single connection with serialized writes
- Methods: `execute`, `fetch_all`, `fetch_one`

### Migrations (`db/migrations.py`)
- SQL file-based migrations in `db/schema/`
- Tracking table `_migrations` for applied versions
- Idempotent: safe to run multiple times
- Sequential execution with version ordering

### Authentication (`auth.py`)
- Bearer token via `Authorization` header
- Constant-time comparison via `hmac.compare_digest`
- Environment variable for proxy API key
- FastAPI dependency for route protection

### Application Lifecycle (`app.py`)
- FastAPI with `lifespan` context manager
- Startup: config → logging → database → migrations → HTTPX client
- Shutdown: HTTPX client → database (reverse order)
- `/healthz`: liveness (always OK)
- `/readyz`: readiness (checks database, accounts, enabled accounts)

### CLI (`cli.py`)
- Click group with `--config` option
- `serve`: load config, create app, run uvicorn
- `check-config`: validate config file
- `migrate`: connect to database, run migrations

## Data Flow

```
config.toml
    │
    ▼
AppConfig.from_toml()
    │
    ▼
create_app(config)
    │
    ▼
lifespan(app)
    │
    ├── configure_logging()
    ├── Database.connect()
    ├── MigrationRunner.run()
    └── httpx.AsyncClient()
    │
    ▼
FastAPI serves requests
    │
    ▼
shutdown
    ├── httpx_client.aclose()
    └── db.disconnect()
```

## Key Decisions

1. **Pydantic v2 for config**: Strong validation, good error messages, TOML-native
2. **aiosqlite**: Async SQLite without thread pool overhead
3. **Single database connection**: Sufficient for single-worker deployment, avoids write contention
4. **SQL migrations**: Simple, debuggable, version-controlled
5. **Click CLI**: Declarative, composable, good help generation
6. **Lifespan context manager**: Clean startup/shutdown, resource management
