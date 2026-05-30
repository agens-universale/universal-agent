#!/usr/bin/env bash
# Merge-blocking checks for working_memory cutover (spec steps 0 and 6).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== rg memory_files (src/) — informational =="
rg 'memory_files' src/ || true
echo "(enforce: only deprecated_tools/memory_files.py + persistence/schema.py)"
echo

echo "== rg journal (src/summarize/) =="
if rg -q 'journal' src/summarize/ 2>/dev/null; then
  echo "FAIL: journal must not appear under src/summarize/" >&2
  rg 'journal' src/summarize/
  exit 1
fi
echo "(no matches — OK)"
echo

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY=python3
fi

echo "== pytest cutover invariants =="
"$PY" -m pytest tests/test_memory_cutover_invariants.py -q
echo

echo "== pytest full suite =="
"$PY" -m pytest tests/ -q --tb=no
echo
echo "Cutover verification passed."
