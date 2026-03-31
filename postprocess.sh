#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

INPUTS_DIR=/Users/liujiarun/Downloads/latex2word/inputs
OUTPUTS_DIR=/Users/liujiarun/Downloads/latex2word/outputs

python label.py --input ${OUTPUTS_DIR}/translated.json --output ${OUTPUTS_DIR}/labeled.json
python refmap.py --input ${OUTPUTS_DIR}/labeled.json --output ${OUTPUTS_DIR}/refmap.json --verbose
rm ${OUTPUTS_DIR}/translated.json

python replace.py \
  --labeled   ${OUTPUTS_DIR}/labeled.json \
  --citations ${OUTPUTS_DIR}/citations.json \
  --refmap    ${OUTPUTS_DIR}/refmap.json \
  --output    ${OUTPUTS_DIR}/replaced.json \
  --verbose
rm ${OUTPUTS_DIR}/labeled.json

python render.py --json ${OUTPUTS_DIR}/replaced.json --docx ${OUTPUTS_DIR}/final.docx --citations ${OUTPUTS_DIR}/citations.json