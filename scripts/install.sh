#!/usr/bin/env bash
set -euo pipefail

# EggPool quick install script
# Usage: curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
# Or:    ./scripts/install.sh  (from a cloned repo)

REPO_URL="https://github.com/eggstack/eggpool.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/eggpool}"

echo "=== EggPool quick install ==="
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

# Copy example configuration files if they don't exist
echo ""
echo "Setting up configuration..."
if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    echo "  Created config.toml from config.example.toml"
else
    echo "  config.toml already exists, skipping"
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "  Created .env from .env.example"
    echo ""
    echo "  IMPORTANT: Edit .env and set your API keys:"
    echo "    - GO_AGGREGATOR_API_KEY: Your local proxy API key"
    echo "    - OPENCODE_GO_KEY_1: Your OpenCode Go subscription key"
else
    echo "  .env already exists, skipping"
fi

# Validate configuration
echo ""
echo "Validating configuration..."
if set -a && source .env 2>/dev/null && set +a && uv run eggpool --config config.toml check-config 2>&1; then
    echo "  Configuration is valid"
else
    echo "  Warning: Configuration validation failed"
    echo "  Please edit config.toml and .env with your settings"
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config.toml with your settings (accounts, upstream URL, etc.)"
echo "  2. Edit .env with your API keys"
echo "  3. Run database migrations: uv run eggpool --config config.toml migrate"
echo "  4. Start the server: uv run eggpool --config config.toml serve"
echo ""
echo "For production deployment, see docs/deployment.md"
