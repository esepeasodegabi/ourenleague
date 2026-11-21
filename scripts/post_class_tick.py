#!/usr/bin/env python3
"""Tick: one hour after a class starts, refresh the DB and publish the league.

Intended to be invoked about every 15 minutes by a scheduler. Portal access
is read-only. The league script is never asked to write to the database.
Secrets stay in the environment and are never printed or written to the log.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
LIGA_DIR = Path("/workspace/liga")
LOG_PATH = SCRIPT_DIR / "logs" / "post_class.log"
STATE_PATH = SCRIPT_DIR / "state" / "post_class_done.json"
PYTHON = SCRIPT_DIR / ".venv" / "bin" / "python"
MADRID = ZoneInfo("Europe/Madrid")

# Make local imports work when launched via the shell wrapper.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from extract import (  # noqa: E402
    DEFAULT_EMAIL,
    Q_CLASSES_FINISHED,
    Q_CLASSES_UPCOMING,
    Q_SCHEDULE,
    parse_dt,
    ymd_slash,
)
from h4c_client import H4CClient, H4CError  # noqa: E402


def now_madrid() -> datetime:
    return datetime.now(MADRID)


def stamp() -> str:
    return now_madrid().strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp()} {msg.rstrip()}\n")


def emit(line: str, *, also_log: bool = True) -> None:
    """Print the single status line required by the caller."""
    sys.stdout.write(line.rstrip() + "\n")
    sys.stdout.flush()
    if also_log:
        log(line)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"classes": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"classes": {}}
    if not isinstance(data, dict):
        return {"classes": {}}
    classes = data.get("classes")
    if not isinstance(classes, dict):
        data["classes"] = {}
    return data


def save_state(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def league_start() -> str:
    default = "2026-11-09"
    cfg_path = LIGA_DIR / "config.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        value = data.get("start") or default
        # Validate ISO date.
        date.fromisoformat(str(value))
        return str(value)
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "True", "yes"):
        return True
    return False


def _class_from_item(item: dict[str, Any], *, is_finished: bool) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict) or not item.get("id"):
        return None
    starts = parse_dt(item.get("tim"))
    if starts is None:
        return None
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=MADRID)
    starts = starts.astimezone(MADRID)
    trainer = item.get("tra") if isinstance(item.get("tra"), dict) else {}
    return {
        "id": str(item["id"]),
        "starts_at": starts,
        "start_hm": starts.strftime("%H:%M"),
        "is_finished": is_finished,
        "is_cancelled": _as_bool(item.get("off")),
        "reserved": item.get("rsv_val"),
        "trainer": trainer.get("name") or "",
    }


def fetch_day_classes(client: H4CClient, day: date) -> list[dict[str, Any]]:
    variables = {"from": ymd_slash(day), "to": ymd_slash(day)}
    finished = client.graphql(Q_CLASSES_FINISHED, variables) or {}
    upcoming = client.graphql(Q_CLASSES_UPCOMING, variables) or {}
    by_id: dict[str, dict[str, Any]] = {}
    # Upcoming first, finished overwrites (a class that already ran is finished).
    for item in upcoming.get("classes") or []:
        rec = _class_from_item(item, is_finished=False)
        if rec:
            by_id[rec["id"]] = rec
    for item in finished.get("classes") or []:
        rec = _class_from_item(item, is_finished=True)
        if rec:
            by_id[rec["id"]] = rec
    return sorted(by_id.values(), key=lambda r: (r["starts_at"], r["id"]))


def fetch_schedule(client: H4CClient) -> list[dict[str, Any]]:
    data = client.graphql(Q_SCHEDULE, {"addRemoved": False}) or {}
    rows: list[dict[str, Any]] = []
    for slot in data.get("schedule") or []:
        if not isinstance(slot, dict):
            continue
        if _as_bool(slot.get("isRemoved")):
            continue
        hour = slot.get("hour")
        day = slot.get("day")
        if not hour or day is None:
            continue
        try:
            day_i = int(day)
        except (TypeError, ValueError):
            continue
        rows.append({"day": day_i, "hour": str(hour), "name": slot.get("name") or ""})
    return rows


def weekday_bounds(slots: list[dict[str, Any]]) -> list[tuple[int, str, str, str, int]]:
    """Return (iso_day, name, earliest, latest, count) for days that have slots."""
    names = {1: "lunes", 2: "martes", 3: "miércoles", 4: "jueves", 5: "viernes", 6: "sábado", 7: "domingo"}
    by_day: dict[int, list[str]] = {}
    for slot in slots:
        by_day.setdefault(slot["day"], []).append(slot["hour"])
    out = []
    for day in range(1, 8):
        hours = sorted(set(by_day.get(day) or []))
        if not hours:
            continue
        out.append((day, names[day], hours[0], hours[-1], len(hours)))
    return out


def due_classes(
    classes: list[dict[str, Any]],
    *,
    now: datetime,
    state: dict[str, Any],
    force: bool,
) -> list[dict[str, Any]]:
    done = state.get("classes") or {}
    due = []
    for rec in classes:
        if rec["is_cancelled"]:
            continue
        if rec["starts_at"] + timedelta(minutes=60) > now:
            continue
        if rec["id"] in done and not force:
            continue
        due.append(rec)
    return due


def format_times(classes: list[dict[str, Any]]) -> str:
    return " ".join(rec["start_hm"] for rec in classes)


def run_cmd(cmd: list[str], *, cwd: Path, timeout: int) -> None:
    # Inherit the environment (secrets already present). Never log the env.
    log("Running: " + " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # Redact anything that looks like a secret if it leaked into output.
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    for key in ("H4C_PASSWORD", "SUPABASE_DB_URL", "GITHUB_OURENLEAGUE_TOKEN"):
        val = os.environ.get(key)
        if val:
            out = out.replace(val, "<redacted>")
    # Keep the log useful but bounded.
    tail = "\n".join(out.strip().splitlines()[-80:])
    if tail:
        log(tail)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed rc={proc.returncode}: {' '.join(cmd)}")


def mark_done(state: dict[str, Any], classes: list[dict[str, Any]]) -> None:
    now = now_madrid().isoformat()
    bucket = state.setdefault("classes", {})
    for rec in classes:
        bucket[rec["id"]] = {
            "starts_at": rec["starts_at"].isoformat(),
            "start_hm": rec["start_hm"],
            "done_at": now,
        }
    save_state(state)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Post-class DB update + league publish tick")
    p.add_argument("--dry-run", action="store_true", help="List due classes and exit without running scripts")
    p.add_argument("--force", action="store_true", help="Treat already-recorded classes as due again")
    p.add_argument("--schedule", action="store_true", help="Print weekly class hours from the portal and exit")
    p.add_argument("--date", default=None, help="Club day to inspect (YYYY-MM-DD, default: today in Europe/Madrid)")
    p.add_argument(
        "--as-of",
        default=None,
        help="Override current time (ISO datetime). Default: now in Europe/Madrid",
    )
    p.add_argument("--from-date", dest="from_date", default=None, help="Recent extract start (default: day-2)")
    p.add_argument("--to-date", dest="to_date", default=None, help="Recent extract end (default: day+1)")
    return p.parse_args(argv)


def resolve_now(args: argparse.Namespace) -> datetime:
    if not args.as_of:
        return now_madrid()
    raw = args.as_of.strip()
    dt = parse_dt(raw)
    if dt is None:
        # date-only
        try:
            d = date.fromisoformat(raw)
            return datetime(d.year, d.month, d.day, tzinfo=MADRID)
        except ValueError as exc:
            raise SystemExit(f"ERROR invalid --as-of: {raw}") from exc
    return dt.astimezone(MADRID)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        now = resolve_now(args)
        day = date.fromisoformat(args.date) if args.date else now.date()
        client = H4CClient(email=os.environ.get("H4C_EMAIL", DEFAULT_EMAIL), lang="es")
        client.login()
        log(f"Portal login ok for tick day={day} now={now.isoformat()}")

        if args.schedule:
            slots = fetch_schedule(client)
            lines = ["SCHEDULE"]
            for _day, name, earliest, latest, n in weekday_bounds(slots):
                lines.append(f"{name}: {earliest}-{latest} ({n} horas)")
            emit(" | ".join(lines))
            return 0

        classes = fetch_day_classes(client, day)
        times = format_times(classes) or "(ninguna)"
        log(
            "Day classes: "
            + ", ".join(
                f"{c['start_hm']} id={c['id'][-6:]} fin={int(c['is_finished'])} off={int(c['is_cancelled'])} rsv={c['reserved']}"
                for c in classes
            )
            or "(none)"
        )
        state = load_state()
        due = due_classes(classes, now=now, state=state, force=args.force)

        if args.dry_run:
            if not due:
                emit(f"DRY_RUN NOTHING_DUE classes={times}")
            else:
                emit(f"DRY_RUN DUE {format_times(due)} (of {times})")
            return 0

        if not due:
            emit("NOTHING_DUE")
            return 0

        extract_from = args.from_date or (day - timedelta(days=2)).isoformat()
        extract_to = args.to_date or (day + timedelta(days=1)).isoformat()
        start = league_start()
        python = str(PYTHON if PYTHON.is_file() else sys.executable)

        run_cmd(
            [
                python,
                str(SCRIPT_DIR / "extract.py"),
                "--recent",
                "--from-date",
                extract_from,
                "--to-date",
                extract_to,
            ],
            cwd=SCRIPT_DIR,
            timeout=60 * 40,
        )
        run_cmd(
            [str(LIGA_DIR / "publish.sh"), "--start", start],
            cwd=LIGA_DIR,
            timeout=60 * 20,
        )
        mark_done(state, due)
        emit(f"PUBLISHED {format_times(due)}")
        return 0
    except subprocess.TimeoutExpired as exc:
        emit(f"ERROR timeout: {exc.cmd[0] if exc.cmd else 'command'}")
        return 1
    except H4CError as exc:
        emit(f"ERROR portal: {exc}")
        return 1
    except Exception as exc:
        log(traceback.format_exc())
        msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        for key in ("H4C_PASSWORD", "SUPABASE_DB_URL", "GITHUB_OURENLEAGUE_TOKEN"):
            val = os.environ.get(key)
            if val:
                msg = msg.replace(val, "<redacted>")
        emit(f"ERROR {msg}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
