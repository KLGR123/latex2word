#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

INPUTS_DIR=/Users/liujiarun/Downloads/latex2word/inputs
OUTPUTS_DIR=/Users/liujiarun/Downloads/latex2word/outputs

python3 label.py --input ${OUTPUTS_DIR}/translated.json --output ${OUTPUTS_DIR}/labeled.json
python3 refmap.py --input ${OUTPUTS_DIR}/labeled.json --output ${OUTPUTS_DIR}/refmap.json --verbose
rm ${OUTPUTS_DIR}/translated.json

python3 replace.py \
  --labeled   ${OUTPUTS_DIR}/labeled.json \
  --citations ${OUTPUTS_DIR}/citations.json \
  --refmap    ${OUTPUTS_DIR}/refmap.json \
  --output    ${OUTPUTS_DIR}/replaced.json \
  --verbose
rm ${OUTPUTS_DIR}/labeled.json

python3 render.py --json outputs/replaced.json --docx outputs/final.docx