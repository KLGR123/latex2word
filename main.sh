#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

INPUTS_DIR=/Users/liujiarun/Downloads/latex2word/inputs
OUTPUTS_DIR=/Users/liujiarun/Downloads/latex2word/outputs
CONFIGS_DIR=/Users/liujiarun/Downloads/latex2word/configs

# Collect all subfolder paths under inputs/
folders=()
for d in "$INPUTS_DIR"/*/; do
  [[ -d "$d" ]] && folders+=("${d%/}")  # strip trailing slash
done

if [[ ${#folders[@]} -gt 0 ]]; then
  python3 tex.py --folders "${folders[@]}" --verbose
else
  echo "No subfolders found in $INPUTS_DIR" >&2
fi

bib_files=()
tex_files=()

for folder in "${folders[@]}"; do
  for bib in "$folder"/*.bib; do
    bib_files+=("$bib")
  done
  for tex in "$folder"/*.tex; do
    tex_files+=("$tex")
  done
done

mkdir -p $OUTPUTS_DIR

if ((${#bib_files[@]} > 0)); then
  python3 bib.py --bib "${bib_files[@]}" --out $OUTPUTS_DIR/refs.json --sort year --dedup --lang en
else
  echo "No .bib files found in folders: ${folders[*]:-(none)}" >&2
fi

if ((${#tex_files[@]} > 0)); then
  python3 chunk.py --tex "${tex_files[@]}" --out $OUTPUTS_DIR/chunks.json --include-title --keep-commands --split-on-forced-linebreak --strict
else
  echo "No .tex files found in folders: ${folders[*]:-(none)}" >&2
fi