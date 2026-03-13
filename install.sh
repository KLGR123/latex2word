#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Pandoc
# ---------------------------------------------------------------------------

install_pandoc() {
    if command -v pandoc &> /dev/null; then
        echo "[INFO] pandoc already installed: $(pandoc --version | head -1)"
        return
    fi

    echo "[INFO] Installing pandoc..."
    OS="$(uname -s)"
    ARCH="$(uname -m)"

    case "$OS" in
        Darwin)
            if command -v brew &> /dev/null; then
                brew install pandoc
            else
                echo "[ERROR] Homebrew not found. Install it first: https://brew.sh" >&2
                exit 1
            fi
            ;;
        Linux)
            # Detect distro and pick the right package manager.
            if command -v apt-get &> /dev/null; then
                sudo apt-get update -qq
                sudo apt-get install -y pandoc
            elif command -v dnf &> /dev/null; then
                sudo dnf install -y pandoc
            elif command -v yum &> /dev/null; then
                sudo yum install -y pandoc
            elif command -v pacman &> /dev/null; then
                sudo pacman -Sy --noconfirm pandoc
            else
                echo "[ERROR] No supported package manager found (apt/dnf/yum/pacman)." >&2
                echo "        Install pandoc manually: https://pandoc.org/installing.html" >&2
                exit 1
            fi
            ;;
        *)
            echo "[ERROR] Unsupported OS: $OS" >&2
            echo "        Install pandoc manually: https://pandoc.org/installing.html" >&2
            exit 1
            ;;
    esac

    echo "[INFO] pandoc installed: $(pandoc --version | head -1)"
}

# ---------------------------------------------------------------------------
# Python requirements
# ---------------------------------------------------------------------------

install_python_deps() {
    REQ="$SCRIPT_DIR/requirements.txt"
    if [[ ! -f "$REQ" ]]; then
        echo "[WARN] requirements.txt not found at $REQ — skipping Python deps." >&2
        return
    fi

    echo "[INFO] Installing Python dependencies from $REQ..."

    # Prefer python3; fall back to python.
    PYTHON="$(command -v python3 || command -v python)"
    if [[ -z "$PYTHON" ]]; then
        echo "[ERROR] Python not found. Install Python 3 first." >&2
        exit 1
    fi

    echo "[INFO] Using Python: $PYTHON ($($PYTHON --version))"

    # --break-system-packages is needed on newer Debian/Ubuntu with PEP 668.
    if "$PYTHON" -m pip install -r "$REQ" --break-system-packages 2>/dev/null; then
        :
    else
        # Older pip versions don't recognise the flag — try without it.
        "$PYTHON" -m pip install -r "$REQ"
    fi

    echo "[INFO] Python dependencies installed."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

install_pandoc
install_python_deps

echo "[INFO] Installation complete."