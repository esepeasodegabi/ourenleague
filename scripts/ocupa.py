#!/usr/bin/env python3
"""Genera la web de ocupación de sacos (site/ocupa/) a partir de Supabase (h4c).

Solo lectura: transacción READ ONLY, nada se escribe en la base de datos.
Solo datos agregados por clase (fecha/hora, asistentes, capacidad y no-shows). Sin nombres, ids ni datos personales.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import liga  # noqa: E402  (reutiliza la conexión read-only y la redacción de errores)

TZ_NAME = "Europe/Madrid"
log = logging.getLogger("ocupa")

# Una fila por hora de inicio de clase (el portal a veces deja un duplicado sin
# cerrar de la misma clase con reservas 'checked_in'; se agrupa por hora y solo
# cuentan los asistentes reales 'attended').
CLASSES_SQL = """
SELECT
  to_char(c.starts_at AT TIME ZONE %(tz)s, 'YYYY-MM-DD"T"HH24:MI') AS start_local,
  max(COALESCE(c.capacity, s.capacity)) AS capacity,
  count(p.*) FILTER (WHERE p.attendance = 'attended') AS attended,
  count(p.*) FILTER (WHERE p.attendance = 'no_show') AS no_show,
  bool_or(COALESCE(c.is_finished, false)) AS finished,
  max(c.class_type) AS class_type
FROM h4c.classes c
LEFT JOIN h4c.class_participants p ON p.class_id = c.id
LEFT JOIN h4c.schedule_slots s ON s.id = c.schedule_slot_id
WHERE c.starts_at IS NOT NULL
  AND COALESCE(c.is_cancelled, false) = false
  AND c.starts_at <= now()
GROUP BY 1
HAVING bool_or(COALESCE(c.is_finished, false))
    OR count(p.*) FILTER (WHERE p.attendance = 'attended') > 0
ORDER BY 1
"""

CANCELLED_SQL = """
SELECT count(*) FROM h4c.classes
WHERE COALESCE(is_cancelled, false) AND starts_at <= now()
"""


def load(conn) -> tuple[list[list], int]:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SHOW transaction_read_only")
        if str(cur.fetchone()[0]).lower() not in {"on", "true", "1"}:
            raise RuntimeError("La transacción no quedó en modo read-only")
        cur.execute(CLASSES_SQL, {"tz": TZ_NAME})
        rows = cur.fetchall()
        cur.execute(CANCELLED_SQL)
        cancelled = int(cur.fetchone()[0])
    conn.rollback()
    classes = []
    for start, cap, att, ns, _fin, _ctype in rows:
        classes.append([start, int(att or 0), int(cap) if cap else None, int(ns or 0)])
    return classes, cancelled


def build(out_dir: Path) -> dict:
    conn = liga.connect_readonly()
    try:
        classes, cancelled = load(conn)
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
    if not classes:
        raise RuntimeError("No hay clases con asistencia en la base de datos")
    data = {
        "generated_at": datetime.now(liga.MADRID).isoformat(timespec="minutes"),
        "timezone": TZ_NAME,
        "first": classes[0][0][:10],
        "last": classes[-1][0][:10],
        "cancelled_excluded": cancelled,
        # [inicio local (Madrid), asistentes, sacos (capacidad), no-shows]
        "cols": ["start", "attended", "capacity", "no_show"],
        "classes": classes,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "data.json").write_text(payload, encoding="utf-8")
    template = (HERE / "ocupa_template.html").read_text(encoding="utf-8")
    html = template.replace("__OCUPA_DATA_JSON__", payload.replace("</", "<\\/"))
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    total = sum(c[1] for c in classes)
    log.info("Ocupación: %s clases, %s asistencias, %s → %s", len(classes), total, data["first"], data["last"])
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(HERE / "site" / "ocupa"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        build(Path(args.out))
    except Exception as exc:  # no imprimir trazas que puedan llevar credenciales
        log.error("Fallo generando ocupación: %s", liga._redact(str(exc)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
