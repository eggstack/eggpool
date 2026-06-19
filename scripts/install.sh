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
if [ ! -f "$(dirname "${BASH_SOURCE[0]}")/../../pyproject.toml" ] || \
   ! grep -q 'name = "eggpool"' "$(dirname "${BASH_SOURCE[0]}")/../../pyproject.toml" 2>/dev/null; then
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
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
    cd "$PROJECT_DIR"
fi

# Save the scripts directory for install_prompt.py
SCRIPTS_DIR="${SCRIPTS_DIR:-$(pwd)/scripts}"

# Check for Python 3.12+
echo "Checking Python version..."
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found. Please install Python 3.12 or later."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 12 ]; }; then
    echo "Error: Python 3.12 or later required (found $PYTHON_VERSION)"
    exit 1
fi
echo "  Python $PYTHON_VERSION found"

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

# Install dependencies
echo ""
echo "Installing dependencies..."
uv sync --extra dev

# Copy example configuration if it doesn't exist
echo ""
echo "Setting up configuration..."
if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    echo "  Created config.toml from config.example.toml"
else
    echo "  config.toml already exists, skipping"
fi

echo ""
echo "Installation complete."
echo ""
echo "Other useful commands:"
echo "  uv run eggpool accounts status   — show configured accounts"
echo "  uv run eggpool newkey             — regenerate server API key"
echo "  uv run eggpool rehash             — reload config in running server"
echo "  uv run eggpool stop               — stop the server"
echo "  uv run eggpool restart            — restart the server"
echo ""
echo "For production deployment, see docs/deployment.md"
echo ""

# A curl-piped installer leaves stdin attached to the exhausted curl pipe.
# Prefer stdin when it is already interactive; otherwise reconnect the prompt
# to the controlling terminal. Keep the existing EOF/skip behavior when no
# controlling terminal is available (for example, in unattended installs).
if [ -t 0 ]; then
    python3 "${SCRIPTS_DIR}/install_prompt.py"
elif { exec 3</dev/tty; } 2>/dev/null; then
    python3 "${SCRIPTS_DIR}/install_prompt.py" <&3
    exec 3<&-
else
    python3 "${SCRIPTS_DIR}/install_prompt.py"
fi
