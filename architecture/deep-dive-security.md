# Deep Dive: Security

Back to [Overview](overview.md)

## Purpose

Header redaction, API key authentication, trusted reverse-proxy attribution,
and security utilities to protect sensitive data in transit and at rest.

## Key Modules

### `security/redaction.py` — HeaderRedactionMiddleware

Strips sensitive headers from upstream responses before they reach clients:
- `Authorization` → redacted
- `X-Api-Key` → redacted
- Provider-specific auth headers → redacted
- Custom headers via config → redacted

### Forwarded client attribution

`[security].trusted_proxies` is an exact immediate-peer allowlist. The proxy
uses `X-Forwarded-For` or `X-Real-IP` only when the ASGI peer address is in
that list, and accepts only the first bounded, control-character-free value.
An empty list ignores forwarded attribution headers and falls back to the
immediate peer.

### `auth.py` — Local API Key Authentication

Constant-time API key comparison to prevent timing attacks:
- Used for local API authentication
- Compares against `EGGPOOL_API_KEY` env var
- Constant-time via `hmac.compare_digest()`

### `providers/contract.py` — Auth Header Construction

`build_auth_headers()` constructs provider-specific auth headers:
- `bearer` mode: `Authorization: Bearer <token>`
- `api_key` mode: custom header
- `raw_authorization` mode: verbatim value
- `none` mode: no auth header

### Bearer-Prefix Guard

`AppConfig.validate_account_credentials()` rejects API keys beginning with `Bearer` for providers using `auth.mode = "bearer"`. Prevents double-scheme auth errors.

## Config Security

- API keys stored in `.env` (never committed)
- `.env.example` shipped without real keys
- `config.toml` contains no secrets
- Provider `api_key_env` references environment variables

## Dashboard Security

- Dashboard auth gate protects all routes
- `/api/stats/runtime` always auth-gated even with public dashboard
- No raw prompts, tool outputs, or auth headers in any card or JSON response
- HTML escaping via `dashboard/escape.py`

## Key Invariants

- Constant-time API key comparison
- Sensitive headers redacted from upstream responses
- API keys never committed to repository
- `.env` files never committed
- Dashboard auth gate always active
- No raw prompts in any output surface
- Forwarded client-IP headers are never trusted from an unconfigured peer
