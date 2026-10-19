#!/usr/bin/env bash
# Launch script for Ragini's Job Hunter instance
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export JOBHUNTER_PROFILE="$SCRIPT_DIR/config/profile_ragini.yaml"
export JOBHUNTER_SEARCH_MATRIX="$SCRIPT_DIR/config/search_matrix_ragini.yaml"
export JOBHUNTER_DATA_DIR="$SCRIPT_DIR/data_ragini"
export JOBHUNTER_DB="$SCRIPT_DIR/data_ragini/jobhunter.sqlite3"

PORT="${PORT:-8081}"
HOST="${HOST:-127.0.0.1}"

echo "Starting Ragini Job Hunter on http://$HOST:$PORT"
echo "Profile: $JOBHUNTER_PROFILE"
echo "Data dir: $JOBHUNTER_DATA_DIR"

exec "$SCRIPT_DIR/.venv/bin/python" -m jobhunter.cli serve --host "$HOST" --port "$PORT"
