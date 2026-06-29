#!/bin/bash
#
# Quick single-run evaluation against an ALREADY-RUNNING local OpenRAG instance.
#
# For full versioned runs (checkout a build -> deploy -> eval -> teardown) use
# orchestrator.py instead; to browse/compare results use `streamlit run dashboard.py`.
# This script just exercises the upload -> generate -> benchmark flow and writes a
# tagged, dashboard-comparable run under reports/<run-id>/.
#
# Usage:
#   ./run_all.sh                          # full: upload -> generate -> benchmark
#   ./run_all.sh --golden <path.json>     # benchmark only, against a supplied dataset
#   GOLDEN_DATASET=<path.json> ./run_all.sh
#   PARTITION=my_partition ./run_all.sh   # override the partition (default: config.py)
#   ./run_all.sh --limit 20               # any extra args are forwarded to benchmark.py
#
# When a golden dataset is supplied, upload_files.py and generate_questions.py
# are skipped (you are responsible for indexing the matching documents).

# Resolve paths from this script's own location so the harness is portable across
# machines and works regardless of where the eval dir sits in the repo (it lives at
# tests/load/automatic-evaluation-pipeline/ on refactor/hexagonal). The OpenRAG repo
# root is found via git toplevel rather than a fixed number of parent dirs.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENRAG_REPO="${OPENRAG_REPO:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || dirname "$SCRIPT_DIR")}"

# Activate the project virtualenv (override with VENV_DIR). Skipped if absent, so the
# script also works under uv/conda/system Python already on PATH.
VENV_DIR="${VENV_DIR:-$OPENRAG_REPO/myenv}"
if [ -f "$VENV_DIR/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
fi

cd "$SCRIPT_DIR" || exit 1

set -e

# Housekeeping: drop report files older than 14 days.
find reports -type f -mtime +14 -delete 2>/dev/null || true

# ---- parse args (anything unrecognised is forwarded to benchmark.py) ----
GOLDEN=""
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --golden)   GOLDEN="$2"; shift 2 ;;
    --golden=*) GOLDEN="${1#--golden=}"; shift ;;
    *)          EXTRA_ARGS+=("$1"); shift ;;
  esac
done
GOLDEN="${GOLDEN:-${GOLDEN_DATASET:-}}"

# ---- resolve the partition (env override, else config.py default) ----
PARTITION="${PARTITION:-$(python3 -c 'from config import CONFIG; print(CONFIG.common.partition)' 2>/dev/null || echo test3)}"

# ---- tag this run so the dashboard treats it as a comparable run, not a
#      "legacy flat" one: unique id + the OpenRAG build's git branch/commit.
#      (OPENRAG_REPO is resolved at the top from this script's location.) ----
RUN_ID="manual-$(date +%Y%m%d-%H%M%S)"
VERSION="$(git -C "$OPENRAG_REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
GIT_COMMIT="$(git -C "$OPENRAG_REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
OUTPUT_DIR="reports/$RUN_ID"
mkdir -p "$OUTPUT_DIR"

# Shared tags handed to benchmark.py so the run is identified in the dashboard.
TAG_ARGS=(--partition "$PARTITION" --run-id "$RUN_ID" --version "$VERSION" \
          --git-commit "$GIT_COMMIT" --output-dir "$OUTPUT_DIR")

echo "******** JOB START $(date) — run_id=$RUN_ID partition=$PARTITION version=$VERSION *******"

if [ -n "$GOLDEN" ]; then
  if [ ! -f "$GOLDEN" ]; then
    echo "Golden dataset not found: $GOLDEN" >&2
    exit 1
  fi
  echo "Using golden dataset: $GOLDEN (skipping upload_files + generate_questions)"
  DATASET="$GOLDEN"
  python3 benchmark.py --dataset "$DATASET" "${TAG_ARGS[@]}" "${EXTRA_ARGS[@]}"
else
  python3 upload_files.py --partition "$PARTITION"
  echo "upload_files done"

  # Keep the generated dataset next to the run's reports.
  DATASET="$OUTPUT_DIR/dataset.json"
  python3 generate_questions.py --partition "$PARTITION" --output "$DATASET"
  echo "generate_questions done"

  python3 benchmark.py --dataset "$DATASET" "${TAG_ARGS[@]}" "${EXTRA_ARGS[@]}"
fi
echo "benchmark done"

# ---- snapshot what produced this run (optional; dashboard discovers runs via
#      reports/*/eval_*.json, this is just for provenance/parity with orchestrator) ----
cat > "$OUTPUT_DIR/run_config.json" <<EOF
{
  "run_id": "$RUN_ID",
  "version": "$VERSION",
  "git_commit": "$GIT_COMMIT",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "partition": "$PARTITION",
  "dataset": "$DATASET",
  "base_url": "${API_BASE_URL:-}",
  "source": "run_all.sh (manual run against an already-running instance)"
}
EOF

echo "Report written to $OUTPUT_DIR  (view: streamlit run dashboard.py)"
echo "******** JOB END $(date) *******"
