#!/usr/bin/env python3
"""Liga interna de Brooklyn Fitboxing Ourense.

Calcula puntos a partir de la asistencia (y, si está disponible, de los
referidos I ❤️ BF del portal) y genera una web estática.

NUNCA escribe ni modifica nada en la base de datos ni en el portal:
solo SELECT / GraphQL de lectura, transacción READ ONLY, ROLLBACK al terminar.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
H4C_DIR = Path("/workspace/h4c")
HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "site"
DEFAULT_TITLE = "Liga Brooklyn Ourense"
PUBLIC_KEY_SALT = "liga-ourense-v1"
TZ_NAME = "Europe/Madrid"
IMG_BASE = "https://www.brooklynfitzone.com/resources/images/"
DEFAULT_AVATAR = "man_ico.png"
PHOTO_SIZE = 160
H4C_EMAIL = os.environ.get("H4C_EMAIL", "")

RETURN_SHORT_DAYS = 14
RETURN_LONG_DAYS = 30
RETURN_SHORT_PTS = 14
RETURN_LONG_PTS = 20
STREAK_PAIR_PTS = 10
STREAK_MONTH_PTS = 14
RUTINA_PTS = {2: 5, 3: 6, 4: 7, 5: 8, 6: 9, 7: 10}
DOBLETE_PTS = 2
ILBF_BOOKING_PTS = 16
ILBF_CONVERSION_PTS = 30

MONTHS_ES = "ene feb mar abr may jun jul ago sep oct nov dic".split()
ORDS = {1: "1.º", 2: "2.º", 3: "3.er", 4: "4.º", 5: "5.º", 6: "6.º", 7: "7.º"}

log = logging.getLogger("liga")


# ---------------------------------------------------------------------------
# Display / privacy
# ---------------------------------------------------------------------------

def public_name(first_name: Optional[str], last_name: Optional[str], name: Optional[str]) -> str:
    """Nombre de pila + inicial del primer apellido."""
    first, last = _split_name(first_name, last_name, name)
    if not first:
        first = "Fitboxer"
    source = last or first
    initial = next((ch for ch in source if ch.isalpha()), "F")
    return f"{first} {initial.upper()}."


def _split_name(first_name: Optional[str], last_name: Optional[str], name: Optional[str]) -> tuple[str, str]:
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if not first and name:
        parts = [p for p in name.strip().split() if p]
        if parts:
            first = parts[0]
            if not last and len(parts) > 1:
                last = parts[-1]
    return first, last


def _surname_letters(last: str) -> str:
    return "".join(ch for ch in (last or "") if ch.isalpha())


def format_surname_prefix(letters: str, width: int) -> str:
    chunk = letters[:width]
    if not chunk:
        return ""
    if len(chunk) == 1:
        return chunk.upper()
    return chunk[0].upper() + chunk[1:].lower()


def disambiguate_names(
    scores: list["PersonScore"],
    raw_names: dict[str, tuple[Optional[str], Optional[str], Optional[str]]],
) -> None:
    """Si hay dos 'Laura G.', pasa a 'Laura Ga.' / 'Laura Gr.' (más letras si hace falta)."""
    groups: dict[str, list[PersonScore]] = defaultdict(list)
    for s in scores:
        groups[s.display_name].append(s)
    for people in groups.values():
        if len(people) < 2:
            continue
        infos = []
        for s in people:
            first, last, full = raw_names.get(s.fitboxer_id, (None, None, None))
            first, last = _split_name(first, last, full)
            if not first:
                first = s.display_name.split()[0] if s.display_name else "Fitboxer"
            infos.append((s, first, _surname_letters(last)))
        max_len = max((len(L) for _, _, L in infos), default=1)
        # A second letter is enough in almost every case; never publish a full surname.
        max_width = min(3, max(2, max_len))
        chosen: list[str] = []
        for width in range(2, max_width + 1):
            names = []
            for _s, first, letters in infos:
                if not letters:
                    # Always "Nombre I." so the privacy scan accepts the public site.
                    if _s.display_name.endswith(".") and " " in _s.display_name:
                        names.append(_s.display_name)
                    else:
                        initial = next((ch for ch in first if ch.isalpha()), "F")
                        names.append(f"{first} {initial.upper()}.")
                else:
                    names.append(f"{first} {format_surname_prefix(letters, width)}.")
            if len(set(names)) == len(names):
                chosen = names
                break
            chosen = names
        for s, nn in zip((x[0] for x in infos), chosen):
            s.display_name = nn


def initials_for(display_name: str) -> str:
    parts = [p for p in display_name.replace(".", "").split() if p]
    if not parts:
        return "F"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def public_trainer(name: Optional[str]) -> Optional[str]:
    """Nombre público del entrenador (nombre + inicial), o un alias corto si es un puesto."""
    if not name:
        return None
    n = " ".join(name.strip().split())
    if not n:
        return None
    lower = n.lower()
    if "emergencias" in lower:
        return "Emergencias"
    if lower.startswith("trainer"):
        return "Trainer"
    parts = n.split()
    if len(parts) == 1:
        return parts[0]
    initial = next((ch for ch in parts[1] if ch.isalpha()), parts[1][0])
    return f"{parts[0]} {initial.upper()}."


def public_key(fitboxer_id: str) -> str:
    digest = hashlib.sha256(f"{PUBLIC_KEY_SALT}:{fitboxer_id}".encode("utf-8")).hexdigest()
    return digest[:16]


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def fmt_day(d: date) -> str:
    return f"{d.day} {MONTHS_ES[d.month - 1]}"


def fmt_week(monday: date) -> str:
    end = monday + timedelta(days=6)
    if monday.month == end.month:
        return f"{monday.day}–{end.day} {MONTHS_ES[monday.month - 1]}"
    return f"{monday.day} {MONTHS_ES[monday.month - 1]}–{end.day} {MONTHS_ES[end.month - 1]}"


def to_madrid_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=MADRID)
        return dt.astimezone(MADRID).date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(MADRID).date()
    except Exception:
        try:
            return date.fromisoformat(text[:10])
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Attendance:
    fitboxer_id: str
    class_id: str
    day: date
    time: Optional[str] = None
    trainer: Optional[str] = None
    class_name: Optional[str] = None


@dataclass
class PointEvent:
    date: date
    points: int
    reason: str
    time: Optional[str] = None
    class_name: Optional[str] = None
    trainer: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "time": self.time,
            "class_name": self.class_name,
            "trainer": self.trainer,
            "points": self.points,
            "reason": self.reason,
        }


@dataclass
class RuleLine:
    key: str
    label: str
    points: int
    count: int
    detail: str
    pending: bool = False
    events: list[PointEvent] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "points": self.points,
            "count": self.count,
            "detail": self.detail,
            "pending": self.pending,
            "events": [e.as_dict() for e in self.events],
        }


@dataclass
class PersonScore:
    fitboxer_id: str
    display_name: str
    public_key: str
    points: int
    training_days: int
    classes: int
    avatar_path: Optional[str] = None  # portal relative path, or None
    photo: Optional[str] = None  # site-relative path written at render time
    breakdown: list[RuleLine] = field(default_factory=list)

    def as_public_dict(self, rank: int) -> dict[str, Any]:
        return {
            "key": self.public_key,
            "rank": rank,
            "name": self.display_name,
            "initials": initials_for(self.display_name),
            "photo": self.photo,
            "points": self.points,
            "training_days": self.training_days,
            "classes": self.classes,
            "breakdown": [line.as_dict() for line in self.breakdown],
        }


@dataclass
class Referral:
    referrer_id: str
    referred_id: str
    referred_name: str
    booking_date: Optional[date]
    conversion_date: Optional[date]


def _days_classes(rows: Iterable[Attendance]) -> dict[date, list[Attendance]]:
    by_day: dict[date, list[Attendance]] = defaultdict(list)
    seen: set[tuple[date, str]] = set()
    for row in rows:
        key = (row.day, row.class_id)
        if key in seen:
            continue
        seen.add(key)
        by_day[row.day].append(row)
    for day in by_day:
        by_day[day].sort(key=lambda r: (r.time or "99:99", r.class_id))
    return dict(by_day)


def _event_from_day(days: dict[date, list[Attendance]], day: date, points: int, reason: str) -> PointEvent:
    rows = days.get(day) or []
    row = rows[0] if rows else None
    return PointEvent(
        date=day,
        points=points,
        reason=reason,
        time=row.time if row else None,
        class_name=row.class_name if row else None,
        trainer=public_trainer(row.trainer) if row else None,
    )


def score_returns(days_before: list[date], days_in_window: list[date], by_day: dict[date, list[Attendance]]) -> RuleLine:
    prev: Optional[date] = days_before[-1] if days_before else None
    n14 = n20 = 0
    events: list[PointEvent] = []
    for day in days_in_window:
        if prev is not None:
            gap = (day - prev).days
            if gap >= RETURN_LONG_DAYS:
                n20 += 1
                events.append(_event_from_day(by_day, day, RETURN_LONG_PTS, f"Vuelta tras {gap} días (1 mes)"))
            elif gap >= RETURN_SHORT_DAYS:
                n14 += 1
                events.append(_event_from_day(by_day, day, RETURN_SHORT_PTS, f"Vuelta tras {gap} días"))
        prev = day
    points = n14 * RETURN_SHORT_PTS + n20 * RETURN_LONG_PTS
    parts = []
    if n14:
        parts.append(f"{n14} × {RETURN_SHORT_PTS} (14 días)")
    if n20:
        parts.append(f"{n20} × {RETURN_LONG_PTS} (30 días)")
    return RuleLine(
        key="vuelta",
        label="Vuelta",
        points=points,
        count=n14 + n20,
        detail=" + ".join(parts) if parts else "sin vueltas",
        events=events,
    )


def score_streaks(qualifying: list[tuple[date, date]], by_day: dict[date, list[Attendance]]) -> tuple[RuleLine, RuleLine]:
    """qualifying: (week_monday, second_training_day)."""
    weeks = sorted(qualifying, key=lambda x: x[0])
    ev2: list[PointEvent] = []
    ev4: list[PointEvent] = []
    i = 0
    while i < len(weeks):
        run = 1
        while i + run < len(weeks) and weeks[i + run][0] == weeks[i][0] + timedelta(days=7 * run):
            run += 1
        for n in range(2, run + 1, 2):
            monday, day = weeks[i + n - 1]
            ev2.append(_event_from_day(
                by_day, day, STREAK_PAIR_PTS,
                f"Racha: semana {n} consecutiva ({fmt_week(monday)})",
            ))
        for n in range(4, run + 1, 4):
            monday, day = weeks[i + n - 1]
            ev4.append(_event_from_day(
                by_day, day, STREAK_MONTH_PTS,
                f"Racha: semana {n} consecutiva, 1 mes ({fmt_week(monday)})",
            ))
        i += run
    n2, n4 = len(ev2), len(ev4)
    return (
        RuleLine(
            key="racha_2s",
            label="Racha 2 semanas",
            points=n2 * STREAK_PAIR_PTS,
            count=n2,
            detail=f"{n2} × {STREAK_PAIR_PTS}" if n2 else "sin racha",
            events=ev2,
        ),
        RuleLine(
            key="racha_mes",
            label="Racha 1 mes",
            points=n4 * STREAK_MONTH_PTS,
            count=n4,
            detail=f"{n4} × {STREAK_MONTH_PTS}" if n4 else "sin racha",
            events=ev4,
        ),
    )


def score_rutina(days_in_window: list[date], by_day: dict[date, list[Attendance]]) -> RuleLine:
    by_week: dict[date, list[date]] = defaultdict(list)
    for day in days_in_window:
        by_week[week_monday(day)].append(day)
    points = 0
    scoring_days = 0
    parts: dict[int, int] = defaultdict(int)
    events: list[PointEvent] = []
    for monday, week_days in by_week.items():
        for i, day in enumerate(sorted(week_days), start=1):
            pts = RUTINA_PTS.get(i, 0)
            if pts:
                points += pts
                scoring_days += 1
                parts[i] += 1
                events.append(_event_from_day(
                    by_day, day, pts,
                    f"{ORDS[i]} día de la semana {fmt_week(monday)}",
                ))
    events.sort(key=lambda e: (e.date, e.time or ""))
    detail = (
        " + ".join(f"{parts[k]} × {RUTINA_PTS[k]} (día {k})" for k in sorted(parts))
        if scoring_days
        else "sin días extra"
    )
    return RuleLine(key="rutina", label="Rutina", points=points, count=scoring_days, detail=detail, events=events)


def score_doblete(by_day: dict[date, list[Attendance]], start: date, end: date) -> RuleLine:
    events: list[PointEvent] = []
    for day, rows in sorted(by_day.items()):
        if start <= day <= end and len(rows) >= 2:
            times = ", ".join(r.time or "?" for r in rows)
            events.append(PointEvent(
                date=day,
                points=DOBLETE_PTS,
                reason=f"Doblete: {len(rows)} clases ({times})",
                time=rows[0].time,
                class_name=rows[0].class_name,
                trainer=public_trainer(rows[0].trainer),
            ))
    n = len(events)
    return RuleLine(
        key="doblete",
        label="Doblete",
        points=n * DOBLETE_PTS,
        count=n,
        detail=f"{n} × {DOBLETE_PTS}" if n else "sin dobletes",
        events=events,
    )


def qualifying_weeks(days_in_window: list[date], start: date) -> list[tuple[date, date]]:
    league_week = week_monday(start)
    by_week: dict[date, list[date]] = defaultdict(list)
    for day in days_in_window:
        monday = week_monday(day)
        if monday >= league_week:
            by_week[monday].append(day)
    out = []
    for monday, days in by_week.items():
        days = sorted(days)
        if len(days) >= 2:
            out.append((monday, days[1]))
    return sorted(out, key=lambda x: x[0])


def pending_social() -> tuple[RuleLine, RuleLine]:
    return (
        RuleLine(key="invitacion_reserva", label="I ❤️ BF reservas", points=0, count=0, detail="pendiente", pending=True),
        RuleLine(key="invitacion_conversion", label="I ❤️ BF conversiones", points=0, count=0, detail="pendiente", pending=True),
    )


def score_social(fitboxer_id: str, referrals: list[Referral], start: date, end: date) -> tuple[RuleLine, RuleLine]:
    ev_b: list[PointEvent] = []
    ev_c: list[PointEvent] = []
    for ref in referrals:
        if ref.referrer_id != fitboxer_id:
            continue
        who = ref.referred_name or "un invitado"
        if ref.booking_date and start <= ref.booking_date <= end:
            ev_b.append(PointEvent(
                date=ref.booking_date,
                points=ILBF_BOOKING_PTS,
                reason=f"I ❤️ BF: {who} reservó su clase",
            ))
        if ref.conversion_date and start <= ref.conversion_date <= end:
            ev_c.append(PointEvent(
                date=ref.conversion_date,
                points=ILBF_CONVERSION_PTS,
                reason=f"I ❤️ BF: {who} se hizo cliente",
            ))
    ev_b.sort(key=lambda e: e.date)
    ev_c.sort(key=lambda e: e.date)
    return (
        RuleLine(
            key="invitacion_reserva",
            label="I ❤️ BF reservas",
            points=len(ev_b) * ILBF_BOOKING_PTS,
            count=len(ev_b),
            detail=f"{len(ev_b)} × {ILBF_BOOKING_PTS}" if ev_b else "sin reservas de invitados",
            events=ev_b,
        ),
        RuleLine(
            key="invitacion_conversion",
            label="I ❤️ BF conversiones",
            points=len(ev_c) * ILBF_CONVERSION_PTS,
            count=len(ev_c),
            detail=f"{len(ev_c)} × {ILBF_CONVERSION_PTS}" if ev_c else "sin conversiones de invitados",
            events=ev_c,
        ),
    )


def score_fitboxer(
    fitboxer_id: str,
    display_name: str,
    attendances: list[Attendance],
    start: date,
    end: date,
    referrals: Optional[list[Referral]] = None,
    social_pending: bool = False,
    avatar_path: Optional[str] = None,
) -> PersonScore:
    by_day = _days_classes(attendances)
    all_days = sorted(by_day)
    days_before = [d for d in all_days if d < start]
    days_in_window = [d for d in all_days if start <= d <= end]
    classes_in_window = sum(len(by_day[d]) for d in days_in_window)

    vuelta = score_returns(days_before, days_in_window, by_day)
    weeks = qualifying_weeks(days_in_window, start)
    racha_2, racha_4 = score_streaks(weeks, by_day)
    rutina = score_rutina(days_in_window, by_day)
    doblete = score_doblete(by_day, start, end)
    if social_pending or referrals is None:
        inv_res, inv_conv = pending_social()
    else:
        inv_res, inv_conv = score_social(fitboxer_id, referrals, start, end)

    breakdown = [vuelta, racha_2, racha_4, rutina, doblete, inv_res, inv_conv]
    return PersonScore(
        fitboxer_id=fitboxer_id,
        display_name=display_name,
        public_key=public_key(fitboxer_id),
        points=sum(line.points for line in breakdown),
        training_days=len(days_in_window),
        classes=classes_in_window,
        avatar_path=avatar_path,
        breakdown=breakdown,
    )


def rank_scores(scores: list[PersonScore]) -> list[PersonScore]:
    return sorted(
        scores,
        key=lambda s: (-s.points, -s.training_days, s.display_name.casefold(), s.public_key),
    )


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def _att(fid: str, day: date, *class_ids: str, time: str = "18:30") -> list[Attendance]:
    if not class_ids:
        class_ids = (f"{fid}-{day.isoformat()}",)
    return [
        Attendance(fid, cid, day, time=time if i == 0 else f"{18+i}:00", trainer="Alex", class_name="Fitboxing")
        for i, cid in enumerate(class_ids)
    ]


def run_self_tests() -> None:
    start = date(2026, 9, 2)
    end = date(2026, 11, 18)
    fid = "fb-test"

    def run(rows: list[Attendance], s: date = start, e: date = end, refs=None, pending: bool = True) -> PersonScore:
        return score_fitboxer(fid, "Ana P.", rows, s, e, referrals=refs, social_pending=pending)

    s = run(_att(fid, date(2026, 9, 3)))
    assert s.breakdown[0].points == 0

    s = run(_att(fid, date(2026, 8, 20)) + _att(fid, date(2026, 9, 3)))
    assert s.breakdown[0].points == 14 and s.breakdown[0].events[0].reason.startswith("Vuelta tras 14")

    s = run(_att(fid, date(2026, 8, 4)) + _att(fid, date(2026, 9, 3)))
    assert s.breakdown[0].points == 20 and "1 mes" in s.breakdown[0].events[0].reason

    s = run(_att(fid, date(2026, 7, 25)) + _att(fid, date(2026, 9, 3)))
    assert s.breakdown[0].points == 20

    days = [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 5), date(2026, 9, 6)]
    s = run([a for d in days for a in _att(fid, d)])
    rutina = next(x for x in s.breakdown if x.key == "rutina")
    assert rutina.points == 18 and len(rutina.events) == 3

    s = run(_att(fid, date(2026, 8, 31)) + _att(fid, date(2026, 9, 1)) + _att(fid, date(2026, 9, 3)))
    assert next(x for x in s.breakdown if x.key == "rutina").points == 0
    assert next(x for x in s.breakdown if x.key == "racha_2s").points == 0

    s = run(_att(fid, date(2026, 9, 3), "c1", "c2", "c3"))
    doblete = next(x for x in s.breakdown if x.key == "doblete")
    assert doblete.points == 2 and doblete.count == 1 and "3 clases" in doblete.events[0].reason
    assert s.training_days == 1 and s.classes == 3

    rows: list[Attendance] = []
    for i in range(4):
        mon = date(2026, 9, 7) + timedelta(days=7 * i)
        rows += _att(fid, mon) + _att(fid, mon + timedelta(days=2))
    s = run(rows)
    racha2 = next(x for x in s.breakdown if x.key == "racha_2s")
    racha4 = next(x for x in s.breakdown if x.key == "racha_mes")
    assert racha2.points == 20 and racha2.count == 2 and len(racha2.events) == 2
    assert racha4.points == 14 and racha4.count == 1

    rows = []
    for i in (0, 1, 3, 4):
        mon = date(2026, 9, 7) + timedelta(days=7 * i)
        rows += _att(fid, mon) + _att(fid, mon + timedelta(days=2))
    s = run(rows)
    assert next(x for x in s.breakdown if x.key == "racha_2s").points == 20
    assert next(x for x in s.breakdown if x.key == "racha_mes").points == 0

    a = score_fitboxer("a", "Zoe Z.", _att("a", date(2026, 9, 3)) + _att("a", date(2026, 9, 4)), start, end, social_pending=True)
    b = score_fitboxer("b", "Ana A.", _att("b", date(2026, 9, 3)) + _att("b", date(2026, 9, 4)), start, end, social_pending=True)
    c = score_fitboxer("c", "Ana A.", _att("c", date(2026, 9, 3)), start, end, social_pending=True)
    ranked = rank_scores([a, b, c])
    assert [p.display_name for p in ranked] == ["Ana A.", "Zoe Z.", "Ana A."]

    # Social
    refs = [
        Referral("fb-test", "g1", "Luis M.", date(2026, 9, 10), date(2026, 10, 1)),
        Referral("fb-test", "g2", "Eva R.", date(2026, 8, 1), None),  # booking before start
        Referral("other", "g3", "No", date(2026, 9, 10), None),
    ]
    s = run(_att(fid, date(2026, 9, 3)), refs=refs, pending=False)
    rb = next(x for x in s.breakdown if x.key == "invitacion_reserva")
    rc = next(x for x in s.breakdown if x.key == "invitacion_conversion")
    assert rb.points == 16 and rb.count == 1 and "Luis M." in rb.events[0].reason
    assert rc.points == 30 and rc.count == 1
    assert s.points == 16 + 30  # only one training day (0 rutina) + social

    # Name disambiguation
    p1 = score_fitboxer("x1", "Jennifer B.", _att("x1", date(2026, 9, 3)), start, end, social_pending=True)
    p2 = score_fitboxer("x2", "Jennifer B.", _att("x2", date(2026, 9, 3)), start, end, social_pending=True)
    disambiguate_names([p1, p2], {
        "x1": ("Jennifer", "Bravo", None),
        "x2": ("Jennifer", "Barrios", None),
    })
    assert {p1.display_name, p2.display_name} == {"Jennifer Br.", "Jennifer Ba."}

    print("self-tests: ok")


# ---------------------------------------------------------------------------
# Database (read only) and portal (read only)
# ---------------------------------------------------------------------------

def _redact(msg: str) -> str:
    text = str(msg)
    for key in ("SUPABASE_DB_URL", "SUPABASE_DB_PASSWORD", "H4C_PASSWORD", "SUPABASE_DB_USER"):
        val = os.environ.get(key)
        if val:
            text = text.replace(val, "<redacted>")
    return text.split("\n")[0][:400]


def _inherit_extract_env() -> None:
    """If extract.py is running with extra DB env vars, reuse them in memory."""
    needed = ("SUPABASE_DB_USER", "SUPABASE_DB_PASSWORD", "SUPABASE_REGION", "H4C_PG_DEADLINE")
    if os.environ.get("SUPABASE_DB_PASSWORD"):
        return
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        if "extract.py" not in cmd:
            continue
        try:
            raw = (proc / "environ").read_bytes().split(b"\x00")
        except Exception:
            continue
        copied = []
        for item in raw:
            if b"=" not in item:
                continue
            k, v = item.split(b"=", 1)
            try:
                ks = k.decode()
            except Exception:
                continue
            if ks in needed and ks not in os.environ:
                os.environ[ks] = v.decode()
                copied.append(ks)
        if copied:
            log.info("Reutilizo variables de conexión del extractor en marcha (%s)", ", ".join(copied))
        return


def _import_connect() -> Callable[[], Any]:
    if str(H4C_DIR) not in sys.path:
        sys.path.insert(0, str(H4C_DIR))
    from extract import connect_postgres  # type: ignore
    return connect_postgres


def connect_readonly(attempts: int = 5):
    _inherit_extract_env()
    connect_postgres = _import_connect()
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            conn = connect_postgres()
            conn.prepare_threshold = None
            conn.autocommit = False
            return conn
        except Exception as exc:
            last = exc
            wait = min(20.0, 1.5 * (2 ** i))
            log.warning("Conexión fallida (intento %s/%s): %s; espero %.1fs", i + 1, attempts, _redact(str(exc)), wait)
            time.sleep(wait)
    raise RuntimeError(f"No pude conectar a Postgres: {_redact(str(last))}")


LOAD_SQL = """
SELECT
  p.fitboxer_id,
  c.id AS class_id,
  (c.starts_at AT TIME ZONE %s)::date AS class_date,
  to_char(c.starts_at AT TIME ZONE %s, 'HH24:MI') AS class_time,
  c.trainer_name,
  COALESCE(NULLIF(s.name, ''), 'Fitboxing') AS class_name
FROM h4c.class_participants p
JOIN h4c.classes c ON c.id = p.class_id
LEFT JOIN h4c.schedule_slots s ON s.id = c.schedule_slot_id
WHERE p.attendance = 'attended'
  AND c.starts_at IS NOT NULL
  AND COALESCE(c.is_cancelled, false) = false
  AND p.fitboxer_id IS NOT NULL

UNION

SELECT
  w.fitboxer_id,
  COALESCE(w.class_id, w.id) AS class_id,
  (COALESCE(c.starts_at, w.started_at) AT TIME ZONE %s)::date AS class_date,
  to_char(COALESCE(c.starts_at, w.started_at) AT TIME ZONE %s, 'HH24:MI') AS class_time,
  c.trainer_name,
  COALESCE(NULLIF(s.name, ''), 'Fitboxing') AS class_name
FROM h4c.workouts w
LEFT JOIN h4c.classes c ON c.id = w.class_id
LEFT JOIN h4c.schedule_slots s ON s.id = c.schedule_slot_id
WHERE w.fitboxer_id IS NOT NULL
  AND COALESCE(c.starts_at, w.started_at) IS NOT NULL
  AND COALESCE(c.is_cancelled, false) = false
"""

NAMES_SQL = """
SELECT fitboxer_id, profile_id, first_name, last_name, name, avatar, status_code
FROM h4c.fitboxers
"""

MAX_ATTENDED_SQL = """
SELECT max(class_date) FROM (
  SELECT (c.starts_at AT TIME ZONE %s)::date AS class_date
  FROM h4c.class_participants p
  JOIN h4c.classes c ON c.id = p.class_id
  WHERE p.attendance = 'attended' AND c.starts_at IS NOT NULL
    AND COALESCE(c.is_cancelled, false) = false
) t
"""

BOOKING_SQL = """
SELECT p.fitboxer_id, min((c.starts_at AT TIME ZONE %s)::date)
FROM h4c.class_participants p
JOIN h4c.classes c ON c.id = p.class_id
WHERE p.attendance IN ('attended', 'no_show', 'reserved', 'checked_in')
  AND c.starts_at IS NOT NULL
  AND COALESCE(c.is_cancelled, false) = false
  AND p.fitboxer_id = ANY(%s)
GROUP BY 1
"""

CONV_SQL = """
SELECT fitboxer_id, min(started_at) FROM h4c.memberships
WHERE fitboxer_id = ANY(%s) GROUP BY 1
"""

ORDER_S_SQL = """
SELECT fitboxer_id, min(ordered_at) FROM h4c.orders
WHERE fitboxer_id = ANY(%s) AND product_type = 'S' GROUP BY 1
"""

EVENT_CONV_SQL = """
SELECT fitboxer_id, min(occurred_at)
FROM h4c.fitboxer_status_events
WHERE fitboxer_id = ANY(%s)
  AND (query_type IN ('5', 'cm', 'clt') OR status_code IN ('4', '5'))
GROUP BY 1
"""


@dataclass
class Directory:
    names: dict[str, tuple[Optional[str], Optional[str], Optional[str]]]
    avatars: dict[str, Optional[str]]
    profiles: dict[str, Optional[str]]  # fitboxer_id -> profile_id
    id_by_any: dict[str, str]  # profile or fitboxer id -> fitboxer_id


def load_attendance(conn, start: date, end: date) -> tuple[list[Attendance], Directory, Optional[date], str]:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SHOW transaction_read_only")
        flag = cur.fetchone()[0]
        if str(flag).lower() not in {"on", "true", "1"}:
            raise RuntimeError("La transacción no quedó en modo read-only")
        try:
            cur.execute("UPDATE h4c.clubs SET alias = alias WHERE false")
            raise RuntimeError("La transacción read-only permitió un UPDATE; aborto por seguridad")
        except Exception as exc:
            if "read-only" not in str(exc).lower() and "read only" not in str(exc).lower():
                raise RuntimeError(f"El chequeo read-only no falló como se esperaba: {_redact(str(exc))}") from exc
        conn.rollback()

        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(LOAD_SQL, (TZ_NAME, TZ_NAME, TZ_NAME, TZ_NAME))
        raw_rows = cur.fetchall()
        cur.execute(MAX_ATTENDED_SQL, (TZ_NAME,))
        max_day = cur.fetchone()[0]

        cur.execute(NAMES_SQL)
        names: dict[str, tuple[Optional[str], Optional[str], Optional[str]]] = {}
        avatars: dict[str, Optional[str]] = {}
        profiles: dict[str, Optional[str]] = {}
        id_by_any: dict[str, str] = {}
        for fid, pid, first, last, name, avatar, _status in cur.fetchall():
            names[fid] = (first, last, name)
            path = (avatar or "").strip() or None
            if path and path != DEFAULT_AVATAR:
                avatars[fid] = path
            profiles[fid] = pid
            id_by_any[fid] = fid
            if pid:
                id_by_any[pid] = fid

        readonly = flag
    rows = []
    for fid, cid, day, hhmm, trainer, cname in raw_rows:
        if day is None:
            continue
        rows.append(Attendance(
            fitboxer_id=fid,
            class_id=cid,
            day=day,
            time=hhmm,
            trainer=trainer,
            class_name=cname,
        ))
    directory = Directory(names=names, avatars=avatars, profiles=profiles, id_by_any=id_by_any)
    return rows, directory, max_day, str(readonly)


def load_referral_dates(conn, ids: list[str]) -> dict[str, dict[str, Optional[date]]]:
    """First booking / conversion evidence for a set of fitboxer ids. Read-only."""
    out: dict[str, dict[str, Optional[date]]] = {i: {"booking": None, "conversion": None} for i in ids}
    if not ids:
        return out
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(BOOKING_SQL, (TZ_NAME, ids))
        for fid, d in cur.fetchall():
            out.setdefault(fid, {"booking": None, "conversion": None})["booking"] = to_madrid_date(d)
        cur.execute(CONV_SQL, (ids,))
        memb = {fid: to_madrid_date(ts) for fid, ts in cur.fetchall()}
        cur.execute(ORDER_S_SQL, (ids,))
        signup = {fid: to_madrid_date(ts) for fid, ts in cur.fetchall()}
        cur.execute(EVENT_CONV_SQL, (ids,))
        events = {fid: to_madrid_date(ts) for fid, ts in cur.fetchall()}
        for fid in set(memb) | set(signup) | set(events) | set(out):
            out.setdefault(fid, {"booking": None, "conversion": None})
            cands = [d for d in (memb.get(fid), signup.get(fid), events.get(fid)) if d]
            if cands:
                out[fid]["conversion"] = min(cands)
    return out


ILBF_QUERY_HEADER = "query ({vars}) {{\n{body}\n}}"
ILBF_ONE = """  p{i}: fitboxer(profileId: $id{i}) {{
    id
    referredCount
    referreds {{ fitboxerId name clt_tip statusTime clubId }}
  }}"""


def _portal_client():
    if str(H4C_DIR) not in sys.path:
        sys.path.insert(0, str(H4C_DIR))
    from h4c_client import H4CClient, H4CError  # type: ignore
    return H4CClient, H4CError


def load_referrals_from_portal(directory: Directory, date_facts: Callable[[list[str]], dict[str, dict[str, Optional[date]]]]) -> Optional[list[Referral]]:
    """Read-only GraphQL fitboxer.referreds (the I 💛 BF screen). None if portal unavailable."""
    if not os.environ.get("H4C_PASSWORD"):
        log.warning("H4C_PASSWORD no está definida; I ❤️ BF se queda pendiente")
        return None
    H4CClient, H4CError = _portal_client()
    try:
        client = H4CClient(email=H4C_EMAIL)
        info = client.login()
        log.info("Portal: sesión de solo lectura iniciada (rol %s)", (info.get("current_role") or {}).get("role"))
    except Exception as exc:
        log.warning("No pude entrar al portal para I ❤️ BF: %s", _redact(str(exc)))
        return None

    items = [(fid, pid or fid) for fid, pid in directory.profiles.items()]
    batch_size = 12
    raw_refs: list[dict[str, Any]] = []
    errors = 0
    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        variables = {f"id{i}": ident for i, (_fid, ident) in enumerate(chunk)}
        var_decl = ", ".join(f"$id{i}: String" for i in range(len(chunk)))
        body = "\n".join(ILBF_ONE.format(i=i) for i in range(len(chunk)))
        query = ILBF_QUERY_HEADER.format(vars=var_decl, body=body)
        try:
            data = client.graphql(query, variables)
        except Exception as exc:
            errors += 1
            log.warning("Lote I ❤️ BF %s–%s falló: %s", start + 1, start + len(chunk), _redact(str(exc)))
            time.sleep(0.4)
            # retry one by one
            for _fid, ident in chunk:
                try:
                    one = client.graphql(
                        "query($id: String){ fitboxer(profileId: $id){ id referredCount referreds { fitboxerId name clt_tip statusTime clubId } } }",
                        {"id": ident},
                    )
                    fb = (one or {}).get("fitboxer") or {}
                    for r in fb.get("referreds") or []:
                        raw_refs.append({"referrer": _fid, **r})
                except Exception as exc2:
                    errors += 1
                    log.warning("I ❤️ BF individual falló: %s", _redact(str(exc2)))
            continue
        for i, (fid, _ident) in enumerate(chunk):
            fb = (data or {}).get(f"p{i}") or {}
            for r in fb.get("referreds") or []:
                raw_refs.append({"referrer": fid, **r})
        if (start // batch_size) % 10 == 0:
            log.info("I ❤️ BF: consultados %s/%s perfiles", min(start + batch_size, len(items)), len(items))
        time.sleep(0.08)

    log.info("I ❤️ BF: %s invitaciones en bruto, errores de lote/unidad=%s", len(raw_refs), errors)

    referred_ids = []
    for r in raw_refs:
        rid = r.get("fitboxerId") or r.get("id")
        real = directory.id_by_any.get(rid, rid)
        if real:
            referred_ids.append(real)
    facts = date_facts(sorted(set(referred_ids)))

    referrals: list[Referral] = []
    for r in raw_refs:
        referrer = r.get("referrer")
        rid_raw = r.get("fitboxerId") or r.get("id")
        if not referrer or not rid_raw:
            continue
        real = directory.id_by_any.get(rid_raw, rid_raw)
        tip = str(r.get("clt_tip") or "")
        st = to_madrid_date(r.get("statusTime"))
        fact = facts.get(real) or {"booking": None, "conversion": None}
        booking = fact.get("booking")
        if booking is None and tip in {"2", "3"}:
            booking = st
        conversion = fact.get("conversion")
        if conversion is None and tip in {"4", "5"}:
            conversion = st
        # tip 6: statusTime is the baja date — never use it as booking or conversion
        first, last, full = directory.names.get(real, (None, None, None))
        if not any((first, last, full)):
            # fall back to portal first name + initial only
            disp = public_name(None, None, r.get("name"))
        else:
            disp = public_name(first, last, full)
        referrals.append(Referral(
            referrer_id=referrer,
            referred_id=real,
            referred_name=disp,
            booking_date=booking,
            conversion_date=conversion,
        ))
    with_b = sum(1 for r in referrals if r.booking_date)
    with_c = sum(1 for r in referrals if r.conversion_date)
    log.info("I ❤️ BF: %s relaciones, %s con fecha de reserva, %s con fecha de conversión", len(referrals), with_b, with_c)
    return referrals


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------

def _is_real_avatar(path: Optional[str]) -> bool:
    if not path:
        return False
    path = path.strip()
    if not path or path == DEFAULT_AVATAR or path.endswith("/" + DEFAULT_AVATAR):
        return False
    return True


def download_photos(scores: list[PersonScore], out_dir: Path) -> None:
    """Download and resize public portal photos for the published ranking only."""
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow no está instalado; las fotos se enlazarán al original público")
        for s in scores:
            if _is_real_avatar(s.avatar_path):
                s.photo = IMG_BASE + s.avatar_path  # type: ignore[operator]
        return

    img_dir = out_dir / "img"
    img_dir.mkdir(parents=True, exist_ok=True)
    ok = skip = fail = 0
    for s in scores:
        if not _is_real_avatar(s.avatar_path):
            skip += 1
            s.photo = None
            continue
        dest = img_dir / f"{s.public_key}.jpg"
        url = IMG_BASE + s.avatar_path  # type: ignore[operator]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "liga-ourense/1 (read-only)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            im = Image.open(io.BytesIO(data))
            im = im.convert("RGB")
            im.thumbnail((PHOTO_SIZE, PHOTO_SIZE))
            im.save(dest, "JPEG", quality=85, optimize=True)
            s.photo = f"img/{s.public_key}.jpg"
            ok += 1
        except Exception as exc:
            fail += 1
            s.photo = None
            log.warning("Foto no descargada para %s: %s", s.display_name, type(exc).__name__)
    keep = {Path(s.photo).name for s in scores if s.photo and not str(s.photo).startswith("http")}
    removed = 0
    for path in img_dir.iterdir():
        if path.is_file() and path.name not in keep:
            path.unlink()
            removed += 1
    if removed:
        log.info("Fotos antiguas eliminadas de site/img: %s", removed)
    log.info("Fotos: %s guardadas, %s sin foto de perfil, %s errores", ok, skip, fail)


# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------

def build_payload(
    title: str,
    start: date,
    end: date,
    top_n: int,
    ranked: list[PersonScore],
    generated_at: datetime,
    data_until: Optional[date],
    attended_people: int,
    attended_classes: int,
    social_pending: bool,
    referral_count: int,
) -> dict[str, Any]:
    top = ranked[:top_n]
    return {
        "title": title,
        "club": "Brooklyn Fitboxing Ourense",
        "timezone": TZ_NAME,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "generated_at": generated_at.isoformat(),
        "data_until": data_until.isoformat() if data_until else None,
        "top": top_n,
        "total_ranked": len(ranked),
        "people_with_attendance": attended_people,
        "attended_classes": attended_classes,
        "social_pending": social_pending,
        "referral_links": referral_count,
        "rules": {
            "vuelta_14": RETURN_SHORT_PTS,
            "vuelta_30": RETURN_LONG_PTS,
            "racha_2_semanas": STREAK_PAIR_PTS,
            "racha_4_semanas": STREAK_MONTH_PTS,
            "rutina": RUTINA_PTS,
            "doblete": DOBLETE_PTS,
            "ilbf_reserva": ILBF_BOOKING_PTS,
            "ilbf_conversion": ILBF_CONVERSION_PTS,
            "mes_en_dias": RETURN_LONG_DAYS,
        },
        "rankings": [p.as_public_dict(i) for i, p in enumerate(top, start=1)],
    }


def render_site(payload: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data_json = json.dumps(payload, ensure_ascii=False, indent=2)
    (out_dir / "data.json").write_text(data_json + "\n", encoding="utf-8")
    template_path = HERE / "template.html"
    if not template_path.exists():
        raise FileNotFoundError(f"Falta la plantilla {template_path}")
    html = template_path.read_text(encoding="utf-8")
    html = html.replace("__LIGA_DATA_JSON__", data_json)
    html = html.replace("__LIGA_TITLE__", payload["title"])
    out = out_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def format_madrid_now() -> datetime:
    return datetime.now(MADRID).replace(microsecond=0)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Calcula la liga interna (solo lectura) y genera una web estática.")
    p.add_argument("--start", required=False, help="Fecha de inicio YYYY-MM-DD (obligatoria salvo --self-test)")
    p.add_argument("--end", help="Fecha de fin inclusive YYYY-MM-DD (por defecto, hoy en Europe/Madrid)")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="Directorio de salida (por defecto ./site)")
    p.add_argument("--top", type=int, default=40)
    p.add_argument("--title", default=DEFAULT_TITLE)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--skip-portal", action="store_true", help="No consulta el portal; I ❤️ BF queda pendiente")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def parse_iso_date(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"{flag} debe ser YYYY-MM-DD") from exc


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.self_test:
        run_self_tests()
        return 0
    if not args.start:
        raise SystemExit("Falta --start YYYY-MM-DD (o usa --self-test)")

    start = parse_iso_date(args.start, "--start")
    end = parse_iso_date(args.end, "--end") if args.end else format_madrid_now().date()
    if end < start:
        raise SystemExit("--end no puede ser anterior a --start")
    if args.top < 1:
        raise SystemExit("--top debe ser >= 1")

    log.info("Liga %s .. %s (Europe/Madrid), top %s", start, end, args.top)
    conn = connect_readonly()
    try:
        rows, directory, data_until, flag = load_attendance(conn, start, end)
        log.info("Transacción read-only=%s; %s asistencias; datos hasta %s; fotos de perfil=%s",
                 flag, len(rows), data_until, len(directory.avatars))
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    referrals: Optional[list[Referral]] = None
    social_pending = True
    if not args.skip_portal:
        def _facts(ids: list[str]) -> dict[str, dict[str, Optional[date]]]:
            c = connect_readonly()
            try:
                return load_referral_dates(c, ids)
            finally:
                try:
                    c.rollback()
                except Exception:
                    pass
                try:
                    c.close()
                except Exception:
                    pass
        referrals = load_referrals_from_portal(directory, _facts)
        social_pending = referrals is None

    by_person: dict[str, list[Attendance]] = defaultdict(list)
    for row in rows:
        by_person[row.fitboxer_id].append(row)

    # Include referrers who may have points even with no attendance in the window
    candidate_ids = set(by_person)
    if referrals:
        candidate_ids.update(r.referrer_id for r in referrals)

    scores: list[PersonScore] = []
    people_in_window = 0
    seen_classes: set[str] = set()
    for fid in candidate_ids:
        first, last, name = directory.names.get(fid, (None, None, None))
        person = score_fitboxer(
            fid,
            public_name(first, last, name),
            by_person.get(fid, []),
            start,
            end,
            referrals=referrals or [],
            social_pending=social_pending,
            avatar_path=directory.avatars.get(fid),
        )
        if person.training_days > 0 or person.points > 0:
            if person.training_days > 0:
                people_in_window += 1
            scores.append(person)
        for a in by_person.get(fid, []):
            if start <= a.day <= end:
                seen_classes.add(a.class_id)

    disambiguate_names(scores, directory.names)
    ranked = rank_scores(scores)
    out_dir = Path(args.out).resolve()
    download_photos(ranked[: args.top], out_dir)

    generated_at = format_madrid_now()
    payload = build_payload(
        title=args.title,
        start=start,
        end=end,
        top_n=args.top,
        ranked=ranked,
        generated_at=generated_at,
        data_until=data_until,
        attended_people=people_in_window,
        attended_classes=len(seen_classes),
        social_pending=social_pending,
        referral_count=len(referrals or []),
    )
    index = render_site(payload, out_dir)

    top = payload["rankings"]
    log.info(
        "Generado %s — %s personas con asistencia, %s clases, top=%s, social_pending=%s",
        index, people_in_window, len(seen_classes), len(top), social_pending,
    )
    for row in top[:10]:
        log.info("  #%s %s — %s pt (%s días)", row["rank"], row["name"], row["points"], row["training_days"])
    if data_until and data_until < end:
        log.warning("Los datos de asistencia en Supabase llegan solo hasta %s; el periodo pedido acaba el %s.", data_until, end)
    print(f"Web generada en {index}")
    print(f"Periodo {start} .. {end} ({TZ_NAME}). Actualizado {generated_at.isoformat()}")
    print(f"Personas con asistencia: {people_in_window}. En el top: {len(top)}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
