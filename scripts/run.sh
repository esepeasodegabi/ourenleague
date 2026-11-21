#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "error: venv not found at $ROOT/.venv (create it and install psycopg)" >&2
  exit 2
fi
exec "$ROOT/.venv/bin/python" "$ROOT/extract.py" "$@"
