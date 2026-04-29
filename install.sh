#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_PANDOC=1
INSTALL_PYTHON=1

usage() {
    cat <<'USAGE'
Usage: ./install.sh [options]

Options:
  --no-pandoc   Skip system pandoc installation/check.
  --no-python   Skip Python dependency installation.
  -h, --help    Show this help message.

Environment:
  PYTHON=/path/to/python  Select the Python interpreter used for pip.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-pandoc)
            INSTALL_PANDOC=0
            ;;
        --no-python)
            INSTALL_PYTHON=0
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

find_python() {
    if [[ -n "${PYTHON:-}" ]]; then
        if ! command -v "$PYTHON" >/dev/null 2>&1; then
            echo "[ERROR] PYTHON is set but not executable: $PYTHON" >&2
            exit 1
        fi
        echo "$PYTHON"
        return
    fi

    if [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/python" ]]; then
        echo "${CONDA_PREFIX}/bin/python"
        return
    fi

    if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
        echo "${VIRTUAL_ENV}/bin/python"
        return
    fi

    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return
    fi
    if command -v python >/dev/null 2>&1; then
        command -v python
        return
    fi

    echo "[ERROR] Python not found. Install Python 3.10+ first." >&2
    exit 1
}

pip_install() {
    local python_bin="$1"
    shift

    if "$python_bin" -m pip install "$@" --break-system-packages 2>/dev/null; then
        return
    fi

    "$python_bin" -m pip install "$@"
}

pip_install_editable_project() {
    local python_bin="$1"
    local project_dir="$2"

    if pip_install "$python_bin" -e "$project_dir" --no-build-isolation; then
        return
    fi

    echo "[WARN] Editable install without build isolation failed; retrying with isolated build." >&2
    pip_install "$python_bin" -e "$project_dir"
}

install_pandoc() {
    if command -v pandoc >/dev/null 2>&1; then
        echo "[INFO] pandoc already installed: $(pandoc --version | head -1)"
        return
    fi

    echo "[INFO] Installing pandoc..."
    local os_name
    os_name="$(uname -s)"

    case "$os_name" in
        Darwin)
            if command -v brew >/dev/null 2>&1; then
                brew install pandoc
            else
                echo "[ERROR] Homebrew not found. Install pandoc manually: https://pandoc.org/installing.html" >&2
                exit 1
            fi
            ;;
        Linux)
            if command -v apt-get >/dev/null 2>&1; then
                sudo apt-get update -qq
                sudo apt-get install -y pandoc
            elif command -v dnf >/dev/null 2>&1; then
                sudo dnf install -y pandoc
            elif command -v yum >/dev/null 2>&1; then
                sudo yum install -y pandoc
            elif command -v pacman >/dev/null 2>&1; then
                sudo pacman -Sy --noconfirm pandoc
            else
                echo "[ERROR] No supported package manager found (apt/dnf/yum/pacman)." >&2
                echo "        Install pandoc manually: https://pandoc.org/installing.html" >&2
                exit 1
            fi
            ;;
        *)
            echo "[ERROR] Unsupported OS: $os_name" >&2
            echo "        Install pandoc manually: https://pandoc.org/installing.html" >&2
            exit 1
            ;;
    esac

    echo "[INFO] pandoc installed: $(pandoc --version | head -1)"
}

install_python_deps() {
    local python_bin
    python_bin="$(find_python)"

    echo "[INFO] Using Python: $python_bin ($("$python_bin" --version))"
    "$python_bin" - <<'PY'
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required for this project.")
PY
    "$python_bin" -m pip --version >/dev/null

    if [[ -f "$SCRIPT_DIR/pyproject.toml" ]]; then
        echo "[INFO] Installing latex2word package in editable mode..."
        pip_install_editable_project "$python_bin" "$SCRIPT_DIR"
        echo "[INFO] Python package installed."
        return
    fi

    if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
        echo "[INFO] Installing Python dependencies from requirements.txt..."
        pip_install "$python_bin" -r "$SCRIPT_DIR/requirements.txt"
        echo "[INFO] Python dependencies installed."
        return
    fi

    echo "[WARN] Neither pyproject.toml nor requirements.txt was found; skipping Python deps." >&2
}

if [[ "$INSTALL_PANDOC" -eq 1 ]]; then
    install_pandoc
else
    echo "[INFO] Skipping pandoc installation."
fi

if [[ "$INSTALL_PYTHON" -eq 1 ]]; then
    install_python_deps
else
    echo "[INFO] Skipping Python dependency installation."
fi

echo "[INFO] Installation complete."
