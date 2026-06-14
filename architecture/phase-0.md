# Phase 0: Repository and Tooling Foundation

## Overview

Initial project setup with Python package structure, build system, and development tooling.

## Components

### Build System
- **hatchling** as PEP 517 build backend
- src layout with `src/go_aggregator/`
- Version managed in `__init__.py`

### Dependencies
- **Runtime**: FastAPI, uvicorn, httpx, aiosqlite, pydantic, jinja2, click
- **Dev**: ruff, pyright, pytest, pytest-asyncio, respx, coverage, pre-commit

### Tooling
- **Ruff**: Linting and formatting (E, F, W, I, N, UP, B, A, SIM, TCH)
- **Pyright**: Static type checking (strict mode)
- **pytest**: Test runner with pytest-asyncio for async tests
- **respx**: HTTPX upstream mocking

### Package Structure
```
src/go_aggregator/
├── __init__.py      # Package version
├── __main__.py      # python -m entrypoint
├── app.py           # FastAPI factory
├── cli.py           # Click CLI
├── constants.py     # Project constants
├── errors.py        # Exception hierarchy
└── logging.py       # Structured logging
```

## Key Decisions

1. **src layout**: Prevents accidental imports of uninstalled code
2. **hatchling**: Modern, fast build backend with good PEP 621 support
3. **Ruff over flake8+isort+black**: Single tool for linting and formatting, faster
4. **Pyright over mypy**: Stricter, faster, better FastAPI support
5. **Click over argparse**: More ergonomic CLI with decorators
