#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Step 1: rebuild the cumulative development triangle (#RSV-4180) --------
# The year-end rebuild stopped after the first development column. Aggregate the
# incremental claim movements back into /app/data/development_triangle.json;
# nothing the engine projects is correct until this is done.

python3 "${SCRIPT_DIR}/build_triangle.py"

# --- Step 2: restore the engine and produce the valuation artifacts ---------

cp "${SCRIPT_DIR}/develop_reserves_fixed.py" /app/workflow/develop_reserves.py
python3 /app/workflow/develop_reserves.py --output-dir /app/output
