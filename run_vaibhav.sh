#!/usr/bin/env bash
# Launch script for Vaibhav's Job Hunter instance
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export JOBHUNTER_PROFILE="$SCRIPT_DIR/config/profile_vaibhav.yaml"
export JOBHUNTER_SEARCH_MATRIX="$SCRIPT_DIR/config/search_matrix_vaibhav.yaml"
export JOBHUNTER_DATA_DIR="$SCRIPT_DIR/data_vaibhav"
export JOBHUNTER_DB="$SCRIPT_DIR/data_vaibhav/jobhunter.sqlite3"

PORT="${PORT:-8085}"
HOST="${HOST:-127.0.0.1}"

if [ $# -eq 0 ]; then
  CMD="serve"
else
  CMD="$1"
  shift
fi

case "$CMD" in
  serve)
    echo "Starting Vaibhav Job Hunter on http://$HOST:$PORT"
    echo "Profile: $JOBHUNTER_PROFILE"
    echo "Data dir: $JOBHUNTER_DATA_DIR"
    exec "$SCRIPT_DIR/.venv/bin/python" -m jobhunter.cli serve --host "$HOST" --port "$PORT" "$@"
    ;;
  *)
    exec "$SCRIPT_DIR/.venv/bin/python" -m jobhunter.cli "$CMD" "$@"
    ;;
esac
