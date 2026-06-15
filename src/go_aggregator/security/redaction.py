"""Secret and credential redaction helpers."""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Authorization: Bearer <token>  /  Authorization: <scheme> <value>
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:[^\s,;\"'}]+(?:\s+[^\s,;\"'}]+)*)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-+/=]+)")

# OpenAI/Anthropic style API keys beginning with sk- (with optional
# suffix or separator).
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b")

# password=..., secret=..., api_key=... assignments
_PASSWORD_RE = re.compile(r"(?i)(password\s*=\s*)([^\s,;\"'}]+)")
_SECRET_RE = re.compile(r"(?i)(\bsecret\s*=\s*)([^\s,;\"'}]+)")
_API_KEY_RE = re.compile(r"(?i)(api[_-]?key\s*=\s*)([^\s,;\"'}]+)")

# JSON "prompt": "..." and "completion": "..." fields
_PROMPT_FIELD_RE = re.compile(r'(?i)("prompt"\s*:\s*)"([^"\\]*(?:\\.[^"\\]*)*)"')
_COMPLETION_FIELD_RE = re.compile(
    r'(?i)("completion"\s*:\s*)"([^"\\]*(?:\\.[^"\\]*)*)"'
)

# https://user:pass@host/...
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)([^\s/:@\"'<>]+):([^\s/@\"'<>]+)@"
)

# Sensitive query parameters
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:key|token|api_key|access_token)=)([^&\s\"'<>]+)"
)


def redact_error_detail(value: str | None) -> str | None:
    """Replace secret-bearing fragments in an error detail string.

    The returned value is safe to persist to a database or write to
    logs. Matches are replaced with the ``[REDACTED]`` marker. ``None``
    and empty input are returned unchanged.
    """
    if value is None or value == "":
        return value
    redacted = value
    redacted = _AUTH_HEADER_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _BEARER_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _SK_KEY_RE.sub(REDACTED, redacted)
    redacted = _PASSWORD_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _SECRET_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _API_KEY_RE.sub(r"\1" + REDACTED, redacted)
    redacted = _PROMPT_FIELD_RE.sub(r'\1"' + REDACTED + '"', redacted)
    redacted = _COMPLETION_FIELD_RE.sub(r'\1"' + REDACTED + '"', redacted)
    redacted = _URL_USERINFO_RE.sub(r"\g<scheme>" + REDACTED + "@", redacted)
    redacted = _SENSITIVE_QUERY_RE.sub(r"\1" + REDACTED, redacted)
    return redacted
