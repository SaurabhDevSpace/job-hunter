#!/usr/bin/env bash
# Re-collect evidence for the endpoint-security allowlist request.
# Binary hashes rotate on every toolchain upgrade, so re-run this immediately
# before filing or updating the ticket.
#
# Usage:  bash collect_blocked_binaries.sh [workspace-root]
set -uo pipefail

WORKSPACE="${1:-$HOME/OneDrive - Ciena Corporation/Desktop/bp_codebases}"

# Only Blue Planet work repositories are in scope. Personal/scratch project
# trees under the same workspace root are excluded deliberately - they are not
# part of this request.
EXCLUDE_DIRS=(self_cool_stuff)

prune_args=()
for d in "${EXCLUDE_DIRS[@]}"; do
  prune_args+=( -path "$WORKSPACE/$d" -prune -o )
done

CANDIDATES=(
  "$HOME/.local/share/uv/python/cpython-3.12.10-macos-aarch64-none/bin/python3.12"
  "$HOME/.local/share/uv/python/cpython-3.10.17-macos-aarch64-none/bin/python3.10"
  "$HOME/miniconda3/bin/python3.12"
  "$HOME/miniconda3/envs/bpa_ai_sdk_py_3_10/bin/python3.10"
  "$HOME/miniconda3/envs/gen_ai_py3_12/bin/python3.12"
  "$HOME/miniconda3/envs/py313/bin/python3.13"
  "/usr/bin/python3"
  "/usr/local/bin/python3"
)

probe() {  # probe <label-cmd...> -> echoes STATUS
  "$@" >/dev/null 2>&1
  local rc=$?
  case $rc in
    0)   echo OK ;;
    137) echo SIGKILL ;;
    *)   echo "rc=$rc" ;;
  esac
}

echo "== Interpreters =="
printf '%-8s %-64s %s\n' STATUS SHA256 PATH
for p in "${CANDIDATES[@]}"; do
  if [ ! -x "$p" ]; then
    printf '%-8s %-64s %s\n' MISSING - "$p"
    continue
  fi
  status=$(probe "$p" -c 'pass')
  printf '%-8s %-64s %s\n' "$status" "$(shasum -a 256 "$p" | awk '{print $1}')" "$p"
done

echo
echo "== Bundled ripgrep binaries =="
find "$HOME/.cache/pkg" -type f -name rg 2>/dev/null | while read -r f; do
  status=$(probe "$f" --version)
  printf '%-8s %-64s %s\n' "$status" "$(shasum -a 256 "$f" | awk '{print $1}')" "$f"
done

echo
echo "== Project virtualenvs under the workspace =="
echo "   (a venv python is a symlink; the exclusion must target the RESOLVED path)"
if [ -d "$WORKSPACE" ]; then
  found=0
  while read -r v; do
    [ -n "$v" ] || continue
    found=1
    real=$(/usr/bin/python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$v" 2>/dev/null || echo "$v")
    status=$(probe "$v" -c 'pass')
    printf '%-8s %s\n' "$status" "${v#"$WORKSPACE"/}"
    printf '         -> %s\n' "$real"
  done < <(find "$WORKSPACE" "${prune_args[@]}" \( -type f -o -type l \) -path '*/.venv/bin/python' -print 2>/dev/null)
  [ "$found" -eq 1 ] || echo "   (none found)"
else
  echo "   (workspace not found: $WORKSPACE)"
fi

echo
echo "== Compiled wheels commonly hit by symptom B =="
echo "   (hashes change on every pip install / uv sync - always re-read them)"
find "$WORKSPACE" "${prune_args[@]}" \
     \( -path '*/site-packages/pydantic_core/*.so' \
     -o -path '*/site-packages/numpy/core/*.so' \
     -o -path '*/site-packages/lxml/*.so' \
     -o -path '*/site-packages/websockets/speedups*.so' \) -print \
     2>/dev/null | head -12 | while read -r so; do
  printf '%-64s %s\n' "$(shasum -a 256 "$so" | awk '{print $1}')" "${so#"$WORKSPACE"/}"
done

echo
echo "Reminder: paste the refreshed values into ticket-body.txt before sending."
