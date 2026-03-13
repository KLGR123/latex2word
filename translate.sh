#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

source "$(dirname "$0")/secrets.env"

CONFIGS_DIR=/Users/liujiarun/Downloads/latex2word/configs
OUTPUTS_DIR=/Users/liujiarun/Downloads/latex2word/outputs

TRANSLATE_ARGS=(
  --input  "$OUTPUTS_DIR/chunks.json"
  --output "$OUTPUTS_DIR/translated.json"
  --provider anthropic
  --model claude-sonnet-4-5-20250929
  --concurrency 8
  --api-key "$ANTHROPIC_API_KEY"
)

if [[ -f "$CONFIGS_DIR/terms.json" ]]; then
  TRANSLATE_ARGS+=(--terms "$CONFIGS_DIR/terms.json")
fi
python3 translate.py "${TRANSLATE_ARGS[@]}" --strip-cjk-spaces
rm "$OUTPUTS_DIR/chunks.json"