#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="latex2word"
PYTHON_VERSION="3.11"
SKIP_PANDOC=0
SKIP_PYTHON=0

for arg in "$@"; do
  case $arg in
    --no-pandoc) SKIP_PANDOC=1 ;;
    --no-python) SKIP_PYTHON=1 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

# ── conda environment ─────────────────────────────────────────────────────────
if [ "$SKIP_PYTHON" -eq 0 ]; then
  if command -v conda &>/dev/null; then
    if conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
      echo "[latex2word] conda env '$ENV_NAME' already exists, skipping creation."
    else
      echo "[latex2word] Creating conda env '$ENV_NAME' (Python $PYTHON_VERSION)..."
      conda create -y -n "$ENV_NAME" python="$PYTHON_VERSION"
    fi
    echo "[latex2word] Installing Python dependencies into '$ENV_NAME'..."
    conda run -n "$ENV_NAME" pip install -e .
    echo ""
    echo "[latex2word] Activate the environment before use:"
    echo "    conda activate $ENV_NAME"
  else
    echo "[latex2word] conda not found — installing into the current Python environment."
    pip install -e .
  fi
fi

# ── pandoc ────────────────────────────────────────────────────────────────────
if [ "$SKIP_PANDOC" -eq 0 ]; then
  if command -v pandoc &>/dev/null; then
    echo "[latex2word] pandoc already installed: $(pandoc --version | head -1)"
  else
    echo "[latex2word] Installing pandoc..."
    OS="$(uname -s)"
    case "$OS" in
      Darwin)
        if command -v brew &>/dev/null; then
          brew install pandoc
        else
          echo "[latex2word] Homebrew not found. Install pandoc manually: https://pandoc.org/installing.html"
        fi
        ;;
      Linux)
        if command -v apt-get &>/dev/null; then
          sudo apt-get install -y pandoc
        elif command -v dnf &>/dev/null; then
          sudo dnf install -y pandoc
        elif command -v pacman &>/dev/null; then
          sudo pacman -S --noconfirm pandoc
        else
          echo "[latex2word] Package manager not recognised. Install pandoc manually: https://pandoc.org/installing.html"
        fi
        ;;
      *)
        echo "[latex2word] Unsupported OS '$OS'. Install pandoc manually: https://pandoc.org/installing.html"
        ;;
    esac
  fi
fi

# ── secrets.env ───────────────────────────────────────────────────────────────
if [ ! -f secrets.env ]; then
  echo "[latex2word] Creating secrets.env from template..."
  cat > secrets.env <<'EOF'
# Paste your API key for the provider you want to use.
# Only the key for the provider you select needs to be filled in.

ANTHROPIC_API_KEY=
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
MOONSHOT_API_KEY=
EOF
  echo "[latex2word] secrets.env created. Open it and add your API key."
else
  echo "[latex2word] secrets.env already exists, skipping."
fi

echo ""
echo "[latex2word] Setup complete."
