# Per-Account Outbound Proxy

EggPool supports routing upstream LLM traffic through per-account outbound proxies using [pproxy](https://github.com/qwj/python-proxy) (version 2.7.9+). This is useful for geo-routing, residential IP rotation, IP isolation between accounts, or tunneling through a corporate proxy.

## How It Works

When an account is configured with a proxy, EggPool creates a dedicated `httpx.AsyncClient` for that account with a custom pproxy transport. All upstream requests for that account are tunneled through the configured proxy. Accounts without a proxy use direct connections.

The proxy is applied at the **account level**, not the provider level. Different accounts on the same provider can use different proxies.

## Configuration

Three mutually exclusive fields on each account control the proxy:

| Field | Description | Use When |
|-------|-------------|----------|
| `proxy` | Reference a named entry from `[proxies.*]` | You want to share proxy config across accounts or keep credentials in env vars |
| `proxy_url` | Inline pproxy URI | The URI contains no secrets (e.g. localhost SOCKS5) |
| `proxy_url_env` | Environment variable name holding the pproxy URI | The URI contains credentials (passwords, tokens) |

An account can set at most one of these fields. Setting more than one is a configuration error.

## Named Proxies (`[proxies.*]`)

Define shared proxy configurations at the top level of your config file. Each entry has either `url` or `url_env` (exactly one).

```toml
# Proxy with inline URL (no credentials)
[proxies.local-socks5]
url = "socks5://127.0.0.1:1080"

# Proxy with URL from environment variable (for credentials)
[proxies.residential-us]
url_env = "RESIDENTIAL_PROXY_URL"

# Proxy with URL from environment variable
[proxies.datacenter-eu]
url_env = "DC_EU_PROXY_URL"
```

Then reference the named proxy from an account:

```toml
[[providers.opencode-go.accounts]]
name = "personal"
api_key = "sk-your-key"
proxy = "local-socks5"

[[providers.opencode-go.accounts]]
name = "work"
api_key = "sk-work-key"
proxy = "residential-us"
```

## pproxy URI Syntax

EggPool accepts any valid [pproxy URI](https://github.com/qwj/python-proxy#uri-syntax):

```
{scheme}://[{cipher}@]{netloc}/[@{localbind}][,{plugins}][?{rules}][#{auth}]
```

### Supported Schemes

| Scheme | Protocol | Notes |
|--------|----------|-------|
| `http://` | HTTP CONNECT proxy | Most common; works with all HTTPS traffic |
| `socks4://` | SOCKS4 | No DNS resolution through proxy |
| `socks5://` | SOCKS5 | Supports DNS resolution through proxy |
| `ss://` | Shadowsocks | Requires cipher and key |
| `ssr://` | ShadowsocksR | Requires cipher, key, and SSR plugins |
| `ssh://` | SSH tunnel | Requires `asyncssh` package |
| `trojan://` | Trojan protocol | Typically combined with SSL |

You can combine schemes with `+` (e.g. `http+socks5://`).

### Authentication in URIs

Use the `#` fragment for username/password:

```
http://proxy.example.com:3128#username:password
socks5://proxy.example.com:1080#user:pass
```

When the URI contains credentials, prefer `url_env` or `proxy_url_env` to keep secrets out of the config file.

## Examples

### Local SOCKS5 Proxy

Route all traffic from one account through a local SOCKS5 proxy (e.g. SSH tunnel):

```toml
[[providers.opencode-go.accounts]]
name = "personal"
api_key = "sk-your-key"
proxy_url = "socks5://127.0.0.1:1080"
```

### HTTP Proxy with Authentication

Route traffic through a corporate or residential HTTP proxy:

```toml
[proxies.residential]
url_env = "RESIDENTIAL_PROXY_URL"

[[providers.opencode-go.accounts]]
name = "residential-acct"
api_key = "sk-key"
proxy = "residential"
```

Then in your environment:

```bash
export RESIDENTIAL_PROXY_URL="http://user:password@proxy.example.com:3128"
```

### Shadowsocks Proxy

Route traffic through a Shadowsocks server:

```toml
[[providers.deepseek.accounts]]
name = "ss-acct"
api_key = "sk-deepseek-key"
proxy_url = "ss://aes-256-gcm:my-secret-key@ss-server.example.com:8388"
```

### Per-Account Proxy Isolation

Different accounts on the same provider use different proxies:

```toml
[proxies.us-east]
url_env = "US_EAST_PROXY_URL"

[proxies.eu-west]
url_env = "EU_WEST_PROXY_URL"

[providers.openai]
id = "openai"
base_url = "https://api.openai.com/v1"
protocols = ["openai"]

[[providers.openai.accounts]]
name = "us-account"
api_key_env = "OPENAI_US_KEY"
proxy = "us-east"

[[providers.openai.accounts]]
name = "eu-account"
api_key_env = "OPENAI_EU_KEY"
proxy = "eu-west"
```

### Mixed: Some Accounts Proxied, Others Direct

Only accounts with a proxy field are routed through the proxy. Other accounts connect directly:

```toml
[providers.opencode-go]
id = "opencode-go"
base_url = "https://opencode.ai/zen/go/v1"
protocols = ["openai", "anthropic"]

# This account connects directly (no proxy field)
[[providers.opencode-go.accounts]]
name = "direct"
api_key = "sk-direct-key"

# This account routes through a proxy
[[providers.opencode-go.accounts]]
name = "proxied"
api_key = "sk-proxied-key"
proxy_url = "socks5://127.0.0.1:1080"
```

### SSH Tunnel as Proxy

If you have an SSH tunnel set up (e.g. `ssh -D 1080 remote-host`), point accounts at the local SOCKS5 endpoint:

```toml
[[providers.opencode-go.accounts]]
name = "tunneled"
api_key = "sk-key"
proxy_url = "socks5://127.0.0.1:1080"
```

## Verifying Proxy Configuration

After configuring proxies, restart the service and verify:

```bash
# Restart
sudo systemctl restart eggpool

# Check logs for proxy setup
sudo journalctl -u eggpool -n 50 --no-pager | grep -i proxy

# Test with the smoke test
GOROUTER_BASE_URL=http://127.0.0.1:11300 \
GOROUTER_API_KEY=$(sudo grep ^GO_AGGREGATOR_API_KEY /etc/eggpool/env | cut -d= -f2-) \
GOROUTER_OPENAI_MODEL=gpt-4 \
GOROUTER_ANTHROPIC_MODEL=claude-3-5-sonnet \
  uv run python scripts/smoke_test.py
```

## Troubleshooting

### Connection Refused / Timeout

1. Verify the proxy is running and reachable from the EggPool host
2. Check that the proxy URI scheme matches the proxy server's protocol
3. For SOCKS5, ensure DNS resolution is configured correctly (use `socks5h://` if the proxy resolves DNS)

### Authentication Errors

1. If using `proxy_url_env`, verify the environment variable is set and exported
2. If using `proxy` with a named entry, verify the `[proxies.*]` section exists and the name matches
3. Check that credentials in the URI are properly encoded (URL-encode special characters like `@`, `:`, `/`)

### Only Some Requests Are Proxied

- Proxies are per-account, not per-provider. Ensure the account routing selects the proxied account.
- Use `eggpool accounts status` to see which accounts have proxy configuration.

### Debugging

Enable verbose logging to see proxy connection details:

```toml
[server]
log_level = "DEBUG"
```

Check logs for pproxy connection attempts and errors:

```bash
sudo journalctl -u eggpool -f | grep -i "proxy\|pproxy\|socks\|tunnel"
```
