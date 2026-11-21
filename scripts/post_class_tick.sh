#!/usr/bin/env bash
# Una hora después del comienzo de cada clase: actualiza h4c (ventana reciente)
# y regenera/publica la liga. Pensado para invocarse cada ~15 minutos.
# Nunca imprime secretos. set +x para no filtrar el token en el xtrace.
set -euo pipefail
set +x
export PS4='+ '

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG="$ROOT/logs/post_class.log"
STATE_DIR="$ROOT/state"
LOCK="$STATE_DIR/post_class_tick.lock"
PYTHON="$ROOT/.venv/bin/python"

mkdir -p "$ROOT/logs" "$STATE_DIR"

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR venv not found at $ROOT/.venv" >&2
  exit 2
fi

exec 9>"$LOCK"
if ! flock -n 9; then
  line="BUSY"
  echo "$line"
  echo "$(TZ=Europe/Madrid date '+%Y-%m-%d %H:%M:%S %Z') $line" >> "$LOG"
  exit 0
fi

exec "$PYTHON" "$ROOT/post_class_tick.py" "$@"
