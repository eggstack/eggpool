#!/usr/bin/env bash
set -euo pipefail

# EggPool quick install script
# Usage: curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
# Or:    ./scripts/install.sh [--force|--upgrade]  (from a cloned repo)

REPO_URL="https://github.com/eggstack/eggpool.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/eggpool}"

FORCE_REINSTALL=0
UPGRADE_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --force)
            FORCE_REINSTALL=1
            ;;
        --upgrade)
            UPGRADE_ONLY=1
            ;;
        --help|-h)
            cat <<'EOF'
EggPool quick install

Usage:
    curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
    ./scripts/install.sh [--force|--upgrade]

Options:
    --force     Reinstall even if an existing `eggpool` binary is on PATH
    --upgrade   Upgrade the existing install; do not reinstall from scratch
    --help      Show this help
EOF
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

echo "EggPool quick install"
echo ""

# Detect whether we are running from inside a cloned repo (SCRIPT_DIR exists
# on disk and points at the repo root). In a curl-piped run SCRIPT_DIR is a
# /dev/fd/... path that does not exist on disk; fall through to cloning.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
SOURCE_CHECKOUT=0
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../pyproject.toml" ] && \
   grep -q 'name = "eggpool"' "$SCRIPT_DIR/../pyproject.toml" 2>/dev/null; then
    SOURCE_CHECKOUT=1
    PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
fi

if [ "$SOURCE_CHECKOUT" -eq 1 ]; then
    cd "$PROJECT_DIR"
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "Using existing installation at $INSTALL_DIR"
        cd "$INSTALL_DIR"
        git pull --ff-only >/dev/null 2>&1 || true
    else
        echo "Cloning repository to $INSTALL_DIR..."
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi
fi

# Always reset PROJECT_DIR to the directory we are actually in. The earlier
# SCRIPT_DIR-based PROJECT_DIR is bogus for curl-piped runs because SCRIPT_DIR
# resolves to a /dev/fd path that does not exist on disk.
PROJECT_DIR="$(pwd)"
SCRIPTS_DIR="$PROJECT_DIR/scripts"

# Find the best available Python >= 3.11 and <= 3.14
# Probes version-suffixed binaries (python3.14, python3.13, ...) for systems
# where the default `python3` is an older system version.
# Max is 3.14 because Pyo3 (used by Granian) does not yet support 3.15.
find_python() {
    for minor in 14 13 12 11; do
        local candidate="python3.${minor}"
        if command -v "$candidate" &> /dev/null; then
            local ver
            ver=$(PYTHONPATH= PYTHONNOUSERSITE=1 "$candidate" -S -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || continue
            local maj min
            maj=$(echo "$ver" | cut -d. -f1)
            min=$(echo "$ver" | cut -d. -f2)
            if [ "$maj" -ge 3 ] && [ "$min" -ge 11 ]; then
                PYTHON="$candidate"
                PYTHON_VERSION="$ver"
                return 0
            fi
        fi
    done
    # Fallback to bare python3
    if command -v python3 &> /dev/null; then
        local ver
        ver=$(PYTHONPATH= PYTHONNOUSERSITE=1 python3 -S -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || true
        local maj min
        maj=$(echo "$ver" | cut -d. -f1)
        min=$(echo "$ver" | cut -d. -f2)
        if [ "$maj" -ge 3 ] && [ "$min" -ge 11 ]; then
            PYTHON="python3"
            PYTHON_VERSION="$ver"
            return 0
        fi
    fi
    return 1
}

echo "Checking Python version..."
if ! find_python; then
    echo "Error: Python 3.11 or later required."
    echo "Install Python from https://www.python.org/downloads/ or your package manager."
    exit 1
fi
echo "  Python $PYTHON_VERSION found ($PYTHON)"

# Decide whether to reinstall. Never silently overwrite an existing install.
EXISTING_BIN=""
if command -v eggpool >/dev/null 2>&1; then
    EXISTING_BIN="$(command -v eggpool)"
fi

if [ -n "$EXISTING_BIN" ] && [ "$FORCE_REINSTALL" -ne 1 ] && [ "$UPGRADE_ONLY" -ne 1 ]; then
    echo ""
    echo "Existing eggpool install detected: $EXISTING_BIN"
    echo "Run 'eggpool update' to upgrade, or rerun with --force to reinstall."
    echo ""
    # Even with an existing install, make sure ~/.local/bin is on PATH so the
    # subsequent commands are reachable, and still seed the config file if
    # it is missing (XDG default or source-checkout copy).
    export PATH="$HOME/.local/bin:$PATH"
    eggpool version
    _seed_install_config "$PROJECT_DIR"
    _print_install_next_steps "$PROJECT_DIR" "$(_installed_config_path)"
    _run_install_prompt
    exit 0
fi

# At this point we either have no existing install, --upgrade, or --force.
# Pick the install method.
USE_PIPX=0
USE_UV_TOOL=0
echo "Checking for pipx..."
if "$PYTHON" -m pipx --version >/dev/null 2>&1; then
    USE_PIPX=1
fi

if [ "$USE_PIPX" -eq 1 ]; then
    echo "Installing eggpool via pipx (Python $PYTHON_VERSION)..."
    if [ "$SOURCE_CHECKOUT" -eq 1 ]; then
        # From a source checkout, install the local code instead of PyPI
        # so the operator is testing what they just cloned rather than
        # silently swapping it for the latest released version.
        if [ "$FORCE_REINSTALL" -eq 1 ]; then
            "$PYTHON" -m pipx install --force "$PROJECT_DIR"
        else
            "$PYTHON" -m pipx install "$PROJECT_DIR"
        fi
    else
        "$PYTHON" -m pipx install eggpool
    fi
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "Checking uv package manager..."
    if ! command -v uv &> /dev/null; then
        echo "Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        if ! command -v uv &> /dev/null; then
            echo "Error: uv installation failed. Install manually:"
            echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
            exit 1
        fi
    fi
    echo "  uv found"

    # Install eggpool as a uv-managed tool. Mirrors `pipx install eggpool`
    # end-to-end: an isolated venv is created under
    # ~/.local/share/uv/tools/eggpool/ and `eggpool` is symlinked into
    # ~/.local/bin/ so it works as a bare command from any directory.
    echo ""
    if [ "$SOURCE_CHECKOUT" -eq 1 ]; then
        echo "Installing eggpool from local checkout ($PROJECT_DIR)..."
        if [ "$FORCE_REINSTALL" -eq 1 ]; then
            uv tool install --force "$PROJECT_DIR"
        else
            uv tool install "$PROJECT_DIR"
        fi
    else
        echo "Installing eggpool from PyPI..."
        uv tool install eggpool
    fi

    uv tool update-shell >/dev/null 2>&1 || true
    export PATH="$HOME/.local/bin:$PATH"
    if command -v eggpool >/dev/null 2>&1; then
        echo "  eggpool installed at: $(command -v eggpool)"
    else
        echo "  Warning: eggpool binary not on PATH yet."
        echo "  Restart your shell or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
fi

# Configure paths and print next steps. Both pipx and uv-tool paths now
# print the resolved config path so the operator always knows where to look.
_seed_install_config "$PROJECT_DIR"
CONFIG_PATH="$(_installed_config_path)"

echo ""
echo "Installation complete."
echo ""
echo "Your config is at: $CONFIG_PATH"
echo ""
_print_install_next_steps "$PROJECT_DIR" "$CONFIG_PATH"

_run_install_prompt

# ---------------------------------------------------------------------------
# Helper functions (defined last so the main flow stays at the top).
# ---------------------------------------------------------------------------

# Seed ~/.config/eggpool/config.toml and ~/.config/eggpool/.env if they are
# missing, without overwriting anything the operator already wrote. Used by
# both the pipx and the uv-tool code paths so behavior is symmetric.
_seed_install_config() {
    local project_dir="$1"
    local config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/eggpool"
    mkdir -p "$config_dir"

    if [ ! -f "$config_dir/config.toml" ]; then
        if [ -f "$project_dir/config.example.toml" ]; then
            cp "$project_dir/config.example.toml" "$config_dir/config.toml"
            echo "  Created $config_dir/config.toml from example template."
        else
            # Minimal fallback that satisfies `eggpool check-config`.
            cat > "$config_dir/config.toml" <<'TOML'
[server]
host = "0.0.0.0"
port = 11300
log_level = "INFO"

[database]
path = "~/.local/share/eggpool/usage.sqlite3"

[models]
refresh_interval_s = 300
TOML
            echo "  Created minimal $config_dir/config.toml."
        fi
    else
        echo "  config.toml already exists at $config_dir, skipping."
    fi
}

# Resolve the canonical installed config path the operator should use.
# Reads $EGGPOOL_CONFIG if set, then falls back to the XDG default.
_installed_config_path() {
    if [ -n "${EGGPOOL_CONFIG:-}" ]; then
        echo "$EGGPOOL_CONFIG"
    else
        echo "${XDG_CONFIG_HOME:-$HOME/.config}/eggpool/config.toml"
    fi
}

# Print the post-install next-step guide. Mirrors the documented primary
# private-deployment path; the cron fallback is offered when systemd is
# unavailable.
_print_install_next_steps() {
    local project_dir="$1"
    local config_path="$2"

    echo "Next steps:"
    echo "  eggpool onboard                                    — interactive provider setup"
    echo "  eggpool --config $config_path check-config        — validate configuration"
    echo ""
    if [ -d /run/systemd/system ] || command -v systemctl >/dev/null 2>&1; then
        echo "Run the systemd installer (preferred when systemd is available):"
        echo "  sudo env \"PATH=\$PATH\" \$(command -v eggpool) deploy systemd --install"
        echo ""
    fi
    echo "Or, on systems without systemd, install the watchdog cron entry:"
    echo "  eggpool deploy cron --install"
    echo ""
    echo "Tip: drop the --config flag by exporting in your shell rc:"
    echo "  export EGGPOOL_CONFIG=\"$config_path\""
    echo ""
    echo "Other useful commands (work from any directory):"
    echo "  eggpool accounts status"
    echo "  eggpool serve"
    echo "  eggpool rehash"
    echo "  eggpool update"
    echo ""
    echo "For production deployment, see docs/deployment.md"
}

# Run the install_prompt helper with the same stdin-detachment logic the
# original script used. Factored out so the helpers above stay close to the
# main flow.
_run_install_prompt() {
    # A curl-piped installer leaves stdin attached to the exhausted curl pipe.
    # Prefer stdin when it is already interactive; otherwise reconnect the
    # prompt to the controlling terminal. Keep the existing EOF/skip behavior
    # when no controlling terminal is available (for example, in unattended
    # installs).
    if [ -t 0 ]; then
        "$PYTHON" -S "${SCRIPTS_DIR}/install_prompt.py"
    elif { exec 3</dev/tty; } 2>/dev/null; then
        "$PYTHON" -S "${SCRIPTS_DIR}/install_prompt.py" <&3
        exec 3<&-
    else
        "$PYTHON" -S "${SCRIPTS_DIR}/install_prompt.py"
    fi
}
