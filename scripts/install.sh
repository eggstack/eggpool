#!/usr/bin/env bash
set -euo pipefail

# EggPool quick install: binary-first verified GitHub raw executable for fresh
# current-native installs. This script is intentionally usable as a curl
# pipeline and does not clone, build, or execute a repository-local
# application for normal installs.

FORCE_REINSTALL=0
UPGRADE_ONLY=0
ADOPT_STANDALONE=0
TARGET_VERSION=""
VERSION_REQUESTED=0
EXPLICIT_PACKAGE_MANAGER=""
NATIVE_RELEASE_VERSION="0.8.0"
INSTALL_INDEX_URL="${EGGPOOL_INSTALL_INDEX_URL:-}"
INSTALL_FIND_LINKS="${EGGPOOL_INSTALL_FIND_LINKS:-}"
RELEASE_BASE_URL="${EGGPOOL_RELEASE_BASE_URL:-https://github.com/eggstack/eggpool/releases}"
ALLOW_NONPROD_ORIGIN="${EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_ORIGIN:-}"
ALLOW_NONPROD_INDEX="${EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX:-}"
DEFAULT_RELEASE_BASE_URL="https://github.com/eggstack/eggpool/releases"
MAX_ARTIFACT_BYTES=134217728

version_at_or_after_native_release() {
    local version="$1"
    local major minor patch
    local native_release_major native_release_minor native_release_patch
    IFS=. read -r major minor patch <<< "$version"
    IFS=. read -r native_release_major native_release_minor native_release_patch <<< "$NATIVE_RELEASE_VERSION"
    ((10#$major > 10#$native_release_major)) ||
        { ((10#$major == 10#$native_release_major && 10#$minor > 10#$native_release_minor)) ||
            { ((10#$major == 10#$native_release_major && 10#$minor == 10#$native_release_minor && 10#$patch >= 10#$native_release_patch)); }; }
}

catalogued_historical_version() {
    case "$1" in
        0.6.7|0.6.8|0.6.9|0.7.0|0.7.1|0.7.2|0.7.3|0.7.4)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

usage() {
    cat <<'EOF'
EggPool quick install (binary-first)

Usage:
    curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
    ./scripts/install.sh [options]

Options:
    --version X.Y.Z     Install that exact catalogued release (leading v is accepted)
    --upgrade           Install the latest stable release explicitly
    --force             Repair a verified EggPool installation without changing
                        owner; never overwrites an unrelated file
    --adopt-standalone  Explicitly migrate a standalone Rust binary to wheel management
    --package-manager uv|pipx|pip
                        Explicitly use a package-manager wheel path for a fresh
                        install instead of the default verified raw binary
    --help              Show this help

Default fresh current-native installs use the verified GitHub raw binary and
never invoke uv, pipx, pip, Python, Cargo, or a source build. Exact Rust
versions use the same raw authority. An explicitly requested catalogued
Python-era version uses the historical package compatibility path. Existing
installations retain their owner: native installs delegate to
`eggpool update`, legacy Python-era installs use their owning manager.
Source-checkout invocation installs the local checkout build and never
resolves the public package by accident.
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

if [[ -n "$INSTALL_INDEX_URL" || -n "$INSTALL_FIND_LINKS" ]]; then
    [[ "$ALLOW_NONPROD_INDEX" == "1" ]] ||
        fail "non-production package indexes require EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX=1"
    [[ "$INSTALL_INDEX_URL" != *$'\n'* && "$INSTALL_INDEX_URL" != *$'\r'* ]] ||
        fail "package index URL contains a newline"
    [[ "$INSTALL_FIND_LINKS" != *$'\n'* && "$INSTALL_FIND_LINKS" != *$'\r'* ]] ||
        fail "package find-links path contains a newline"
fi

# Release-origin guard: production must remain the default HTTPS authority.
# Any fixture/test override requires explicit opt-in and never persists.
if [[ "$RELEASE_BASE_URL" != "$DEFAULT_RELEASE_BASE_URL" ]]; then
    [[ "$ALLOW_NONPROD_ORIGIN" == "1" ]] ||
        fail "non-production release origin requires EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_ORIGIN=1"
    case "$RELEASE_BASE_URL" in
        https://*|http://127.0.0.1*|http://localhost*|file://*)
            ;;
        *)
            fail "non-production release origin is not an allowed test origin"
            ;;
    esac
    [[ "$RELEASE_BASE_URL" != *$'\n'* && "$RELEASE_BASE_URL" != *$'\r'* && "$RELEASE_BASE_URL" != *" "* ]] ||
        fail "release origin contains an unexpected separator"
fi

package_source_args() {
    local manager="$1"
    PACKAGE_SOURCE_ARGS=()
    if [[ -n "$INSTALL_FIND_LINKS" ]]; then
        case "$manager" in
            uv)
                PACKAGE_SOURCE_ARGS+=(--no-index --find-links "$INSTALL_FIND_LINKS")
                ;;
            pipx)
                PACKAGE_SOURCE_ARGS+=(--pip-args "--no-index --find-links $INSTALL_FIND_LINKS")
                ;;
            pip)
                PACKAGE_SOURCE_ARGS+=(--no-index --find-links "$INSTALL_FIND_LINKS")
                ;;
        esac
    elif [[ -n "$INSTALL_INDEX_URL" ]]; then
        case "$manager" in
            uv)
                PACKAGE_SOURCE_ARGS+=(--index "$INSTALL_INDEX_URL")
                ;;
            pipx)
                PACKAGE_SOURCE_ARGS+=(--index-url "$INSTALL_INDEX_URL")
                ;;
            pip)
                PACKAGE_SOURCE_ARGS+=(--index-url "$INSTALL_INDEX_URL")
                ;;
        esac
    fi
}

normalize_version() {
    local value="$1"
    value="${value#v}"
    value="${value#V}"
    if [[ ! "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        fail "invalid version '$1'; use X.Y.Z or vX.Y.Z"
    fi
    printf '%s' "$value"
}

normalize_package_manager() {
    local value="$1"
    case "$value" in
        uv|uv-tool)
            printf 'uv-tool'
            ;;
        pipx)
            printf 'pipx'
            ;;
        pip|pip-venv|venv)
            printf 'pip'
            ;;
        *)
            fail "invalid --package-manager '$1'; use uv, pipx, or pip"
            ;;
    esac
}

while (($#)); do
    case "$1" in
        --version)
            (($# >= 2)) || fail "--version requires X.Y.Z"
            TARGET_VERSION="$(normalize_version "$2")"
            VERSION_REQUESTED=1
            shift 2
            ;;
        --version=*)
            TARGET_VERSION="$(normalize_version "${1#*=}")"
            VERSION_REQUESTED=1
            shift
            ;;
        --upgrade)
            UPGRADE_ONLY=1
            shift
            ;;
        --force)
            FORCE_REINSTALL=1
            shift
            ;;
        --adopt-standalone)
            ADOPT_STANDALONE=1
            shift
            ;;
        --package-manager)
            (($# >= 2)) || fail "--package-manager requires uv, pipx, or pip"
            EXPLICIT_PACKAGE_MANAGER="$(normalize_package_manager "$2")"
            shift 2
            ;;
        --package-manager=*)
            EXPLICIT_PACKAGE_MANAGER="$(normalize_package_manager "${1#*=}")"
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if ((VERSION_REQUESTED)) && ! version_at_or_after_native_release "$TARGET_VERSION"; then
    catalogued_historical_version "$TARGET_VERSION" ||
        fail "requested historical version $TARGET_VERSION is not in the schema-compatible catalog"
fi

if [[ "$(id -u)" == 0 ]]; then
    fail "personal quick install refuses root; use the explicit system deployment command instead"
fi

HOST_OS="$(uname -s 2>/dev/null || true)"
HOST_ARCH="$(uname -m 2>/dev/null || true)"
TARGET_CLASS=""
RAW_OS=""
RAW_ARCH=""
case "$HOST_OS:$HOST_ARCH" in
    Linux:x86_64|Linux:amd64)
        TARGET_CLASS="linux-x86_64"
        RAW_OS="linux"
        RAW_ARCH="x86_64"
        ;;
    Linux:aarch64|Linux:arm64)
        TARGET_CLASS="linux-aarch64"
        RAW_OS="linux"
        RAW_ARCH="aarch64"
        ;;
    Darwin:arm64|Darwin:aarch64)
        TARGET_CLASS="macos-arm64"
        RAW_OS="macos"
        RAW_ARCH="aarch64"
        ;;
    *)
        fail "unsupported platform $HOST_OS/$HOST_ARCH; no supported raw binary is available"
        ;;
esac

raw_filename_for_version() {
    printf 'eggpool-%s-%s-%s' "$1" "$RAW_OS" "$RAW_ARCH"
}

# Release URL helpers (shell-native, no jq/Python).
release_sidecar_url_latest() {
    printf '%s/latest/download/SHA256SUMS' "${RELEASE_BASE_URL%/}"
}

release_sidecar_url_exact() {
    printf '%s/download/v%s/SHA256SUMS' "${RELEASE_BASE_URL%/}" "$1"
}

release_asset_url_pinned() {
    printf '%s/download/v%s/%s' "${RELEASE_BASE_URL%/}" "$1" "$2"
}

require_production_https() {
    local url="$1"
    if [[ "$RELEASE_BASE_URL" == "$DEFAULT_RELEASE_BASE_URL" ]]; then
        case "$url" in
            https://*)
                ;;
            *)
                fail "release URL must remain HTTPS in production"
                ;;
        esac
    fi
}

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" 2>/dev/null && pwd || true)"
SOURCE_CHECKOUT=0
PROJECT_DIR=""
if [[ -n "$SCRIPT_DIR" ]] && [[ -f "$SCRIPT_DIR/../rust/Cargo.toml" ]] && \
    [[ -f "$SCRIPT_DIR/../packaging/pypi/pyproject.toml" ]]; then
    SOURCE_CHECKOUT=1
    PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

if ((SOURCE_CHECKOUT)); then
    if ((VERSION_REQUESTED)); then
        SOURCE_VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$PROJECT_DIR/rust/Cargo.toml" | head -n 1)"
        [[ "$SOURCE_VERSION" == "$TARGET_VERSION" ]] || \
            fail "source checkout version is $SOURCE_VERSION, not requested $TARGET_VERSION"
    fi
    echo "Using source checkout: $PROJECT_DIR"
fi

EXISTING_BIN=""
if command -v eggpool >/dev/null 2>&1; then
    EXISTING_BIN="$(command -v eggpool)"
fi

PROVENANCE_KIND=""
PROVENANCE_PYTHON=""
PROVENANCE_VERSION=""
PROVENANCE_NATIVE=""
PROVENANCE_MANAGER=""
PROVENANCE_ENVIRONMENT=""
PROVENANCE_EVIDENCE=""

parse_provenance_report() {
    local key value
    PROVENANCE_KIND=""
    PROVENANCE_PYTHON=""
    PROVENANCE_VERSION=""
    PROVENANCE_NATIVE=""
    PROVENANCE_MANAGER=""
    PROVENANCE_ENVIRONMENT=""
    PROVENANCE_EVIDENCE=""
    while IFS=$'\t' read -r key value; do
        case "$key" in
            kind) PROVENANCE_KIND="$value" ;;
            python) PROVENANCE_PYTHON="$value" ;;
            version) PROVENANCE_VERSION="$value" ;;
            native) PROVENANCE_NATIVE="$value" ;;
            manager) PROVENANCE_MANAGER="$value" ;;
            environment) PROVENANCE_ENVIRONMENT="$value" ;;
            executable) : ;;
            evidence)
                if [[ -z "$PROVENANCE_EVIDENCE" ]]; then
                    PROVENANCE_EVIDENCE="$value"
                else
                    PROVENANCE_EVIDENCE="$PROVENANCE_EVIDENCE; $value"
                fi
                ;;
        esac
    done <<< "$1"
    [[ -n "$PROVENANCE_KIND" ]]
}

probe_python_provenance() {
    local executable="$1"
    local shebang python
    shebang="$(head -n 1 "$executable" 2>/dev/null || true)"
    [[ "$shebang" == '#!'* ]] || return 1
    python="${shebang#\#!}"
    python="${python%% *}"
    if [[ "$python" == */env ]]; then
        python="${shebang#*env }"
        python="${python%% *}"
        python="$(command -v "$python" 2>/dev/null || true)"
    fi
    [[ -x "$python" ]] || return 1

    local report
    report="$(PYTHONPATH= PYTHONNOUSERSITE=1 "$python" -c '
import importlib.metadata as metadata
import json
import pathlib
import sys

def safe(value):
    value = str(value)
    if any(char in value for char in "\\r\\n\\t"):
        return "-"
    return value[:160]

try:
    distribution = metadata.distribution("eggpool")
    distribution_path = pathlib.Path(distribution._path)
    environment = pathlib.Path(sys.prefix)
    installer_path = distribution_path / "INSTALLER"
    installer = installer_path.read_text(encoding="utf-8").strip().lower() if installer_path.is_file() else ""
    parts = {part.lower() for part in environment.parts}
    uv = "uv" in parts and "tools" in parts
    pipx = "pipx" in parts and bool({"venvs", "shared"} & parts)
    if uv and pipx or installer == "uv" and pipx or installer == "pipx" and uv:
        kind = "ambiguous"
    elif pipx:
        kind = "pipx"
    elif uv:
        kind = "uv-tool"
    elif (environment / "pyvenv.cfg").is_file():
        kind = "pip"
    else:
        kind = "ambiguous"
    direct_url = distribution_path / "direct_url.json"
    if direct_url.is_file():
        try:
            url = json.loads(direct_url.read_text(encoding="utf-8")).get("url", "")
            if isinstance(url, str) and url.startswith("file://"):
                source = pathlib.Path(url[7:])
                if any((ancestor / ".git").exists() for ancestor in source.parents):
                    kind = "source-checkout"
        except (OSError, ValueError, TypeError):
            kind = "ambiguous"
    print("kind\\t" + kind)
    print("python\\t" + safe(sys.executable))
    print("environment\\t" + safe(environment))
    print("version\\t" + safe(distribution.version))
    print("native\\tfalse")
    print("executable\\t" + safe(sys.argv[0]))
except Exception:
    print("kind\\tambiguous")
' 2>/dev/null)" || return 1
    parse_provenance_report "$report"
    [[ "$PROVENANCE_KIND" != ambiguous ]]
}

classify_existing_owner() {
    if [[ -z "$EXISTING_BIN" ]]; then
        PROVENANCE_KIND=""
        return 0
    fi
    echo "Inspecting existing eggpool install: $EXISTING_BIN"
    local rust_report=""
    if rust_report="$("$EXISTING_BIN" install-provenance --shell 2>/dev/null)" && \
        parse_provenance_report "$rust_report"; then
        :
    elif probe_python_provenance "$EXISTING_BIN"; then
        :
    else
        PROVENANCE_KIND="ambiguous"
    fi

    case "$PROVENANCE_KIND" in
        uv-tool|pipx|pip|standalone-rust)
            echo "  Existing owner: $PROVENANCE_KIND"
            ;;
        source-checkout)
            fail "existing eggpool belongs to a source checkout; use that checkout's documented developer flow"
            ;;
        ambiguous|*)
            if [[ -n "$PROVENANCE_EVIDENCE" ]]; then
                fail "existing eggpool ownership is ambiguous ($PROVENANCE_EVIDENCE); remove the collision explicitly or use a known manager"
            fi
            fail "existing eggpool ownership is ambiguous; remove the collision explicitly or use a known manager"
            ;;
    esac
}

find_uv() {
    command -v uv 2>/dev/null || true
}

find_pipx() {
    command -v pipx 2>/dev/null || true
}

find_curl() {
    command -v curl 2>/dev/null || true
}

# ---- lock, temp, and signal handling (WP-D) ----

INSTALL_STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/eggpool"
INSTALL_LOCK_DIR="$INSTALL_STATE_DIR/install.lock.d"
INSTALL_TMPDIR=""
INSTALL_DEST_DIR="${EGGPOOL_INSTALL_BIN_DIR:-$HOME/.local/bin}"
INSTALL_DEST="$INSTALL_DEST_DIR/eggpool"
LOCK_HELD=0

# Fresh-install transaction state (M002). The fresh transaction ends only
# after executable publication, provenance/version revalidation, and
# first-time config seeding. Rollback removes only the executable committed
# by this invocation when its identity still matches, plus a newly-created
# partial config only when safely attributable. It never deletes a raced
# replacement, a pre-existing config, or a symlink/special-file boundary.
FRESH_TX_ACTIVE=0
FRESH_TX_COMMITTED=0
FRESH_TX_EXPECTED_HASH=""
FRESH_TX_EXPECTED_VERSION=""
FRESH_TX_CONFIG=""
FRESH_TX_CONFIG_EXISTED=0
FRESH_TX_ROLLBACK_DONE=0

cleanup_install_temp() {
    if [[ -n "$INSTALL_TMPDIR" && -d "$INSTALL_TMPDIR" ]]; then
        rm -rf "$INSTALL_TMPDIR"
    fi
    # Never remove the installed command or user state here.
}

release_install_lock() {
    if ((LOCK_HELD)) && [[ -d "$INSTALL_LOCK_DIR" ]]; then
        rmdir "$INSTALL_LOCK_DIR" 2>/dev/null || true
    fi
    LOCK_HELD=0
}

# Guarded fresh-install rollback. Returns 0 when the executable committed by
# this transaction was removed (or was already absent) and any partial config
# was cleaned or was absent. Returns 1 when manual recovery is required
# (destination identity changed, unsafe config boundary, or removal failed).
# Idempotent via FRESH_TX_ROLLBACK_DONE; safe to call from traps.
fresh_tx_rollback_guarded() {
    local dest="${INSTALL_DEST}"
    local expected_hash="${FRESH_TX_EXPECTED_HASH:-}"
    local expected_version="${FRESH_TX_EXPECTED_VERSION:-}"
    local config_path="${FRESH_TX_CONFIG:-}"
    local config_existed="${FRESH_TX_CONFIG_EXISTED:-0}"
    local need_manual=0

    if ((FRESH_TX_ROLLBACK_DONE)); then
        return 0
    fi
    FRESH_TX_ROLLBACK_DONE=1

    if [[ -e "$dest" || -L "$dest" ]]; then
        if [[ -L "$dest" ]]; then
            need_manual=1
        elif [[ ! -f "$dest" ]]; then
            need_manual=1
        elif [[ -z "$expected_hash" ]]; then
            need_manual=1
        else
            local current_hash=""
            current_hash="$(hash_file_sha256 "$dest" 2>/dev/null || true)"
            if [[ -z "$current_hash" || "$current_hash" != "$expected_hash" ]]; then
                need_manual=1
            else
                local ver_out=""
                ver_out="$("$dest" version 2>/dev/null | head -n 1 | tr -d '\r' | sed -E 's/^[[:space:]]+//;s/[[:space:]]+$//' || true)"
                if [[ -n "$expected_version" && -n "$ver_out" && "$ver_out" != "$expected_version" ]]; then
                    need_manual=1
                else
                    rm -f "$dest" 2>/dev/null || need_manual=1
                    if [[ -e "$dest" ]]; then
                        need_manual=1
                    fi
                fi
            fi
        fi
    fi

    if [[ -n "$config_path" ]] && (( ! config_existed )); then
        if [[ -e "$config_path" || -L "$config_path" ]]; then
            if [[ -L "$config_path" ]]; then
                need_manual=1
            elif [[ -f "$config_path" ]]; then
                rm -f "$config_path" 2>/dev/null || need_manual=1
            else
                need_manual=1
            fi
        fi
    fi

    if ((need_manual)); then
        return 1
    fi
    return 0
}

install_trap_cleanup() {
    if ((FRESH_TX_ACTIVE)) && ((FRESH_TX_COMMITTED)) && (( ! FRESH_TX_ROLLBACK_DONE )); then
        fresh_tx_rollback_guarded >/dev/null 2>&1 || true
    fi
    cleanup_install_temp
    release_install_lock
}

fresh_tx_signal_handler() {
    if ((FRESH_TX_ACTIVE)) && ((FRESH_TX_COMMITTED)) && (( ! FRESH_TX_ROLLBACK_DONE )); then
        fresh_tx_rollback_guarded >/dev/null 2>&1 || true
    fi
    cleanup_install_temp
    release_install_lock
    exit 130
}

# Fail after fresh commit with transactional rollback. Attempts guarded
# removal of the executable committed by this invocation plus any partial
# config, then exits via fail() with diagnostics distinguishing rollback
# success from manual-recovery. Keeps the install lock held until rollback
# completes.
fail_fresh_tx() {
    local reason="$1"
    local cfg="${FRESH_TX_CONFIG:-unknown config path}"
    if fresh_tx_rollback_guarded; then
        FRESH_TX_ACTIVE=0
        release_install_lock
        cleanup_install_temp
        fail "$reason; fresh install was rolled back (executable and partial config removed)"
    else
        FRESH_TX_ACTIVE=0
        release_install_lock
        cleanup_install_temp
        fail "$reason; rollback could not be proven safe (destination may have changed) — manual recovery required: verify $INSTALL_DEST identity and remove any partial config at $cfg explicitly"
    fi
}

trap install_trap_cleanup EXIT
trap fresh_tx_signal_handler INT TERM HUP

acquire_install_lock() {
    mkdir -p "$INSTALL_STATE_DIR" 2>/dev/null || fail "could not create install state directory"
    if ! mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; then
        fail "another install or update is already in progress; retry after it completes"
    fi
    LOCK_HELD=1
}

make_install_tempdir() {
    INSTALL_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/eggpool-install.XXXXXX" 2>/dev/null)" || \
        fail "could not create a private install temp directory"
    chmod 700 "$INSTALL_TMPDIR" 2>/dev/null || true
}

# ---- shell-native release helpers (WP-A/B) ----

download_url_to_file() {
    local url="$1" dest="$2"
    local curl_bin
    curl_bin="$(find_curl)"
    [[ -n "$curl_bin" ]] || fail "curl is required for the verified raw-binary install"
    require_production_https "$url"
    # Production pins HTTPS; non-production test origins use the same strict
    # curl flags without a protocol pin so file/loopback fixtures can serve.
    if [[ "$RELEASE_BASE_URL" == "$DEFAULT_RELEASE_BASE_URL" ]]; then
        "$curl_bin" -fsSL --proto '=https' --tlsv1.2 "$url" -o "$dest" || return 1
    else
        "$curl_bin" -fsSL "$url" -o "$dest" || return 1
    fi
}

hash_file_sha256() {
    local file="$1" digest
    if command -v sha256sum >/dev/null 2>&1; then
        digest="$(sha256sum "$file" 2>/dev/null | awk '{print $1}')" || return 1
    elif command -v shasum >/dev/null 2>&1; then
        digest="$(shasum -a 256 "$file" 2>/dev/null | awk '{print $1}')" || return 1
    else
        fail "no SHA-256 tool is available (need sha256sum or shasum)"
    fi
    [[ "$digest" =~ ^[0-9a-fA-F]{64}$ ]] || return 1
    printf '%s' "$digest" | tr 'A-F' 'a-f'
}

file_size_bytes() {
    wc -c < "$1" 2>/dev/null | tr -d ' \t\r\n' || return 1
}

valid_digest_syntax() {
    [[ "$1" =~ ^[0-9a-fA-F]{64}$ ]]
}

valid_raw_filename_syntax() {
    local name="$1"
    # Exactly eggpool-X.Y.Z-os-arch, no path separators or whitespace.
    [[ "$name" != *"/"* && "$name" != *"\\"* && "$name" != *" "* && "$name" != *$'\t'* ]] || return 1
    [[ "$name" =~ ^eggpool-[0-9]+\.[0-9]+\.[0-9]+-(linux|macos)-(x86_64|aarch64)$ ]] || return 1
}

version_from_raw_filename() {
    local name="$1" rest
    rest="${name#eggpool-}"
    rest="${rest#v}"
    printf '%s' "$rest" | sed -E 's/^([0-9]+\.[0-9]+\.[0-9]+)-.*$/\1/'
}

# Select exactly one raw proxy candidate from a SHA256SUMS sidecar.
# Sets SELECTED_FILENAME / SELECTED_SHA256 / SELECTED_VERSION.
# Rejects wheel and eggpool-connect entries; fails closed on ambiguity.
select_raw_from_sidecar() {
    local sidecar="$1" mode="$2" wanted="$3"
    local matches=0 candidate_file="" candidate_hash="" candidate_version=""
    local line hash path base version
    SELECTED_FILENAME=""
    SELECTED_SHA256=""
    SELECTED_VERSION=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Strip CR, skip blanks.
        line="${line%$'\r'}"
        [[ -z "${line//[[:space:]]/}" ]] && continue
        # Strict "<64-hex><space><path>" split on first whitespace run.
        hash="${line%%[[:space:]]*}"
        path="${line#*[[:space:]]}"
        # Reject lines without a separator or with extra whitespace-only path.
        [[ "$hash" == "$line" ]] && fail "checksum sidecar is malformed"
        # Trim leading whitespace from path.
        path="${path#"${path%%[![:space:]]*}"}"
        [[ -z "$path" ]] && fail "checksum sidecar is malformed"
        # Path must not contain whitespace (fail closed on ambiguous parsing).
        case "$path" in
            *\ *|*$'\t'*|*$'\r'*|*$'\n'*)
                fail "checksum sidecar is malformed"
                ;;
        esac
        valid_digest_syntax "$hash" || fail "checksum sidecar contains a malformed digest"
        base="${path##*/}"
        # Never accept wheel or helper entries as proxy candidates.
        case "$base" in
            *.whl|*.tar.gz|*.zip|eggpool-connect*|*.sh|*.ps1|*manifest.json|SHA256SUMS)
                continue
                ;;
        esac
        valid_raw_filename_syntax "$base" || continue
        # Must match this platform's os/arch suffix.
        case "$base" in
            *-"$RAW_OS"-"$RAW_ARCH")
                ;;
            *)
                continue
                ;;
        esac
        version="$(version_from_raw_filename "$base")"
        [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
        if [[ "$mode" == "exact" ]]; then
            [[ "$base" == "$wanted" ]] || continue
        fi
        matches=$((matches + 1))
        candidate_file="$base"
        # Normalize digest to lowercase.
        candidate_hash="$(printf '%s' "$hash" | tr 'A-F' 'a-f')"
        candidate_version="$version"
        if ((matches > 1)); then
            fail "checksum sidecar is ambiguous (multiple raw entries for this target)"
        fi
    done < "$sidecar"
    ((matches == 1)) || {
        if ((matches == 0)); then
            fail "checksum sidecar has no matching raw entry for this target"
        else
            fail "checksum sidecar is ambiguous (multiple raw entries for this target)"
        fi
    }
    SELECTED_FILENAME="$candidate_file"
    SELECTED_SHA256="$candidate_hash"
    SELECTED_VERSION="$candidate_version"
    if [[ "$mode" == "exact" ]]; then
        [[ "$SELECTED_VERSION" == "$TARGET_VERSION" ]] || \
            fail "checksum sidecar version does not match the requested version"
        [[ "$SELECTED_FILENAME" == "$wanted" ]] || \
            fail "checksum sidecar filename does not match the requested version"
    fi
}

verify_staged_candidate() {
    local staged="$1" expected_version="$2" expected_hash="$3"
    local size actual output report
    local saved_kind saved_version saved_native saved_python saved_manager saved_env saved_ev
    size="$(file_size_bytes "$staged")" || fail "could not measure the downloaded artifact"
    [[ "$size" -gt 0 ]] || fail "downloaded artifact is empty"
    ((size <= MAX_ARTIFACT_BYTES)) || fail "downloaded artifact exceeds the 128 MiB bound"
    actual="$(hash_file_sha256 "$staged")" || fail "downloaded artifact hash verification failed"
    [[ "$actual" == "$expected_hash" ]] || fail "downloaded artifact checksum mismatch"
    chmod +x "$staged" 2>/dev/null || fail "could not make the staged candidate executable"
    output="$("$staged" version 2>/dev/null | head -n 1 | tr -d '\r')" || \
        fail "staged candidate version self-check failed"
    # Trim surrounding whitespace.
    output="$(printf '%s' "$output" | sed -E 's/^[[:space:]]+//;s/[[:space:]]+$//')"
    [[ -n "$output" ]] || fail "staged candidate returned an empty version"
    [[ "$output" == "$expected_version" ]] || \
        fail "staged candidate reports wrong version (got $output, expected $expected_version)"
    report="$("$staged" install-provenance --shell 2>/dev/null)" || \
        fail "staged candidate identity check failed"
    saved_kind="$PROVENANCE_KIND"
    saved_version="$PROVENANCE_VERSION"
    saved_native="$PROVENANCE_NATIVE"
    saved_python="$PROVENANCE_PYTHON"
    saved_manager="$PROVENANCE_MANAGER"
    saved_env="$PROVENANCE_ENVIRONMENT"
    saved_ev="$PROVENANCE_EVIDENCE"
    parse_provenance_report "$report" || fail "staged candidate is not recognized as native EggPool"
    [[ "$PROVENANCE_KIND" == "standalone-rust" ]] || \
        fail "staged candidate is not a standalone Rust executable"
    [[ "$PROVENANCE_NATIVE" == "true" ]] || \
        fail "staged candidate is not the native Rust release"
    [[ "$PROVENANCE_VERSION" == "$expected_version" ]] || \
        fail "staged candidate provenance version mismatch"
    PROVENANCE_KIND="$saved_kind"
    PROVENANCE_VERSION="$saved_version"
    PROVENANCE_NATIVE="$saved_native"
    PROVENANCE_PYTHON="$saved_python"
    PROVENANCE_MANAGER="$saved_manager"
    PROVENANCE_ENVIRONMENT="$saved_env"
    PROVENANCE_EVIDENCE="$saved_ev"
}

refuse_dest_collision() {
    local dest="$1"
    if [[ -L "$dest" ]]; then
        fail "destination $dest is a symlink; refusing to change which eggpool command wins on PATH"
    fi
    if [[ -e "$dest" && ! -f "$dest" ]]; then
        fail "destination $dest is not a regular file; refusing to replace a special file"
    fi
}

same_path() {
    local left="$1" right="$2"
    [[ "$left" == "$right" ]] && return 0
    [[ -e "$left" && -e "$right" ]] || return 1
    [[ "$(realpath "$left" 2>/dev/null || readlink -f "$left" 2>/dev/null || printf '%s' "$left")" == "$(realpath "$right" 2>/dev/null || readlink -f "$right" 2>/dev/null || printf '%s' "$right")" ]]
}

# Fresh verified raw-binary install (WP-B). No Python/manager on this path.
# M002: `--force` is a verified-repair switch, never permission to overwrite
# an unowned file. A fresh destination that already exists is always a
# collision, even with `--force`, because no EggPool owner was classified.
install_fresh_raw_binary() {
    local requested="$1" # "latest" or exact X.Y.Z
    local sidecar_url sidecar_file asset_url asset_file
    local wanted=""
    acquire_install_lock
    make_install_tempdir
    mkdir -p "$INSTALL_DEST_DIR" 2>/dev/null || fail "could not create $INSTALL_DEST_DIR"
    refuse_dest_collision "$INSTALL_DEST"
    if [[ -e "$INSTALL_DEST" || -L "$INSTALL_DEST" ]]; then
        fail "destination $INSTALL_DEST already exists and is not a verified EggPool installation; --force repairs a verified EggPool installation and cannot overwrite an unrelated file (remove it explicitly to proceed)"
    fi
    # Revalidate: no existing command may have appeared after preflight.
    if command -v eggpool >/dev/null 2>&1; then
        fail "an eggpool command appeared during install preflight; rerun to classify its owner"
    fi
    # Arm the fresh transaction before any mutation. The EXIT/signal traps
    # use this state for guarded rollback; success disarms it below.
    FRESH_TX_ACTIVE=1
    FRESH_TX_COMMITTED=0
    FRESH_TX_EXPECTED_HASH=""
    FRESH_TX_EXPECTED_VERSION=""
    FRESH_TX_CONFIG=""
    FRESH_TX_CONFIG_EXISTED=0
    FRESH_TX_ROLLBACK_DONE=0
    sidecar_file="$INSTALL_TMPDIR/SHA256SUMS"
    asset_file="$INSTALL_TMPDIR/eggpool-candidate"
    if [[ "$requested" == "latest" ]]; then
        sidecar_url="$(release_sidecar_url_latest)"
        download_url_to_file "$sidecar_url" "$sidecar_file" || \
            fail "could not download the release checksum sidecar"
        select_raw_from_sidecar "$sidecar_file" "latest" ""
        # Bind the asset fetch to the version parsed from the sidecar so a
        # moving latest pointer cannot mix checksum and binary releases.
        asset_url="$(release_asset_url_pinned "$SELECTED_VERSION" "$SELECTED_FILENAME")"
    else
        wanted="$(raw_filename_for_version "$requested")"
        sidecar_url="$(release_sidecar_url_exact "$requested")"
        download_url_to_file "$sidecar_url" "$sidecar_file" || \
            fail "could not download the release checksum sidecar for $requested"
        select_raw_from_sidecar "$sidecar_file" "exact" "$wanted"
        asset_url="$(release_asset_url_pinned "$SELECTED_VERSION" "$SELECTED_FILENAME")"
    fi
    download_url_to_file "$asset_url" "$asset_file" || \
        fail "could not download the raw release asset"
    verify_staged_candidate "$asset_file" "$SELECTED_VERSION" "$SELECTED_SHA256"
    # Same-filesystem staged commit for atomic rename.
    local staged_samefs
    staged_samefs="$(mktemp "$INSTALL_DEST_DIR/.eggpool-install.XXXXXX" 2>/dev/null)" || \
        fail "could not stage the verified candidate"
    chmod 700 "$staged_samefs" 2>/dev/null || true
    rm -f "$staged_samefs"
    cp "$asset_file" "$staged_samefs" || fail "could not stage the verified candidate"
    chmod 755 "$staged_samefs" || fail "could not stage the verified candidate"
    # Race-proof revalidation before atomic commit. Fresh never replaces an
    # existing destination, even with --force.
    refuse_dest_collision "$INSTALL_DEST"
    if [[ -e "$INSTALL_DEST" || -L "$INSTALL_DEST" ]]; then
        rm -f "$staged_samefs"
        fail "destination $INSTALL_DEST changed during install; refusing to overwrite an unverified file (remove it explicitly to proceed)"
    fi
    if command -v eggpool >/dev/null 2>&1; then
        rm -f "$staged_samefs"
        fail "an eggpool command appeared during install; rerun to classify its owner"
    fi
    # Record transaction identity before commit: expected hash/version plus
    # config existence. Rollback revalidates hash/version before deleting.
    FRESH_TX_EXPECTED_HASH="$SELECTED_SHA256"
    FRESH_TX_EXPECTED_VERSION="$SELECTED_VERSION"
    local fresh_config_path="${EGGPOOL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/eggpool/config.toml}"
    FRESH_TX_CONFIG="$fresh_config_path"
    if [[ -f "$fresh_config_path" ]]; then
        FRESH_TX_CONFIG_EXISTED=1
    else
        FRESH_TX_CONFIG_EXISTED=0
    fi
    mv "$staged_samefs" "$INSTALL_DEST" || {
        rm -f "$staged_samefs"
        fail "could not commit the verified executable"
    }
    chmod 755 "$INSTALL_DEST" 2>/dev/null || true
    FRESH_TX_COMMITTED=1
    export PATH="$INSTALL_DEST_DIR:$PATH"
    local active
    active="$(command -v eggpool 2>/dev/null || true)"
    if [[ -z "$active" ]]; then
        fail_fresh_tx "installed eggpool command is not on PATH"
    fi
    if ! same_path "$active" "$INSTALL_DEST"; then
        fail_fresh_tx "installed eggpool at an unexpected PATH location; refusing a silent command collision"
    fi
    local report cli_version
    if report="$("$active" install-provenance --shell 2>/dev/null)" && parse_provenance_report "$report"; then
        :
    else
        fail_fresh_tx "installed command did not provide verifiable native provenance"
    fi
    if [[ "$PROVENANCE_KIND" != "standalone-rust" ]]; then
        fail_fresh_tx "installed command is owned by $PROVENANCE_KIND, expected standalone-rust"
    fi
    if [[ "$PROVENANCE_NATIVE" != "true" ]]; then
        fail_fresh_tx "installed command is not the native Rust release"
    fi
    if [[ "$PROVENANCE_VERSION" != "$SELECTED_VERSION" ]]; then
        fail_fresh_tx "installed version is ${PROVENANCE_VERSION:-unknown}, expected $SELECTED_VERSION"
    fi
    cli_version="$("$active" version 2>/dev/null | head -n 1 | tr -d '\r' | sed -E 's/^[[:space:]]+//;s/[[:space:]]+$//')" || \
        fail_fresh_tx "installed eggpool version check failed"
    if [[ -z "$cli_version" ]]; then
        fail_fresh_tx "installed eggpool returned an empty version"
    fi
    # Transactional first-time config seeding. Existing config means no write.
    # Failure after commit rolls back the executable committed by this
    # invocation plus any partial config it created; a raced replacement is
    # never deleted.
    if [[ -f "$fresh_config_path" ]]; then
        echo "Preserved existing config: $fresh_config_path"
    else
        if "$active" init-config "$fresh_config_path"; then
            echo "Created $fresh_config_path from the installed package's canonical template."
        else
            fail_fresh_tx "could not seed the missing config at $fresh_config_path (init-config failed)"
        fi
    fi
    print_binary_next_steps "$cli_version" "$SELECTED_VERSION" "$active"
    # Disarm the transaction on success; the install is complete.
    FRESH_TX_ACTIVE=0
    FRESH_TX_COMMITTED=0
    release_install_lock
    cleanup_install_temp
}

seed_config_after_commit() {
    # Explicit result/ownership contract (M002 §6.3):
    # - preserved-existing when config existed before invocation;
    # - created-successfully on first-time seed success;
    # - failed-with-no-created-file vs failed-with-new-partial-file on error.
    # A new partial file is removed only when this invocation can prove it
    # created it (did not exist at preflight, still a regular file, never a
    # symlink/special boundary). A pre-existing config is never removed.
    local active="$1"
    local config_path="${EGGPOOL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/eggpool/config.toml}"
    local existed=0
    if [[ -f "$config_path" ]]; then
        existed=1
    fi
    if ((existed)); then
        echo "Preserved existing config: $config_path"
        return 0
    fi
    if "$active" init-config "$config_path"; then
        echo "Created $config_path from the installed package's canonical template."
        return 0
    fi
    if [[ -L "$config_path" ]]; then
        fail "could not seed the missing config at $config_path; partial config could not be proven safe (symlink boundary) — manual recovery required"
    fi
    if [[ -f "$config_path" ]]; then
        rm -f "$config_path" 2>/dev/null || true
    elif [[ -e "$config_path" ]]; then
        fail "could not seed the missing config at $config_path; partial config could not be proven safe (special-file boundary) — manual recovery required"
    fi
    fail "could not seed the missing config at $config_path"
}

print_binary_next_steps() {
    local cli_version="$1" selected="$2" active="$3"
    local config_path="${EGGPOOL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/eggpool/config.toml}"
    echo ""
    echo "Installation complete."
    echo "  Version: $cli_version"
    echo "  Authority: verified GitHub raw binary ($selected)"
    echo "  Manager: standalone-rust"
    echo "  Command: $active"
    echo "  Config:  $config_path"
    echo ""
    echo "Next steps:"
    echo "  eggpool onboard"
    echo "  eggpool --config $config_path check-config"
    echo "  sudo env \"PATH=\$PATH\" \"\$(command -v eggpool)\" deploy systemd --install"
    echo "  eggpool deploy cron --install        # when systemd is unavailable"
    echo "  eggpool update"
}

# Standalone repair via the same verified raw authority (used for --force
# when the native updater reports a no-op or fails after mutation).
repair_standalone_via_raw() {
    local target_version="$1" existing="$2"
    local sidecar_url sidecar_file asset_file wanted
    acquire_install_lock
    make_install_tempdir
    refuse_dest_collision "$existing"
    [[ -f "$existing" ]] || fail "standalone executable is missing; cannot repair"
    local old_version="${PROVENANCE_VERSION:-unknown}"
    local backup="${existing}.eggpool-standalone-${old_version}.rollback"
    [[ ! -e "$backup" ]] || fail "standalone rollback name already exists: $backup"
    local was_running=0
    if "$existing" runtime-status --json >/dev/null 2>&1; then
        was_running=1
        "$existing" stop >/dev/null 2>&1 || fail "could not stop the running standalone service; no files were changed"
    fi
    # Restore helper on failure.
    standalone_restore() {
        if [[ -e "$backup" && ! -e "$existing" ]]; then
            mv "$backup" "$existing" 2>/dev/null || true
        fi
        if ((was_running)) && [[ -x "$existing" ]]; then
            PATH="$(dirname "$existing"):$PATH" "$existing" restart >/dev/null 2>&1 || true
        fi
    }
    wanted="$(raw_filename_for_version "$target_version")"
    sidecar_url="$(release_sidecar_url_exact "$target_version")"
    sidecar_file="$INSTALL_TMPDIR/SHA256SUMS"
    asset_file="$INSTALL_TMPDIR/eggpool-candidate"
    if ! download_url_to_file "$sidecar_url" "$sidecar_file"; then
        ((was_running)) && PATH="$(dirname "$existing"):$PATH" "$existing" restart >/dev/null 2>&1 || true
        release_install_lock; cleanup_install_temp
        fail "could not download the release checksum sidecar for $target_version"
    fi
    if ! select_raw_from_sidecar "$sidecar_file" "exact" "$wanted"; then
        ((was_running)) && PATH="$(dirname "$existing"):$PATH" "$existing" restart >/dev/null 2>&1 || true
        release_install_lock; cleanup_install_temp
        fail "checksum sidecar has no matching raw entry for $target_version"
    fi
    local asset_url
    asset_url="$(release_asset_url_pinned "$SELECTED_VERSION" "$SELECTED_FILENAME")"
    if ! download_url_to_file "$asset_url" "$asset_file"; then
        ((was_running)) && PATH="$(dirname "$existing"):$PATH" "$existing" restart >/dev/null 2>&1 || true
        release_install_lock; cleanup_install_temp
        fail "could not download the raw release asset"
    fi
    if ! verify_staged_candidate "$asset_file" "$SELECTED_VERSION" "$SELECTED_SHA256"; then
        ((was_running)) && PATH="$(dirname "$existing"):$PATH" "$existing" restart >/dev/null 2>&1 || true
        release_install_lock; cleanup_install_temp
        fail "staged candidate verification failed; previous standalone command was left untouched"
    fi
    mv "$existing" "$backup" || {
        ((was_running)) && PATH="$(dirname "$existing"):$PATH" "$existing" restart >/dev/null 2>&1 || true
        release_install_lock; cleanup_install_temp
        fail "could not stage the standalone binary for rollback"
    }
    local staged_samefs
    staged_samefs="$(mktemp "$(dirname "$existing")/.eggpool-repair.XXXXXX" 2>/dev/null)" || {
        standalone_restore
        release_install_lock; cleanup_install_temp
        fail "could not stage the verified candidate"
    }
    rm -f "$staged_samefs"
    if ! cp "$asset_file" "$staged_samefs"; then
        standalone_restore
        rm -f "$staged_samefs"
        release_install_lock; cleanup_install_temp
        fail "could not stage the verified candidate"
    fi
    chmod 755 "$staged_samefs" || {
        standalone_restore
        rm -f "$staged_samefs"
        release_install_lock; cleanup_install_temp
        fail "could not stage the verified candidate"
    }
    if ! mv "$staged_samefs" "$existing"; then
        standalone_restore
        rm -f "$staged_samefs"
        release_install_lock; cleanup_install_temp
        fail "could not commit the verified executable; previous standalone command was restored when applicable"
    fi
    local report
    if ! report="$("$existing" install-provenance --shell 2>/dev/null)" || ! parse_provenance_report "$report"; then
        standalone_restore
        release_install_lock; cleanup_install_temp
        fail "repaired command did not provide verifiable native provenance; previous command was restored"
    fi
    if [[ "$PROVENANCE_KIND" != "standalone-rust" || "$PROVENANCE_VERSION" != "$SELECTED_VERSION" ]]; then
        standalone_restore
        release_install_lock; cleanup_install_temp
        fail "repaired command ownership or version mismatch; previous command was restored"
    fi
    if ((was_running)); then
        PATH="$(dirname "$existing"):$PATH" "$existing" restart >/dev/null 2>&1 || {
            standalone_restore
            release_install_lock; cleanup_install_temp
            fail "repaired install could not restart the service; previous standalone command was restored when applicable"
        }
    fi
    seed_config_after_commit "$existing"
    local cli_version
    cli_version="$("$existing" version 2>/dev/null | head -n 1 | tr -d '\r')" || cli_version="$SELECTED_VERSION"
    print_binary_next_steps "$cli_version" "$SELECTED_VERSION" "$existing"
    if [[ -e "$backup" ]]; then
        echo "  Standalone rollback retained at: $backup"
    fi
    release_install_lock
    cleanup_install_temp
}

# Existing native delegation (WP-C): use the installed binary's own updater
# so manager/standalone authority, locking, restart, and rollback are not
# duplicated in shell.
delegate_existing_native_update() {
    local existing="$1"
    local update_args=()
    if ((VERSION_REQUESTED)); then
        # Standalone cannot downgrade to a Python-era target.
        if [[ "$PROVENANCE_KIND" == "standalone-rust" ]] && ! version_at_or_after_native_release "$TARGET_VERSION"; then
            fail "standalone installs cannot downgrade directly to historical Python-era release $TARGET_VERSION"
        fi
        update_args+=("$TARGET_VERSION")
    fi
    echo "Delegating to the existing native updater: $existing update ${update_args[*]:-<latest>}"
    if (( ${#update_args[@]} )); then
        if "$existing" update "${update_args[@]}"; then
            :
        else
            local status=$?
            if ((FORCE_REINSTALL)) && [[ "$PROVENANCE_KIND" == "standalone-rust" ]]; then
                echo "Native updater reported no change; performing verified standalone repair..." >&2
                local repair_target
                if ((VERSION_REQUESTED)); then
                    repair_target="$TARGET_VERSION"
                else
                    repair_target="${PROVENANCE_VERSION:-}"
                    [[ -n "$repair_target" ]] || fail "native update failed and no repair target is known"
                fi
                repair_standalone_via_raw "$repair_target" "$existing"
                return 0
            fi
            fail "native update failed (exit $status); previous installation was left untouched by the updater"
        fi
    else
        if "$existing" update; then
            :
        else
            local status=$?
            if ((FORCE_REINSTALL)) && [[ "$PROVENANCE_KIND" == "standalone-rust" ]]; then
                echo "Native updater reported no change; performing verified standalone repair..." >&2
                local repair_target="${PROVENANCE_VERSION:-}"
                [[ -n "$repair_target" ]] || fail "native update failed and no repair target is known"
                repair_standalone_via_raw "$repair_target" "$existing"
                return 0
            fi
            fail "native update failed (exit $status); previous installation was left untouched by the updater"
        fi
    fi
    # Verify resulting ownership/version without mutating config first.
    local report
    if report="$("$existing" install-provenance --shell 2>/dev/null)" && parse_provenance_report "$report"; then
        :
    elif probe_python_provenance "$existing"; then
        :
    else
        fail "updated command did not provide verifiable provenance"
    fi
    # Owner must not silently change (except explicit --adopt-standalone
    # migration, handled on a separate authority).
    # NOTE: $PROVENANCE_KIND was overwritten by the post-update report; the
    # pre-update kind is captured by the caller. This check is performed by
    # the caller via EXPECTED_OWNER.
    if ((VERSION_REQUESTED)) && [[ -n "${PROVENANCE_VERSION:-}" && "$PROVENANCE_VERSION" != "$TARGET_VERSION" ]]; then
        # The native updater treats same-version as a no-op; a different
        # reported version after an exact request is a failure.
        # For manager owners the updater may already guarantee this; keep the
        # check strict for standalone and lenient logging for managers is
        # handled by the updater itself. Fail closed here.
        fail "updated version is ${PROVENANCE_VERSION:-unknown}, expected $TARGET_VERSION"
    fi
    local cli_version
    cli_version="$("$existing" version 2>/dev/null | head -n 1 | tr -d '\r')" || \
        fail "updated eggpool version check failed"
    [[ -n "$cli_version" ]] || fail "updated eggpool returned an empty version"
    seed_config_after_commit "$existing"
    local config_path="${EGGPOOL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/eggpool/config.toml}"
    echo ""
    echo "Update complete."
    echo "  Version: $cli_version"
    echo "  Manager: $PROVENANCE_KIND"
    echo "  Command: $existing"
    echo "  Config:  $config_path"
    echo ""
    echo "Next steps:"
    echo "  eggpool onboard"
    echo "  eggpool --config $config_path check-config"
}

check_historical_python_compat() {
    local target="$1"
    local python_bin=""
    if command -v python3 >/dev/null 2>&1; then
        python_bin="$(command -v python3)"
    else
        fail "historical Python-era target $target requires Python >=3.11, but no python3 was found"
    fi
    local version_text
    version_text="$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)" || \
        fail "historical Python-era target $target requires Python >=3.11, but the interpreter could not be probed"
    local major minor
    IFS=. read -r major minor <<< "$version_text"
    if ((10#$major < 3 || (10#$major == 3 && 10#$minor < 11))); then
        fail "historical Python-era target $target requires Python >=3.11 (found $version_text); use a current Rust release for a dependency-free install"
    fi
}

# ---- package-manager flows (historical/explicit/legacy/adoption) ----
# Preserves the bounded manager-detection compatibility logic for explicit
# historical targets, explicit --package-manager fresh installs, legacy
# Python-era existing installs, and explicit standalone adoption.

run_package_authority() {
    local manager_kind="$1" package_spec="$2" manager_force="$3"
    local manager manager_bin_dir expected_bin
    local package_command=()
    UV="$(find_uv)"
    PIPX="$(find_pipx)"
    case "$manager_kind" in
        uv-tool)
            [[ -x "${PROVENANCE_MANAGER:-$UV}" || -x "$UV" ]] || fail "the existing uv tool owner is unavailable; restore uv and rerun"
            if [[ -n "${PROVENANCE_MANAGER:-}" && -x "$PROVENANCE_MANAGER" ]]; then
                UV="$PROVENANCE_MANAGER"
            fi
            manager="uv"
            manager_bin_dir="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
            ;;
        pipx)
            [[ -x "${PROVENANCE_MANAGER:-$PIPX}" || -x "$PIPX" ]] || fail "the existing pipx owner is unavailable; restore pipx and rerun"
            if [[ -n "${PROVENANCE_MANAGER:-}" && -x "$PROVENANCE_MANAGER" ]]; then
                PIPX="$PROVENANCE_MANAGER"
            fi
            manager="pipx"
            manager_bin_dir="${PIPX_BIN_DIR:-$HOME/.local/bin}"
            ;;
        pip)
            manager="pip"
            [[ -x "$PROVENANCE_PYTHON" ]] || fail "the owning Python interpreter is unavailable; repair the environment with its manager"
            manager_bin_dir="$(dirname "$EXISTING_BIN")"
            ;;
        *)
            fail "unsupported package authority $manager_kind"
            ;;
    esac

    # Collision preflight for manager bin (non-pip).
    if [[ "$manager_kind" == "pip" ]]; then
        expected_bin="$EXISTING_BIN"
    else
        expected_bin="$manager_bin_dir/eggpool"
        if [[ -e "$expected_bin" ]]; then
            local collision=1
            if [[ -n "$EXISTING_BIN" ]] && same_path "$expected_bin" "$EXISTING_BIN"; then
                collision=0
            fi
            if ((collision)); then
                fail "manager bin collision at $expected_bin; refusing to change which eggpool command wins on PATH"
            fi
        fi
    fi

    # Standalone backup when migrating via adoption.
    local old_standalone="" standalone_backup="" was_running=0
    if [[ "$PROVENANCE_KIND" == "standalone-rust" ]]; then
        old_standalone="$EXISTING_BIN"
        if "$EXISTING_BIN" runtime-status --json >/dev/null 2>&1; then
            was_running=1
            "$EXISTING_BIN" stop >/dev/null 2>&1 || fail "could not stop the running standalone service; no files were changed"
        fi
        local standalone_version="${PROVENANCE_VERSION:-unknown}"
        standalone_backup="${old_standalone}.eggpool-standalone-${standalone_version}.rollback"
        [[ ! -e "$standalone_backup" ]] || fail "standalone rollback name already exists: $standalone_backup"
        mv "$old_standalone" "$standalone_backup" || fail "could not stage the standalone binary for rollback"
    fi

    restore_standalone_fn() {
        if [[ -n "$standalone_backup" && -e "$standalone_backup" && ! -e "$old_standalone" ]]; then
            mv "$standalone_backup" "$old_standalone"
        fi
        if ((was_running)) && [[ -n "$old_standalone" && -x "$old_standalone" ]]; then
            PATH="$(dirname "$old_standalone"):$PATH" "$old_standalone" restart >/dev/null 2>&1 || true
        fi
    }

    echo "Installing EggPool through $manager_kind..."
    local install_ok=1
    PACKAGE_SOURCE_ARGS=()
    case "$manager_kind" in
        uv-tool) package_source_args uv ;;
        pipx) package_source_args pipx ;;
        pip) package_source_args pip ;;
    esac
    case "$manager_kind" in
        uv-tool)
            if [[ "$manager" == "uv" ]]; then
                UV_BIN_USED="${UV:-$(find_uv)}"
                [[ -n "$UV_BIN_USED" ]] || { restore_standalone_fn; fail "uv is not available"; }
                package_command=("$UV_BIN_USED" tool install)
                if ((manager_force)); then
                    package_command+=(--force)
                fi
                if ((${#PACKAGE_SOURCE_ARGS[@]})); then
                    package_command+=("${PACKAGE_SOURCE_ARGS[@]}")
                fi
                package_command+=("$package_spec")
                if ! "${package_command[@]}"; then install_ok=0; fi
            fi
            ;;
        pipx)
            PIPX_BIN_USED="${PIPX:-$(find_pipx)}"
            [[ -n "$PIPX_BIN_USED" ]] || { restore_standalone_fn; fail "pipx is not available"; }
            package_command=("$PIPX_BIN_USED" install)
            if ((manager_force)); then
                package_command+=(--force)
            fi
            if ((${#PACKAGE_SOURCE_ARGS[@]})); then
                package_command+=("${PACKAGE_SOURCE_ARGS[@]}")
            fi
            package_command+=("$package_spec")
            if ! "${package_command[@]}"; then install_ok=0; fi
            ;;
        pip)
            package_command=("$PROVENANCE_PYTHON" -m pip install --upgrade --force-reinstall)
            if ((${#PACKAGE_SOURCE_ARGS[@]})); then
                package_command+=("${PACKAGE_SOURCE_ARGS[@]}")
            fi
            package_command+=("$package_spec")
            if ! "${package_command[@]}"; then install_ok=0; fi
            ;;
    esac
    if (( ! install_ok )); then
        restore_standalone_fn
        fail "package-manager installation failed; previous standalone command was restored when applicable"
    fi

    if [[ "$manager_kind" != pip ]]; then
        export PATH="$manager_bin_dir:$PATH"
    fi
    local active_bin
    active_bin="$(command -v eggpool 2>/dev/null || true)"
    if [[ "$manager_kind" != pip ]] && ! same_path "$active_bin" "$expected_bin"; then
        restore_standalone_fn
        fail "manager installed eggpool at an unexpected PATH location; refusing a silent command collision"
    fi
    [[ -n "$active_bin" && -x "$active_bin" ]] || {
        restore_standalone_fn
        fail "installed eggpool command is not executable; previous standalone command was restored when applicable"
    }

    local report native_required=1
    if ((VERSION_REQUESTED)) && ! version_at_or_after_native_release "$TARGET_VERSION"; then
        native_required=0
    fi
    if report="$("$active_bin" install-provenance --shell 2>/dev/null)" && parse_provenance_report "$report"; then
        :
    elif ! probe_python_provenance "$active_bin"; then
        restore_standalone_fn
        fail "installed command did not provide verifiable package provenance"
    fi
    [[ "$PROVENANCE_KIND" == "$manager_kind" ]] || {
        restore_standalone_fn
        fail "installed command is owned by $PROVENANCE_KIND, expected $manager_kind"
    }
    if ((native_required)) && [[ "$PROVENANCE_NATIVE" != true ]]; then
        # For Rust-era wheels the installed command must be native. Legacy
        # interpreters report native=false only for historical targets where
        # native_required was cleared above.
        # Python-fallback probing reports native=false; re-check via Rust
        # provenance when available.
        if report="$("$active_bin" install-provenance --shell 2>/dev/null)" && parse_provenance_report "$report"; then
            [[ "$PROVENANCE_NATIVE" == "true" ]] || {
                restore_standalone_fn
                fail "installed command is not the native Rust release wheel owned by $manager_kind"
            }
        else
            restore_standalone_fn
            fail "installed command is not the native Rust release wheel owned by $manager_kind"
        fi
    fi
    if ((VERSION_REQUESTED)) && [[ "$PROVENANCE_VERSION" != "$TARGET_VERSION" ]]; then
        restore_standalone_fn
        fail "installed version is ${PROVENANCE_VERSION:-unknown}, expected $TARGET_VERSION"
    fi
    local cli_version
    cli_version="$("$active_bin" version 2>/dev/null | head -n 1 | tr -d '\r')" || {
        restore_standalone_fn
        fail "installed eggpool version check failed"
    }
    [[ -n "$cli_version" ]] || {
        restore_standalone_fn
        fail "installed eggpool returned an empty version"
    }

    local config_path="${EGGPOOL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/eggpool/config.toml}"
    if [[ ! -f "$config_path" ]]; then
        "$active_bin" init-config "$config_path" || {
            restore_standalone_fn
            fail "could not seed the missing config at $config_path"
        }
        echo "Created $config_path from the installed package's canonical template."
    else
        echo "Preserved existing config: $config_path"
    fi

    if ((was_running)); then
        PATH="$manager_bin_dir:$PATH" "$active_bin" restart >/dev/null 2>&1 || {
            restore_standalone_fn
            fail "new install could not restart the service; previous standalone command was restored when applicable"
        }
    fi

    echo ""
    echo "Installation complete."
    echo "  Version: $cli_version"
    echo "  Manager: $manager_kind"
    echo "  Command: $active_bin"
    echo "  Config:  $config_path"
    if [[ -n "$standalone_backup" && -e "$standalone_backup" ]]; then
        echo "  Standalone rollback retained at: $standalone_backup"
    fi
    echo ""
    echo "Next steps:"
    echo "  eggpool onboard"
    echo "  eggpool --config $config_path check-config"
    echo "  sudo env \"PATH=\$PATH\" \"\$(command -v eggpool)\" deploy systemd --install"
    echo "  eggpool deploy cron --install        # when systemd is unavailable"
    echo "  eggpool update"
}

# ---- authority dispatch (WP-A) ----

classify_existing_owner

AUTHORITY=""
PACKAGE_SPEC="eggpool"
MANAGER_FORCE=0
if [[ -n "$EXISTING_BIN" ]] || ((FORCE_REINSTALL)) || ((UPGRADE_ONLY)); then
    MANAGER_FORCE=1
fi

if ((SOURCE_CHECKOUT)) && [[ -z "$EXISTING_BIN" ]]; then
    # Developer flow: installing from a source checkout uses the local
    # candidate and never resolves the public package by accident.
    AUTHORITY="source-local"
    PACKAGE_SPEC="$PROJECT_DIR/packaging/pypi"
    if ((VERSION_REQUESTED)) && ((SOURCE_CHECKOUT == 1)); then
        # Version agreement already validated above.
        :
    fi
elif [[ -n "$EXISTING_BIN" ]]; then
    case "$PROVENANCE_KIND" in
        standalone-rust)
            if ((ADOPT_STANDALONE)); then
                AUTHORITY="standalone-adoption"
                echo "  Explicit standalone-to-wheel adoption requested"
                if ((VERSION_REQUESTED)) && ! version_at_or_after_native_release "$TARGET_VERSION"; then
                    fail "standalone installs cannot adopt historical Python-era release $TARGET_VERSION via wheel migration"
                fi
                if ((VERSION_REQUESTED)); then
                    PACKAGE_SPEC="eggpool==$TARGET_VERSION"
                fi
                # Manager selection for adoption: explicit flag wins,
                # otherwise prefer uv when present, else pipx.
                if [[ -n "$EXPLICIT_PACKAGE_MANAGER" ]]; then
                    ADOPTION_MANAGER_KIND="$EXPLICIT_PACKAGE_MANAGER"
                else
                    if [[ -n "$(find_uv)" ]]; then
                        ADOPTION_MANAGER_KIND="uv-tool"
                    elif [[ -n "$(find_pipx)" ]]; then
                        ADOPTION_MANAGER_KIND="pipx"
                    else
                        fail "no package manager is available for standalone adoption; install uv or pipx and retry"
                    fi
                fi
            else
                if ((VERSION_REQUESTED)) && ! version_at_or_after_native_release "$TARGET_VERSION"; then
                    fail "standalone installs cannot downgrade directly to historical Python-era release $TARGET_VERSION"
                fi
                AUTHORITY="existing-native-update"
            fi
            ;;
        uv-tool|pipx|pip)
            if [[ "$PROVENANCE_NATIVE" == "true" ]]; then
                AUTHORITY="existing-native-update"
            else
                # Legacy Python-era package install: preserve owner/package
                # compatibility transition via the owning manager.
                AUTHORITY="existing-legacy-package"
                if ((VERSION_REQUESTED)); then
                    PACKAGE_SPEC="eggpool==$TARGET_VERSION"
                fi
            fi
            # Explicit adoption flag is only meaningful for standalone.
            if ((ADOPT_STANDALONE)); then
                fail "--adopt-standalone applies only to standalone Rust installs"
            fi
            ;;
        *)
            fail "existing eggpool ownership is ambiguous; remove the collision explicitly or use a known manager"
            ;;
    esac
else
    # Fresh install: no existing command.
    if ((ADOPT_STANDALONE)); then
        fail "--adopt-standalone requires an existing standalone Rust install"
    fi
    if ((SOURCE_CHECKOUT)); then
        AUTHORITY="source-local"
        PACKAGE_SPEC="$PROJECT_DIR/packaging/pypi"
    elif ((VERSION_REQUESTED)) && ! version_at_or_after_native_release "$TARGET_VERSION"; then
        AUTHORITY="fresh-historical-package"
        PACKAGE_SPEC="eggpool==$TARGET_VERSION"
    elif [[ -n "$EXPLICIT_PACKAGE_MANAGER" ]]; then
        AUTHORITY="fresh-package-explicit"
        if ((VERSION_REQUESTED)); then
            PACKAGE_SPEC="eggpool==$TARGET_VERSION"
        else
            PACKAGE_SPEC="eggpool"
        fi
    else
        if ((VERSION_REQUESTED)); then
            AUTHORITY="fresh-binary-exact"
        else
            AUTHORITY="fresh-binary-latest"
        fi
    fi
fi

# Do not let the mere presence of pipx or uv alter the authority of a fresh
# current-native install: fresh-binary authorities above ignore manager
# discovery entirely.

case "$AUTHORITY" in
    fresh-binary-latest)
        echo "Selected authority: verified GitHub raw binary (latest) for $TARGET_CLASS"
        install_fresh_raw_binary "latest"
        ;;
    fresh-binary-exact)
        echo "Selected authority: verified GitHub raw binary ($TARGET_VERSION) for $TARGET_CLASS"
        install_fresh_raw_binary "$TARGET_VERSION"
        ;;
    fresh-historical-package)
        echo "Selected authority: historical package compatibility ($TARGET_VERSION)"
        check_historical_python_compat "$TARGET_VERSION"
        # Historical path requires a manager; prefer explicit flag, else uv, else pipx.
        if [[ -n "$EXPLICIT_PACKAGE_MANAGER" ]]; then
            HIST_MANAGER="$EXPLICIT_PACKAGE_MANAGER"
        else
            if [[ -n "$(find_uv)" ]]; then
                HIST_MANAGER="uv-tool"
            elif [[ -n "$(find_pipx)" ]]; then
                HIST_MANAGER="pipx"
            else
                echo "uv and pipx were not found; bootstrapping uv from its documented HTTPS installer..."
                curl -fsSL https://astral.sh/uv/install.sh | sh
                export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
                [[ -n "$(find_uv)" ]] || fail "uv bootstrap failed; install uv manually and rerun"
                HIST_MANAGER="uv-tool"
            fi
        fi
        # Historical installs always force the exact spec.
        run_package_authority "$HIST_MANAGER" "$PACKAGE_SPEC" 1
        ;;
    fresh-package-explicit)
        echo "Selected authority: explicit package-manager install ($EXPLICIT_PACKAGE_MANAGER)"
        run_package_authority "$EXPLICIT_PACKAGE_MANAGER" "$PACKAGE_SPEC" "$MANAGER_FORCE"
        ;;
    existing-native-update)
        EXPECTED_OWNER="$PROVENANCE_KIND"
        delegate_existing_native_update "$EXISTING_BIN"
        # Post-delegation owner must not silently change.
        if [[ "$PROVENANCE_KIND" != "$EXPECTED_OWNER" ]]; then
            fail "update changed ownership from $EXPECTED_OWNER to $PROVENANCE_KIND; refusing silent migration"
        fi
        ;;
    existing-legacy-package)
        echo "Selected authority: legacy package transition ($PROVENANCE_KIND)"
        run_package_authority "$PROVENANCE_KIND" "$PACKAGE_SPEC" "$MANAGER_FORCE"
        ;;
    standalone-adoption)
        echo "Selected authority: standalone-to-wheel adoption ($ADOPTION_MANAGER_KIND)"
        run_package_authority "$ADOPTION_MANAGER_KIND" "$PACKAGE_SPEC" "$MANAGER_FORCE"
        ;;
    source-local)
        echo "Selected authority: source-checkout local install"
        # Source flow preserves the previous manager-selection behavior.
        SOURCE_MANAGER_KIND="$PROVENANCE_KIND"
        if [[ -z "$SOURCE_MANAGER_KIND" ]]; then
            if [[ -n "$EXPLICIT_PACKAGE_MANAGER" ]]; then
                SOURCE_MANAGER_KIND="$EXPLICIT_PACKAGE_MANAGER"
            elif [[ -n "$(find_uv)" ]]; then
                SOURCE_MANAGER_KIND="uv-tool"
            elif [[ -n "$(find_pipx)" ]]; then
                SOURCE_MANAGER_KIND="pipx"
            else
                echo "uv and pipx were not found; bootstrapping uv from its documented HTTPS installer..."
                curl -fsSL https://astral.sh/uv/install.sh | sh
                export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
                [[ -n "$(find_uv)" ]] || fail "uv bootstrap failed; install uv manually and rerun"
                SOURCE_MANAGER_KIND="uv-tool"
            fi
        fi
        # When invoked from a checkout with an existing owner, preserve it.
        if [[ -z "$SOURCE_MANAGER_KIND" ]]; then
            SOURCE_MANAGER_KIND="uv-tool"
            if [[ -z "$(find_uv)" ]]; then
                echo "uv and pipx were not found; bootstrapping uv from its documented HTTPS installer..."
                curl -fsSL https://astral.sh/uv/install.sh | sh
                export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
                [[ -n "$(find_uv)" ]] || fail "uv bootstrap failed; install uv manually and rerun"
            fi
        fi
        run_package_authority "$SOURCE_MANAGER_KIND" "$PACKAGE_SPEC" "$MANAGER_FORCE"
        ;;
    *)
        fail "could not select an install authority"
        ;;
esac
