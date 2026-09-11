#!/usr/bin/env bash
set -euo pipefail

# EggPool quick install: install the native Rust wheel through one package
# manager. This script is intentionally usable as a curl pipeline and does
# not clone, build, or execute the repository's Python application.

FORCE_REINSTALL=0
UPGRADE_ONLY=0
ADOPT_STANDALONE=0
TARGET_VERSION=""
VERSION_REQUESTED=0
RUST_CUTOVER_VERSION="0.8.0"
INSTALL_INDEX_URL="${EGGPOOL_INSTALL_INDEX_URL:-}"
INSTALL_FIND_LINKS="${EGGPOOL_INSTALL_FIND_LINKS:-}"

version_at_or_after_cutover() {
    local version="$1"
    local major minor patch
    local cutover_major cutover_minor cutover_patch
    IFS=. read -r major minor patch <<< "$version"
    IFS=. read -r cutover_major cutover_minor cutover_patch <<< "$RUST_CUTOVER_VERSION"
    ((10#$major > 10#$cutover_major)) ||
        { ((10#$major == 10#$cutover_major && 10#$minor > 10#$cutover_minor)) ||
            { ((10#$major == 10#$cutover_major && 10#$minor == 10#$cutover_minor && 10#$patch >= 10#$cutover_patch)); }; }
}

usage() {
    cat <<'EOF'
EggPool quick install

Usage:
    curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
    ./scripts/install.sh [options]

Options:
    --version X.Y.Z     Install that exact catalogued release (leading v is accepted)
    --upgrade           Install the latest stable release explicitly
    --force             Reinstall or repair using the selected manager
    --adopt-standalone  Explicitly migrate a standalone Rust binary to a wheel
    --help              Show this help

Without --version, --upgrade selects the latest stable package-channel
release. --upgrade may be combined with --version; the exact version wins.
Source-checkout invocation installs the local checkout candidate and never
resolves the public package by accident.
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

if [[ -n "$INSTALL_INDEX_URL" || -n "$INSTALL_FIND_LINKS" ]]; then
    [[ "${EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX:-}" == "1" ]] ||
        fail "non-production package indexes require EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX=1"
    [[ "$INSTALL_INDEX_URL" != *$'\n'* && "$INSTALL_INDEX_URL" != *$'\r'* ]] ||
        fail "package index URL contains a newline"
    [[ "$INSTALL_FIND_LINKS" != *$'\n'* && "$INSTALL_FIND_LINKS" != *$'\r'* ]] ||
        fail "package find-links path contains a newline"
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
    if [[ ! "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        fail "invalid version '$1'; use X.Y.Z or vX.Y.Z"
    fi
    printf '%s' "$value"
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

if [[ "$(id -u)" == 0 ]]; then
    fail "personal quick install refuses root; use the explicit system deployment command instead"
fi

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" 2>/dev/null && pwd || true)"
SOURCE_CHECKOUT=0
PROJECT_DIR=""
if [[ -n "$SCRIPT_DIR" ]] && [[ -f "$SCRIPT_DIR/../rust/Cargo.toml" ]] && \
    [[ -f "$SCRIPT_DIR/../pyproject.toml" ]]; then
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

if [[ -n "$EXISTING_BIN" ]]; then
    echo "Inspecting existing eggpool install: $EXISTING_BIN"
    RUST_REPORT=""
    if RUST_REPORT="$("$EXISTING_BIN" install-provenance --shell 2>/dev/null)" && \
        parse_provenance_report "$RUST_REPORT"; then
        :
    elif probe_python_provenance "$EXISTING_BIN"; then
        :
    else
        PROVENANCE_KIND="ambiguous"
    fi

    case "$PROVENANCE_KIND" in
        uv-tool|pipx|pip)
            echo "  Existing owner: $PROVENANCE_KIND"
            ;;
        standalone-rust)
            if (( ! ADOPT_STANDALONE )); then
                fail "standalone Rust install found; rerun with --adopt-standalone to migrate it safely"
            fi
            echo "  Explicit standalone-to-wheel adoption requested"
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
fi

find_uv() {
    command -v uv 2>/dev/null || true
}

find_pipx() {
    command -v pipx 2>/dev/null || true
}

UV=""
PIPX=""
MANAGER_KIND="$PROVENANCE_KIND"
MANAGER=""
MANAGER_BIN_DIR=""
PACKAGE_SPEC="eggpool"
MANAGER_FORCE=0

if [[ -n "$EXISTING_BIN" ]] || ((FORCE_REINSTALL)) || ((UPGRADE_ONLY)); then
    MANAGER_FORCE=1
fi

if ((SOURCE_CHECKOUT)); then
    PACKAGE_SPEC="$PROJECT_DIR/packaging/pypi"
fi
if ((VERSION_REQUESTED)) && ((SOURCE_CHECKOUT == 0)); then
    PACKAGE_SPEC="eggpool==$TARGET_VERSION"
fi

case "$MANAGER_KIND" in
    uv-tool)
        UV="${PROVENANCE_MANAGER:-$(find_uv)}"
        [[ -x "$UV" ]] || fail "the existing uv tool owner is unavailable; restore uv and rerun"
        MANAGER="uv"
        MANAGER_BIN_DIR="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
        ;;
    pipx)
        PIPX="${PROVENANCE_MANAGER:-$(find_pipx)}"
        [[ -x "$PIPX" ]] || fail "the existing pipx owner is unavailable; restore pipx and rerun"
        MANAGER="pipx"
        MANAGER_BIN_DIR="${PIPX_BIN_DIR:-$HOME/.local/bin}"
        ;;
    pip)
        MANAGER="pip"
        [[ -x "$PROVENANCE_PYTHON" ]] || fail "the owning Python interpreter is unavailable; repair the environment with its manager"
        MANAGER_BIN_DIR="$(dirname "$EXISTING_BIN")"
        ;;
    standalone-rust)
        UV="$(find_uv)"
        if [[ -n "$UV" ]]; then
            MANAGER_KIND="uv-tool"
            MANAGER="uv"
            MANAGER_BIN_DIR="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
        else
            PIPX="$(find_pipx)"
            if [[ -n "$PIPX" ]]; then
                MANAGER_KIND="pipx"
                MANAGER="pipx"
                MANAGER_BIN_DIR="${PIPX_BIN_DIR:-$HOME/.local/bin}"
            else
                fail "no package manager is available for standalone adoption; install uv or pipx and retry"
            fi
        fi
        ;;
    "")
        UV="$(find_uv)"
        if [[ -n "$UV" ]]; then
            MANAGER_KIND="uv-tool"
            MANAGER="uv"
            MANAGER_BIN_DIR="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
        else
            PIPX="$(find_pipx)"
            if [[ -n "$PIPX" ]]; then
                MANAGER_KIND="pipx"
                MANAGER="pipx"
                MANAGER_BIN_DIR="${PIPX_BIN_DIR:-$HOME/.local/bin}"
            fi
        fi
        ;;
esac

if [[ -z "$MANAGER" ]]; then
    echo "uv and pipx were not found; bootstrapping uv from its documented HTTPS installer..."
    curl -fsSL https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    UV="$(find_uv)"
    [[ -n "$UV" ]] || fail "uv bootstrap failed; install uv manually and rerun"
    MANAGER_KIND="uv-tool"
    MANAGER="uv"
    MANAGER_BIN_DIR="${UV_TOOL_BIN_DIR:-$HOME/.local/bin}"
fi

same_path() {
    local left="$1" right="$2"
    [[ "$left" == "$right" ]] && return 0
    [[ -e "$left" && -e "$right" ]] || return 1
    [[ "$(realpath "$left")" == "$(realpath "$right")" ]]
}

EXPECTED_BIN="$MANAGER_BIN_DIR/eggpool"
if [[ "$MANAGER_KIND" == pip ]]; then
    EXPECTED_BIN="$EXISTING_BIN"
else
    if [[ -e "$EXPECTED_BIN" ]]; then
        collision=1
        if [[ -n "$EXISTING_BIN" ]] && same_path "$EXPECTED_BIN" "$EXISTING_BIN"; then
            collision=0
        fi
        if ((collision)); then
            fail "manager bin collision at $EXPECTED_BIN; refusing to change which eggpool command wins on PATH"
        fi
    fi
fi

OLD_STANDALONE=""
STANDALONE_BACKUP=""
WAS_RUNNING=0
restore_standalone() {
    local old_dir
    if [[ -n "$STANDALONE_BACKUP" && -e "$STANDALONE_BACKUP" && ! -e "$OLD_STANDALONE" ]]; then
        mv "$STANDALONE_BACKUP" "$OLD_STANDALONE"
    fi
    if ((WAS_RUNNING)) && [[ -n "$OLD_STANDALONE" && -x "$OLD_STANDALONE" ]]; then
        old_dir="$(dirname "$OLD_STANDALONE")"
        PATH="$old_dir:$PATH" "$OLD_STANDALONE" restart >/dev/null 2>&1 || true
    fi
}

if [[ "$PROVENANCE_KIND" == standalone-rust ]]; then
    OLD_STANDALONE="$EXISTING_BIN"
    if "$EXISTING_BIN" runtime-status --json >/dev/null 2>&1; then
        WAS_RUNNING=1
        "$EXISTING_BIN" stop >/dev/null 2>&1 || fail "could not stop the running standalone service; no files were changed"
    fi
    STANDALONE_VERSION="${PROVENANCE_VERSION:-unknown}"
    STANDALONE_BACKUP="${OLD_STANDALONE}.eggpool-standalone-${STANDALONE_VERSION}.rollback"
    [[ ! -e "$STANDALONE_BACKUP" ]] || fail "standalone rollback name already exists: $STANDALONE_BACKUP"
    mv "$OLD_STANDALONE" "$STANDALONE_BACKUP" || fail "could not stage the standalone binary for rollback"
fi

echo "Installing EggPool through $MANAGER_KIND..."
install_ok=1
PACKAGE_SOURCE_ARGS=()
case "$MANAGER_KIND" in
    uv-tool) package_source_args uv ;;
    pipx) package_source_args pipx ;;
    pip) package_source_args pip ;;
esac
case "$MANAGER_KIND" in
    uv-tool)
        PACKAGE_COMMAND=("$UV" tool install)
        if ((MANAGER_FORCE)); then
            PACKAGE_COMMAND+=(--force)
        fi
        if ((${#PACKAGE_SOURCE_ARGS[@]})); then
            PACKAGE_COMMAND+=("${PACKAGE_SOURCE_ARGS[@]}")
        fi
        PACKAGE_COMMAND+=("$PACKAGE_SPEC")
        if ! "${PACKAGE_COMMAND[@]}"; then install_ok=0; fi
        ;;
    pipx)
        PACKAGE_COMMAND=("$PIPX" install)
        if ((MANAGER_FORCE)); then
            PACKAGE_COMMAND+=(--force)
        fi
        if ((${#PACKAGE_SOURCE_ARGS[@]})); then
            PACKAGE_COMMAND+=("${PACKAGE_SOURCE_ARGS[@]}")
        fi
        PACKAGE_COMMAND+=("$PACKAGE_SPEC")
        if ! "${PACKAGE_COMMAND[@]}"; then install_ok=0; fi
        ;;
    pip)
        PACKAGE_COMMAND=("$PROVENANCE_PYTHON" -m pip install --upgrade --force-reinstall)
        if ((${#PACKAGE_SOURCE_ARGS[@]})); then
            PACKAGE_COMMAND+=("${PACKAGE_SOURCE_ARGS[@]}")
        fi
        PACKAGE_COMMAND+=("$PACKAGE_SPEC")
        if ! "${PACKAGE_COMMAND[@]}"; then install_ok=0; fi
        ;;
esac
if (( ! install_ok )); then
    restore_standalone
    fail "package-manager installation failed; previous standalone command was restored when applicable"
fi

if [[ "$MANAGER_KIND" != pip ]]; then
    export PATH="$MANAGER_BIN_DIR:$PATH"
fi
ACTIVE_BIN="$(command -v eggpool 2>/dev/null || true)"
if [[ "$MANAGER_KIND" != pip ]] && ! same_path "$ACTIVE_BIN" "$EXPECTED_BIN"; then
    restore_standalone
    fail "manager installed eggpool at an unexpected PATH location; refusing a silent command collision"
fi
[[ -n "$ACTIVE_BIN" && -x "$ACTIVE_BIN" ]] || {
    restore_standalone
    fail "installed eggpool command is not executable; previous standalone command was restored when applicable"
}

REPORT=""
NATIVE_REQUIRED=1
if ((VERSION_REQUESTED)) && ! version_at_or_after_cutover "$TARGET_VERSION"; then
    NATIVE_REQUIRED=0
fi
if REPORT="$("$ACTIVE_BIN" install-provenance --shell 2>/dev/null)" && parse_provenance_report "$REPORT"; then
    :
elif ! probe_python_provenance "$ACTIVE_BIN"; then
    restore_standalone
    fail "installed command did not provide verifiable package provenance"
fi
[[ "$PROVENANCE_KIND" == "$MANAGER_KIND" ]] || {
    restore_standalone
    fail "installed command is owned by $PROVENANCE_KIND, expected $MANAGER_KIND"
}
if ((NATIVE_REQUIRED)) && [[ "$PROVENANCE_NATIVE" != true ]]; then
    restore_standalone
    fail "installed command is not the native Rust cutover wheel owned by $MANAGER_KIND"
fi
if ((VERSION_REQUESTED)) && [[ "$PROVENANCE_VERSION" != "$TARGET_VERSION" ]]; then
    restore_standalone
    fail "installed version is ${PROVENANCE_VERSION:-unknown}, expected $TARGET_VERSION"
fi
CLI_VERSION="$("$ACTIVE_BIN" version 2>/dev/null | head -n 1 | tr -d '\r')" || {
    restore_standalone
    fail "installed eggpool version check failed"
}
[[ -n "$CLI_VERSION" ]] || {
    restore_standalone
    fail "installed eggpool returned an empty version"
}

CONFIG_PATH="${EGGPOOL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/eggpool/config.toml}"
if [[ ! -f "$CONFIG_PATH" ]]; then
    "$ACTIVE_BIN" init-config "$CONFIG_PATH" || {
        restore_standalone
        fail "could not seed the missing config at $CONFIG_PATH"
    }
    echo "Created $CONFIG_PATH from the installed package's canonical template."
else
    echo "Preserved existing config: $CONFIG_PATH"
fi

if ((WAS_RUNNING)); then
    PATH="$MANAGER_BIN_DIR:$PATH" "$ACTIVE_BIN" restart >/dev/null 2>&1 || {
        restore_standalone
        fail "new install could not restart the service; previous standalone command was restored when applicable"
    }
fi

echo ""
echo "Installation complete."
echo "  Version: $CLI_VERSION"
echo "  Manager: $MANAGER_KIND"
echo "  Command: $ACTIVE_BIN"
echo "  Config:  $CONFIG_PATH"
if [[ -n "$STANDALONE_BACKUP" && -e "$STANDALONE_BACKUP" ]]; then
    echo "  Standalone rollback retained at: $STANDALONE_BACKUP"
fi
echo ""
echo "Next steps:"
echo "  eggpool onboard"
echo "  eggpool --config $CONFIG_PATH check-config"
echo "  sudo env \"PATH=\$PATH\" \"\$(command -v eggpool)\" deploy systemd --install"
echo "  eggpool deploy cron --install        # when systemd is unavailable"
echo "  eggpool update"
