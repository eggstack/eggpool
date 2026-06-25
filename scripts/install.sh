#!/usr/bin/env bash
set -euo pipefail

# EggPool quick install script
# Usage: curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
# Or:    ./scripts/install.sh  (from a cloned repo)

REPO_URL="https://github.com/eggstack/eggpool.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/eggpool}"

echo "EggPool quick install"
echo ""

# If not inside a cloned repo, download one
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
if [ ! -f "$PROJECT_DIR/pyproject.toml" ] || \
   ! grep -q 'name = "eggpool"' "$PROJECT_DIR/pyproject.toml" 2>/dev/null; then
    if [ -d "$INSTALL_DIR" ]; then
        echo "Using existing installation at $INSTALL_DIR"
        cd "$INSTALL_DIR"
        git pull --ff-only || true
    else
        echo "Cloning repository to $INSTALL_DIR..."
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi
else
    cd "$PROJECT_DIR"
fi

# Save the scripts directory for install_prompt.py
SCRIPTS_DIR="${SCRIPTS_DIR:-$(pwd)/scripts}"

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

# Check for existing eggpool install
echo "Checking for existing eggpool install..."
if command -v eggpool >/dev/null 2>&1; then
    echo "Existing eggpool install detected: $(command -v eggpool)"
    echo "Using existing install. Run 'eggpool update' to upgrade."
    eggpool version
fi

# Check for pipx (invoke via detected Python to ensure correct version)
echo "Checking for pipx..."
if "$PYTHON" -m pipx --version >/dev/null 2>&1; then
    echo "Installing eggpool via pipx (Python $PYTHON_VERSION)..."
    "$PYTHON" -m pipx install eggpool
    # Ensure ~/.local/bin is on PATH so subsequent shell invocations
    # (and any helpers invoked below) can find the freshly-installed
    # `eggpool` binary.
    export PATH="$HOME/.local/bin:$PATH"
    echo "Installation complete. Run 'eggpool onboard' to start."
else
    # Check for uv
    echo "Checking uv package manager..."
    if ! command -v uv &> /dev/null; then
        echo "Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        # Ensure uv is on PATH for the rest of this script
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        if ! command -v uv &> /dev/null; then
            echo "Error: uv installation failed. Install manually:"
            echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
            exit 1
        fi
    fi
    echo "  uv found"

    # Install eggpool as a uv-managed tool. This mirrors `pipx install
    # eggpool` end-to-end: an isolated venv is created under
    # ~/.local/share/uv/tools/eggpool/ and `eggpool` is symlinked into
    # ~/.local/bin/ (or the Windows equivalent) so it works as a bare
    # command from any directory. `eggpool update` already detects this
    # install method and routes upgrades via `uv tool install`.
    echo ""
    echo "Installing eggpool as a uv-managed tool..."
    uv tool install "."

    # Persist ~/.local/bin on PATH for future shells. Idempotent — exits
    # non-zero if the user's shell can't be detected, which we treat as
    # a soft failure (the export below covers the current shell).
    uv tool update-shell >/dev/null 2>&1 || true

    # Re-export PATH so the current script session can find the freshly
    # installed `eggpool` binary, regardless of whether update-shell
    # succeeded.
    export PATH="$HOME/.local/bin:$PATH"
    if command -v eggpool >/dev/null 2>&1; then
        echo "  eggpool installed at: $(command -v eggpool)"
    else
        echo "  Warning: eggpool binary not on PATH yet."
        echo "  Restart your shell or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi

    # Copy example configuration if it doesn't exist
    echo ""
    echo "Setting up configuration..."
    if [ ! -f config.toml ]; then
        cp config.example.toml config.toml
        echo "  Created config.toml from config.example.toml"
    else
        echo "  config.toml already exists, skipping"
    fi

    CONFIG_PATH="$PROJECT_DIR/config.toml"
    echo ""
    echo "Installation complete."
    echo ""
    echo "Your config is at: $CONFIG_PATH"
    echo ""
    echo "Other useful commands (work from any directory):"
    echo "  eggpool --config $CONFIG_PATH accounts status   — show configured accounts"
    echo "  eggpool --config $CONFIG_PATH serve              — start the server"
    echo "  eggpool --config $CONFIG_PATH newkey             — regenerate server API key"
    echo "  eggpool --config $CONFIG_PATH rehash             — reload config in running server"
    echo "  eggpool --config $CONFIG_PATH update             — upgrade eggpool"
    echo ""
    echo "Tip: drop the --config flag by exporting in your shell rc:"
    echo "  export EGGPOOL_CONFIG=\"$CONFIG_PATH\""
    echo "  (CLI support for EGGPOOL_CONFIG is tracked separately.)"
    echo ""
    echo "For production deployment, see docs/deployment.md"
    echo ""
fi

# A curl-piped installer leaves stdin attached to the exhausted curl pipe.
# Prefer stdin when it is already interactive; otherwise reconnect the prompt
# to the controlling terminal. Keep the existing EOF/skip behavior when no
# controlling terminal is available (for example, in unattended installs).
# Use -S to avoid processing broken system .pth files (see above).
if [ -t 0 ]; then
    "$PYTHON" -S "${SCRIPTS_DIR}/install_prompt.py"
elif { exec 3</dev/tty; } 2>/dev/null; then
    "$PYTHON" -S "${SCRIPTS_DIR}/install_prompt.py" <&3
    exec 3<&-
else
    "$PYTHON" -S "${SCRIPTS_DIR}/install_prompt.py"
fi
