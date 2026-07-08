"""Deterministic normalization primitives for model-info identity matching.

All functions are pure, side-effect-free, and stdlib-only. They operate on
raw strings and return new strings — inputs are never mutated, preserving
the original for display and diagnostics.

Design goals:
- Deterministic: same input always produces the same output.
- Conservative: collapsing only happens when a vendor token exactly repeats
  the next token after casefolding.
- Auditable: every normalization step is explicit and traceable.
"""

from __future__ import annotations

import re
import unicodedata

# Separator pattern: space, hyphen, underscore, colon, dot, slash.
_SEP_RE = re.compile(r"[-_:. /]+")

_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")


def normalize_model_key(value: str) -> str:
    """Deterministic comparison key for any model identifier.

    Rules (in order):
    1. Unicode normalize with NFKC
    2. .casefold() (not .lower())
    3. Strip leading/trailing whitespace
    4. Strip leading ``<vendor>:`` / ``<vendor> -`` decoration when the
       vendor token exactly matches the next token (handles
       ``MiniMax: MiniMax M3`` -> ``MiniMax M3`` -> ``minimaxm3``)
    5. Replace separators (- _ : . / space) with nothing
    6. Remove all non-alphanumeric characters
    7. Collapse runs of whitespace between tokens before stripping
       (so the duplicate-token collapse in step 4 still works)
    """
    # Step 1: Unicode NFKC normalization
    value = unicodedata.normalize("NFKC", value)
    # Step 2: casefold
    value = value.casefold()
    # Step 3: strip leading/trailing whitespace
    value = value.strip()
    if not value:
        return ""
    # Step 7 (before step 4): collapse runs of whitespace so token
    # comparison works correctly.
    value = re.sub(r"\s+", " ", value)
    # Step 4: collapse duplicate vendor prefix (must happen before
    # separator removal so we can split on the first separator).
    value = collapse_duplicate_vendor(value)
    # Step 5: replace separators with nothing
    value = _SEP_RE.sub("", value)
    # Step 6: remove all non-alphanumeric characters
    value = _NON_ALNUM_RE.sub("", value)
    return value


def split_source_id(value: str) -> tuple[str | None, str]:
    """Split slash-delimited source IDs into (namespace_vendor_or_None, model_segment).

    Examples:
        "minimax/minimax-m3"        -> ("minimax", "minimax-m3")
        "anthropic/claude-sonnet-4.5" -> ("anthropic", "claude-sonnet-4.5")
        "minimax-m3"                -> (None, "minimax-m3")
        "opencode-go/minimax-m3"    -> ("opencode-go", "minimax-m3")
    """
    parts = value.split("/", 1)
    if len(parts) == 2:
        return (parts[0], parts[1])
    return (None, value)


# ---------------------------------------------------------------------------
# Deployment-suffix variant generator
# ---------------------------------------------------------------------------
#
# Some providers attach deployment/presentation suffixes to a base source
# model ID.  ``MiniMax-M2.7-highspeed`` is the same underlying ``MiniMax-M2.7``
# model, but presented under a routing alias.  These tokens are
# deployment-tier descriptors, NOT semantic model variants like ``pro``,
# ``mini``, ``flash``, or ``lite``.
#
# Stripping only happens when the trailing token is in this set AND the
# remaining base still contains a digit/family anchor.  Bare names like
# just ``highspeed`` are rejected.

DEPLOYMENT_SUFFIX_TOKENS: frozenset[str] = frozenset(
    {
        "highspeed",
        "fast",
        "turbo",
        "speed",
        "lowlatency",
        "lowlat",
    }
)

# Tokens that look like deployment suffixes but are actually semantic
# model variants.  Must NEVER be stripped.
SEMANTIC_VARIANT_TOKENS: frozenset[str] = frozenset(
    {
        "pro",
        "mini",
        "flash",
        "lite",
        "max",
        "plus",
        "instruct",
        "chat",
        "reasoning",
        "thinking",
        "preview",
        "code",
        "coder",
        "omni",
    }
)


def has_digit_or_family_anchor(value: str) -> bool:
    """Return True if the model string contains at least one digit or
    a recognizable family anchor.

    Used to ensure we never strip suffixes from a bare name like
    ``highspeed`` alone.  This is a deliberately loose test: any digit
    anywhere in the string is enough, since version/family anchors are
    how we tell two deployment variants apart.
    """
    if not value:
        return False
    return any(ch.isdigit() for ch in value)


_DEPLOYMENT_SUFFIX_RE = re.compile(
    r"(?i)(?P<sep>[-_:. /]+)(?P<suffix>highspeed|fast|turbo|speed|lowlatency|lowlat)$"
)


def _strip_deployment_suffix_segment(raw: str) -> tuple[str, str] | None:
    """Strip a single deployment-suffix segment from a model string.

    Returns ``(base, suffix)`` when stripping succeeds and the base has
    a digit/family anchor, or ``None`` when no safe strip is possible.
    """
    match = _DEPLOYMENT_SUFFIX_RE.search(raw)
    if match is None:
        return None
    suffix = match.group("suffix").casefold()
    if suffix not in DEPLOYMENT_SUFFIX_TOKENS:
        return None
    base = raw[: match.start("sep")]
    if not base or not has_digit_or_family_anchor(base):
        return None
    tokens = tokenize_model_key(raw)
    if set(tokens) & SEMANTIC_VARIANT_TOKENS:
        return None
    return base, suffix


def generate_deployment_suffix_variants(value: str) -> tuple[str, ...]:
    """Return deterministic base-name variants with deployment suffixes stripped.

    Conservative rules:

    1. If the input is slash-delimited, only the model segment is varied;
       the namespace prefix is preserved on each emitted variant.
    2. The trailing token (after separator split) must be in
       :data:`DEPLOYMENT_SUFFIX_TOKENS`.  Tokens in
       :data:`SEMANTIC_VARIANT_TOKENS` are NEVER stripped.
    3. After stripping, the remaining base must still carry a digit or
       family anchor -- preventing ``highspeed`` -> ``""``.
    4. Only one suffix is stripped per call.  Chained suffixes are not
       automatic (no evidence yet of safety).
    5. Order is deterministic: original input first, then the stripped
       variant (if produced).  Callers should de-duplicate if needed.

    Examples::

        >>> generate_deployment_suffix_variants("MiniMax-M2.7-highspeed")
        ('MiniMax-M2.7-highspeed', 'MiniMax-M2.7')
        >>> generate_deployment_suffix_variants("minimax/MiniMax-M2.7-highspeed")
        ('minimax/MiniMax-M2.7-highspeed', 'minimax/MiniMax-M2.7')
        >>> generate_deployment_suffix_variants("MiniMax-M2.7-pro")
        ('MiniMax-M2.7-pro',)
    """
    if not value:
        return (value,)

    namespace, model_segment = split_source_id(value)

    result = _strip_deployment_suffix_segment(model_segment)
    if result is None:
        return (value,)

    base, _suffix = result
    stripped = f"{namespace}/{base}" if namespace else base
    if stripped == value:
        return (value,)

    return (value, stripped)


def normalize_vendor_key(value: str | None) -> str | None:
    """Return a deterministic vendor comparison key.

    Uses normalize_model_key; returns None when input is None or empty.
    """
    if value is None:
        return None
    normalized = normalize_model_key(value)
    return normalized if normalized else None


def tokenize_model_key(value: str) -> tuple[str, ...]:
    """Tokenize a model identifier into a tuple of lowercase tokens.

    Splits on the same separators as normalize_model_key (space, hyphen,
    underscore, colon, dot, slash). Filters empty tokens. Preserves
    ordering.

    Example: "claude-sonnet-4.5" -> ("claude", "sonnet", "4", "5")
             "MiniMax-M3" -> ("minimax", "m3") after casefold
    """
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    raw = _SEP_RE.split(value)
    return tuple(t for t in raw if t)


def strip_provider_namespace(value: str, known_providers: set[str]) -> str:
    """If value is slash-delimited AND the prefix is in known_providers,
    return the model segment after the slash; otherwise return value.

    This is for cases like "opencode-go/minimax-m3" -> "minimax-m3"
    (when opencode-go is a known provider). The provider namespace is
    NOT a vendor namespace for aggregator providers.
    """
    parts = value.split("/", 1)
    if len(parts) == 2 and parts[0] in known_providers:
        return parts[1]
    return value


def collapse_duplicate_vendor(value: str) -> str:
    """If value starts with a leading vendor token that exactly repeats
    the next token (case-insensitively after stripping the trailing
    separator and whitespace), drop the leading repeat.

    Example: "MiniMax: MiniMax M3" -> "MiniMax M3"
             "MiniMax - MiniMax M3" -> "MiniMax M3"
             "minimax: minimax m3" -> "minimax m3"
             "minimax-m3" -> "minimax-m3" (unchanged)
    """
    # Split on the first separator to isolate the leading token.
    # Only trigger collapse for separators used in display-name decoration
    # (colon, dash, space, dot, underscore) — NOT slash, which indicates
    # a source namespace like "minimax/minimax-m3".
    match = re.match(r"^([^\s:_.\-/]+)\s*([:_.\- ])\s*", value)
    if not match:
        return value
    prefix = match.group(1)
    rest = value[match.end() :]
    rest_lower = rest.casefold()
    prefix_lower = prefix.casefold()
    if rest_lower.startswith(prefix_lower):
        return rest
    return value
