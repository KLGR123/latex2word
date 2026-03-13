#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

INPUTS_DIR=/Users/liujiarun/Downloads/latex2word/inputs
OUTPUTS_DIR=/Users/liujiarun/Downloads/latex2word/outputs

python3 label.py --input ${OUTPUTS_DIR}/translated.json --output ${OUTPUTS_DIR}/labeled.json
python3 refmap.py --input ${OUTPUTS_DIR}/labeled.json --output ${OUTPUTS_DIR}/refmap.json --verbose