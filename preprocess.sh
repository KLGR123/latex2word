#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

INPUTS_DIR=/Users/liujiarun/Downloads/latex2word/inputs
OUTPUTS_DIR=/Users/liujiarun/Downloads/latex2word/outputs

# Collect all subfolder paths under inputs/
folders=()
for d in "$INPUTS_DIR"/*/; do
  [[ -d "$d" ]] && folders+=("${d%/}")  # strip trailing slash
done

if [[ ${#folders[@]} -gt 0 ]]; then
  python tex.py --folders "${folders[@]}" --verbose --strip-formatting
else
  echo "No subfolders found in $INPUTS_DIR" >&2
fi

# Collect .bib and .bbl files together; bib.py handles routing internally:
# .bib files are parsed with bibtexparser, .bbl files with the BBL parser.
# When both exist, entries from all files are merged before dedup/sort.
ref_files=()
tex_files=()

for folder in "${folders[@]}"; do
  for f in "$folder"/*.bib "$folder"/*.bbl; do
    ref_files+=("$f")
  done
  for tex in "$folder"/*.tex; do
    tex_files+=("$tex")
  done
done

mkdir -p "$OUTPUTS_DIR"

if ((${#ref_files[@]} > 0)); then
  python bib.py --bib "${ref_files[@]}" --out "$OUTPUTS_DIR/citations.json" --sort year --dedup --lang en
else
  echo "No .bib or .bbl files found in folders: ${folders[*]:-(none)}" >&2
fi

if ((${#tex_files[@]} > 0)); then
  python chunk.py --tex "${tex_files[@]}" --out "$OUTPUTS_DIR/chunks.json" --include-title --keep-commands --split-on-forced-linebreak --strict
else
  echo "No .tex files found in folders: ${folders[*]:-(none)}" >&2
fi