#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

step() {
    echo ""
    echo "================================================================"
    echo "  STEP $1: $2"
    echo "================================================================"
}

elapsed() {
    local secs=$1
    printf "%dm%02ds" $((secs / 60)) $((secs % 60))
}

run_step() {
    local num="$1" label="$2" script="$3"
    step "$num" "$label"
    local t0=$SECONDS
    bash "$SCRIPT_DIR/$script"
    echo "[INFO] $label done in $(elapsed $((SECONDS - t0)))."
}

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

TOTAL_START=$SECONDS

run_step 1 "Preprocess  (chunk / bib)"  preprocess.sh
run_step 2 "Translate"                  translate.sh
run_step 3 "Postprocess (label / ref)"  postprocess.sh

echo ""
echo "================================================================"
echo "  ALL STEPS COMPLETE  (total: $(elapsed $((SECONDS - TOTAL_START))))"
echo "================================================================"