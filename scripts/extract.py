#!/usr/bin/env python3
"""Load My Hit4Change portal data into the h4c Supabase schema.

Read-only against the portal (GraphQL / search queries only). Upserts into
Postgres are idempotent (ON CONFLICT DO UPDATE). Credentials and connection
strings are never logged, printed, or written to files by this script.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import random
import re
import select
import socket
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from h4c_client import H4CClient, H4CError, sleep as client_sleep
from staging_store import Stage

log = logging.getLogger("h4c.extract")

MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_EMAIL = ""  # usar la variable de entorno H4C_EMAIL
BACKFILL_START = date(2024, 11, 1)
DEFAULT_PAUSE = 0.4
RECENT_STAGE = SCRIPT_DIR / "staging" / "h4c_stage_recent.sqlite"
RECENT_LOCK = SCRIPT_DIR / "state" / "extract_recent.lock"
INCREMENTAL_STAGE = SCRIPT_DIR / "staging" / "h4c_stage_incremental.sqlite"
PG_OVERRIDE_FILE = SCRIPT_DIR / "state" / "pg.env"
STATUS_TYPES = ("1", "fb", "clt", "5", "cm", "out")
# Portal fitboxer/ficha ids are 24-char hex Mongo ObjectIds. Some bookings
# instead carry a Salesforce-style id (e.g. 18-char 00Q… lead), which the
# ficha query rejects: "not a valid MongoObjectId". Those are skippable.
_MONGO_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")


def is_mongo_object_id(value: Optional[str]) -> bool:
    return bool(value) and bool(_MONGO_OBJECT_ID_RE.fullmatch(str(value)))


def _is_invalid_mongo_object_id_error(err: Any) -> bool:
    return "not a valid mongoobjectid" in str(err or "").lower()

FREETRIAL_TYPES = ("M", "C")
GENERATED_COLS = {"amount_eur"}
CA_CANDIDATES = [
    SCRIPT_DIR / "certs" / "prod-ca-2021.crt",
    Path("/workspace/h4c/certs/prod-ca-2021.crt"),
]

# ---------------------------------------------------------------------------
# GraphQL (proven field selections)
# ---------------------------------------------------------------------------

Q_CLUB = """
query {
  club {
    id alias avatar country email telephone symbol currency
    fitcoins { id price }
    plans {
      id name shortName price type commitmentMode cash bookIncluded bookAmount
      serviceAmount enrollmentFee cancellationFee displayMode offerConfigs isRemoved config
    }
    address address2 parking zipCode city facebookUid instagramUid
    latitude longitude mapCenterLatitude mapCenterLongitude zoomMap
    staff
  }
}
"""

Q_DASHBOARD = """
query($month: Int!, $year: Int!) {
  dashboard(month: $month, year: $year) {
    id symbol totFac totFacHoy soc socHoy clt cltHoy fit
    vtaAcu vtaMes offers offersHoy fb fbHoy
    addSoc24 addSoc24Hoy addSoc addSocHoy addClt addCltHoy baj bajHoy
    woNoShow fbNoShow debNoShow
    facRatio { target value }
    newMbmMes
    conSoc
    conRatio { target value }
    bajRatio { target value }
    unpaid engage AWMode HALFMode pauseMode
    sessionsGifts sessionsExtension
  }
}
"""

Q_DASHBOARD_SUMMARY = """
query($month: Int!, $year: Int!) {
  dashboardSummary(month: $month, year: $year) {
    id offers offersHoy woTarget engage
  }
}
"""

Q_SCHEDULE = """
query($addRemoved: Boolean) {
  schedule(addRemoved: $addRemoved) {
    id isRemoved name clubId capacity room hour day aggregatorLimit
  }
}
"""

Q_CLASSES_FINISHED = """
query($from: String!, $to: String!) {
  classes(from: $from, to: $to, finished: true) {
    id rsv_val fb_val tim wrk capacity type aggregatorsCount
    off tim_att tim_fin private
    tra { id name show_img }
  }
}
"""

Q_CLASSES_UPCOMING = """
query($from: String!, $to: String!) {
  classes(from: $from, to: $to, finished: false) {
    id rsv_val fb_val tim dsp off tim_att tim_fin
    tra { id name show_img }
    private type aggregatorsCount capacity
  }
}
"""

Q_CLASS = """
query($id: String!) {
  class(id: $id) {
    id tim rsv_val fb_val aggregatorsCount capacity type tim_att tim_fin challenge
    tra { id name show_img }
    workouts {
      id wrk syn pow n_saco total_karma h4cHouse h4cHouseIsDefault donatedEnergy type
      fitboxer {
        id fitboxerId aggregatorId aggregatorName name show_img eml clt_tip
        district { id alias }
      }
    }
    no_show {
      id fitboxerId aggregatorId name show_img eml clt_tip
    }
    activity { id type user when data system }
  }
}
"""

Q_NEXT_CLASS = """
query($id: String!) {
  nextClass(id: $id) {
    id
    club { id alias }
    type endClassId
    tra { id name show_img }
    tim tim_att tim_fin rsv_val fb_val aggregatorsCount aggregatorLimit capacity
    dsp off bags challenge
    bookings {
      id fitboxerId aggregatorId aggregatorName name nick show_img eml clt_tip
      level trn_imp spe_ofe status statusTime sessionsCount lastSessionDate
      isBookingCheckIn(nextClassId: $id)
      district { id alias }
    }
    waiting {
      id fitboxerId name show_img eml clt_tip
      district { id alias }
    }
    activity { id type user when data system }
  }
}
"""

Q_SESSIONS = """
query($from: String!, $to: String!) {
  sessions(from: $from, to: $to) {
    id tim wrk syn pow n_saco clt
    rounds { mac num syn wrk pow sts }
    rounds_ok
  }
}
"""

Q_FREETRIALS = """
query($type: String!) {
  freetrials(type: $type) {
    id code created updated type clubAlias emisorId emisorName used usedBy usedByName imp webOffer
  }
}
"""

Q_FITBOXER = """
query($id: String!) {
  fitboxer(profileId: $id) {
    id fitboxerId aggregatorId aggregatorName name firstName lastName eml tfn nick
    clt_tip show_img salesForceId birthdate gender dni lastSessionDate sessionsCount
    lastNoShow w_avg syn pow engage engageHistory h4cHouse isAnywhereActive level
    city zipPostalCode mainGoal newEngagement availableExtensions
    club { id alias }
    activeOffer { id date productType offerType expireIn }
    activeSubscription { id amount discountNextCharge name planId fee }
    events(velneo: false) {
      id status time when metadata source
      club { id name alias }
      payment { id name imp }
      order { id description amount currency }
      trainer { id name }
      subscription { id name offReason offReasonText }
    }
    bookings {
      id cla_cen classGlobalTime mod_tim nextClassId globalPass
      club { id name }
      class { id type }
      guestIds isGuest ownerId
    }
    classSubscriptions { id cla_cen classGlobalTime mod_tim nextClassId }
    sessionsPacks { id name type expireIn available gpAvailable total globalPass }
    benefits { id code type subtype name description dateAchieved sourceType metadata url expiresAt }
    rewards { id code type subtype name description dateAchieved sourceType metadata url expiresAt }
  }
}
"""

Q_LEADS = """
query {
  leads {
    id FirstName LastName Email Phone CreatedDate LastModifiedDate LastActivityDate
    Status LeadSource Objections__c InputChannel__c PaymentRefusalReason__c Other_source__c
  }
}
"""

Q_AGGREGATORS = """
query {
  fitboxersFromAggregator {
    id fitboxerId name eml aggregatorName aggregatorId show_img
  }
}
"""

Q_MEMBERSHIPS = """
query {
  memberships {
    id fitboxerId fitboxerPreview planId name initTime fee amount payDay payHour
    discountNextCharge unpaidments voucherType billingPeriod recurringSchedule nextCharge
  }
}
"""

Q_UNSUBS = """
query($from: String!, $to: String!) {
  unsubs(from: $from, to: $to) {
    id fitboxerId fitboxerPreview planId name offRequestTime offTime offReason
    offReasonText valoration amount recurringSchedule downgrade lastLevel
  }
}
"""

Q_ORDERS = """
query orders($from: String!, $to: String!) {
  orders(from: $from, to: $to) {
    id productType productId description date discount amount fitboxerId
    fitboxerPreview paymentReference status statusTime paymentMethod metadata voucherType
  }
}
"""

Q_STATUS = """
query($type: String!, $from: String!, $to: String!) {
  fitboxerStatus(type: $type, from: $from, to: $to) {
    id status time fitboxerId
    fitboxer { id fitboxerId name clt_tip }
    metadata
    prevStatus prevTime elapsed
    payment { id name imp }
    order { id description amount currency }
    subscription { id name offReason offReasonText }
    trainer { id name }
    when
  }
}
"""


# ---------------------------------------------------------------------------
# Secret-safe logging
# ---------------------------------------------------------------------------

def _secret_values() -> list[str]:
    vals = []
    for key in (
        "H4C_PASSWORD",
        "SUPABASE_DB_URL",
        "SUPABASE_DB_PASSWORD",
        "PGPASSWORD",
        "DATABASE_URL",
    ):
        v = os.environ.get(key)
        if v:
            vals.append(v)
            # also the URL-decoded password part if this looks like a URL
            if "://" in v and "@" in v:
                try:
                    cred, _ = v.split("://", 1)[1].rsplit("@", 1)
                    _u, _, pw = cred.partition(":")
                    if pw:
                        vals.append(unquote(pw))
                        vals.append(pw)
                except Exception:
                    pass
    return [v for v in vals if v]


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            for secret in _secret_values():
                if secret and secret in msg:
                    msg = msg.replace(secret, "<redacted>")
            record.msg = msg
            record.args = ()
        except Exception:
            record.msg = "<log record redacted>"
            record.args = ()
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(_RedactFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    log.addFilter(_RedactFilter())


@contextmanager
def exclusive_file_lock(path: Path, *, blocking: bool = False):
    """Advisory exclusive lock. Non-blocking by default so a tick can fail fast."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except BlockingIOError as exc:
            raise RuntimeError(f"Another recent extract holds {path}") from exc
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Date / value helpers
# ---------------------------------------------------------------------------

def ymd_slash(d: date) -> str:
    return f"{d.year}/{d.month}/{d.day}"


def ymd_dash(d: date) -> str:
    return d.isoformat()


def week_ranges(start: date, end: date) -> list[tuple[date, date]]:
    ranges = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=6), end)
        ranges.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return ranges


def month_starts(start: date, end: date) -> list[date]:
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(date(y, m, 1))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return months


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=MADRID)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # API sometimes uses +0000
    if len(text) >= 5 and (text[-5] in "+-") and text[-3] != ":" and text[-5] != "T":
        text = text[:-2] + ":" + text[-2:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        # date-only and naive timestamps from the portal are treated as Madrid
        if len(str(value).strip()) <= 10:
            dt = dt.replace(tzinfo=MADRID)
        else:
            dt = dt.replace(tzinfo=UTC)
    return dt


def parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(MADRID).date()
    if isinstance(value, date):
        return value
    dt = parse_dt(value)
    return dt.astimezone(MADRID).date() if dt else None


def as_int(value: Any) -> Optional[int]:
    if value is None or value is False or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def as_num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (list, dict, bool)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "t"):
            return True
        if low in ("false", "0", "no", "f"):
            return False
    return None


def as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text if text else None


def as_json(value: Any) -> Optional[Jsonb]:
    if value is None:
        return None
    return Jsonb(value)


def raw_of(obj: Any) -> Jsonb:
    return Jsonb(obj if obj is not None else {})


def first_present(*values: Any) -> Any:
    for v in values:
        if v is not None and v != "":
            return v
    return None


# ---------------------------------------------------------------------------
# Postgres connection (direct, session pooler, HTTP CONNECT proxy)
# ---------------------------------------------------------------------------

_proxy_guard = threading.Lock()
_proxies: dict[tuple[str, int], "_Forwarder"] = {}
_last_ok_attempt: Optional[dict[str, Any]] = None


def _load_pg_overrides() -> None:
    """Load SUPABASE_DB_USER / SUPABASE_DB_PASSWORD from a local 0600 file.

    The parent environment often has only SUPABASE_DB_URL (user postgres).
    The pooler role that actually works for this project is h4c_extract;
    its password is kept in state/pg.env and is never logged.
    """
    path = PG_OVERRIDE_FILE
    if not path.is_file():
        return
    try:
        raw = path.read_text()
    except OSError:
        return
    loaded_user = False
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        if key in {"SUPABASE_DB_USER", "SUPABASE_DB_PASSWORD"}:
            os.environ[key] = val
            if key == "SUPABASE_DB_USER":
                loaded_user = True
    if loaded_user:
        log.info("Loaded database role override from local state file")


def _parse_db_url(url: str) -> tuple[str, str, str, int, str]:
    url = url.strip()
    if "://" not in url or "@" not in url:
        raise RuntimeError("SUPABASE_DB_URL is not a Postgres URI")
    cred, after = url.split("://", 1)[1].rsplit("@", 1)
    user, _, pw = cred.partition(":")
    hostport, _, pathq = after.partition("/")
    if ":" in hostport and hostport.rsplit(":", 1)[-1].isdigit():
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
    else:
        host, port = hostport, 5432
    db, _, _q = pathq.partition("?")
    return unquote(user), unquote(pw), unquote(host), port, unquote(db) or "postgres"


def _project_ref(host: str) -> Optional[str]:
    if host.startswith("db.") and host.endswith(".supabase.co"):
        parts = host.split(".")
        if len(parts) >= 4:
            return parts[1]
    return None


def _detect_http_proxy() -> Optional[tuple[str, int]]:
    for key in ("SAND_EGRESS_TUNNEL_PROXY_ADDR", "ALL_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY"):
        raw = os.environ.get(key)
        if not raw:
            continue
        raw = raw.strip()
        if "://" in raw:
            raw = raw.split("://", 1)[1]
        raw = raw.split("/")[0]
        if raw.startswith("socks"):
            continue
        if ":" in raw:
            host, port_s = raw.rsplit(":", 1)
            if port_s.isdigit():
                return host, int(port_s)
    # Box egress tunnel (HTTP CONNECT)
    try:
        s = socket.create_connection(("127.0.0.1", 8791), timeout=0.3)
        s.close()
        return "127.0.0.1", 8791
    except OSError:
        return None


def _enable_tcp_keepalive(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
    # Linux TCP_USER_TIMEOUT (ms): fail faster when the path is black-holed.
    try:
        sock.setsockopt(socket.IPPROTO_TCP, 18, 30000)
    except OSError:
        pass


def _http_connect(proxy_host: str, proxy_port: int, target_host: str, target_port: int, timeout: float = 20.0) -> socket.socket:
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    req = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n\r\n"
    ).encode()
    s.sendall(req)
    s.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = s.recv(1024)
        if not chunk:
            break
        data += chunk
    header = data.split(b"\r\n", 1)[0]
    if b" 200 " not in header:
        s.close()
        raise RuntimeError("HTTP CONNECT failed")
    # Blocking + keepalive: do not leave the CONNECT-handshake timeout on the
    # tunnel, or a pause while we talk to the portal can kill the DB socket.
    s.settimeout(None)
    _enable_tcp_keepalive(s)
    return s


class _Forwarder(threading.Thread):
    """One local listener per upstream (host, port). Reused across DB sessions.

    Earlier versions discarded the dict entry on every reconnect but left the
    listening socket open, leaking thousands of LISTEN fds and eventually
    exhausting the egress tunnel ("Too many open files").
    """

    def __init__(
        self,
        target_host: str,
        target_port: int,
        proxy: tuple[str, int],
        connect_host: Optional[str] = None,
    ) -> None:
        super().__init__(daemon=True)
        self.target_host = target_host
        self.target_port = target_port
        self.connect_host = connect_host or target_host
        self.proxy = proxy
        self.port = 0
        self.srv: Optional[socket.socket] = None
        self.ready = threading.Event()
        self._stopped = False

    def run(self) -> None:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        self.port = srv.getsockname()[1]
        srv.listen(32)
        self.srv = srv
        self.ready.set()
        while not self._stopped:
            try:
                client, _ = srv.accept()
                _enable_tcp_keepalive(client)
            except OSError:
                return
            threading.Thread(target=self._pump, args=(client,), daemon=True).start()

    def _pump(self, client: socket.socket) -> None:
        remote = None
        try:
            remote = _http_connect(self.proxy[0], self.proxy[1], self.connect_host, self.target_port)
        except Exception:
            try:
                client.close()
            except OSError:
                pass
            return
        try:
            sockets = [client, remote]
            while True:
                readable, _, err = select.select(sockets, [], sockets, 180)
                if err:
                    break
                if not readable:
                    continue
                for sock in readable:
                    other = remote if sock is client else client
                    data = sock.recv(65536)
                    if not data:
                        return
                    other.sendall(data)
        except Exception:
            pass
        finally:
            for sock in (client, remote):
                if sock is None:
                    continue
                try:
                    sock.close()
                except OSError:
                    pass

    def alive(self) -> bool:
        return (not self._stopped) and self.is_alive() and self.port > 0 and self.srv is not None

    def stop(self) -> None:
        self._stopped = True
        srv = self.srv
        self.srv = None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass


def _start_local_forwarder(
    target_host: str,
    target_port: int,
    proxy: tuple[str, int],
    *,
    connect_host: Optional[str] = None,
    replace: bool = False,
) -> int:
    key = (target_host, target_port)
    connect_host = connect_host or target_host
    with _proxy_guard:
        existing = _proxies.get(key)
        if (
            existing is not None
            and existing.alive()
            and not replace
            and existing.connect_host == connect_host
        ):
            return existing.port
        if existing is not None:
            existing.stop()
            _proxies.pop(key, None)
        fwd = _Forwarder(target_host, target_port, proxy, connect_host=connect_host)
        fwd.start()
        if not fwd.ready.wait(5):
            fwd.stop()
            raise RuntimeError("local forwarder failed to start")
        _proxies[key] = fwd
        log.info(
            "Local CONNECT forwarder on 127.0.0.1:%s -> %s:%s (CONNECT %s)",
            fwd.port,
            target_host,
            target_port,
            connect_host,
        )
        return fwd.port


def _ssl_kwargs() -> dict[str, Any]:
    ca = os.environ.get("SUPABASE_CA_FILE")
    path = Path(ca) if ca else next((p for p in CA_CANDIDATES if p.exists()), None)
    kw: dict[str, Any] = {"gssencmode": "disable", "channel_binding": "disable"}
    mode = os.environ.get("H4C_PG_SSLMODE")
    if mode:
        kw["sslmode"] = mode
        if mode not in {"disable", "allow", "prefer"} and path and path.exists():
            kw["sslrootcert"] = str(path)
        return kw
    if path and path.exists():
        kw["sslmode"] = "verify-full"
        kw["sslrootcert"] = str(path)
    else:
        kw["sslmode"] = "require"
    return kw


def _dns_is_fake_ip(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        ip = info[4][0]
        if ip.startswith("198.18.") or ip.startswith("198.19."):
            return True
    return False


_doh_cache: dict[str, tuple[float, list[str]]] = {}
_DOH_TTL_S = 60.0
_DOH_URLS = (
    "https://cloudflare-dns.com/dns-query?name={host}&type=A",
    "https://dns.google/resolve?name={host}&type=A",
)


def _is_usable_public_ip(ip: str) -> bool:
    if not ip or ip.startswith("198.18.") or ip.startswith("198.19."):
        return False
    if ip.startswith("127.") or ip.startswith("0.") or ip.startswith("10."):
        return False
    if ip.startswith("192.168.") or ip.startswith("169.254."):
        return False
    return True


def _resolve_public_ips(hostname: str) -> list[str]:
    """Resolve A records over HTTPS so we do not use the box's 198.18 interceptor.

    The egress CONNECT tunnel can reach the Supabase pooler by public IP, but
    CONNECT to the hostname:6543 hangs (the tunnel cannot complete that hop).
    """
    import urllib.request

    now = time.time()
    cached = _doh_cache.get(hostname)
    if cached and cached[0] > now and cached[1]:
        return cached[1]
    headers = {"Accept": "application/dns-json", "User-Agent": "h4c-extractor/0.1"}
    ips: list[str] = []
    last_err: Optional[Exception] = None
    for tmpl in _DOH_URLS:
        url = tmpl.format(host=hostname)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            for ans in payload.get("Answer") or []:
                if int(ans.get("type") or 0) != 1:
                    continue
                ip = str(ans.get("data") or "").strip()
                if _is_usable_public_ip(ip) and ip not in ips:
                    ips.append(ip)
            if ips:
                break
        except Exception as exc:
            last_err = exc
            continue
    if ips:
        _doh_cache[hostname] = (now + _DOH_TTL_S, ips)
        log.info("DoH resolved %s -> %s", hostname, ", ".join(ips))
    elif last_err:
        log.warning("DoH resolve failed for %s: %s", hostname, type(last_err).__name__)
    return ips


def _try_connect(**kwargs: Any) -> psycopg.Connection:
    timeout = int(kwargs.pop("connect_timeout", 8))
    return psycopg.connect(connect_timeout=timeout, **kwargs)


def _disable_prepared_statements(conn: psycopg.Connection) -> None:
    """Transaction poolers (Supabase :6543) reuse backends across clients.

    psycopg3 treats prepare_threshold=0 as "prepare on first execute", which
    names statements _pg3_N and then races with other sessions:
    DuplicatePreparedStatement: prepared statement "_pg3_1" already exists.
    None disables server-side prepares entirely.
    """
    conn.prepare_threshold = None


def _open_psycopg(**kwargs: Any) -> psycopg.Connection:
    kwargs.setdefault("keepalives", 1)
    kwargs.setdefault("keepalives_idle", 10)
    kwargs.setdefault("keepalives_interval", 5)
    kwargs.setdefault("keepalives_count", 3)
    kwargs.setdefault("gssencmode", "disable")
    kwargs.setdefault("channel_binding", "disable")
    # Force off: 0 would prepare immediately (psycopg3), not disable.
    kwargs["prepare_threshold"] = None
    timeout = int(kwargs.pop("connect_timeout", 8))
    conn = psycopg.connect(connect_timeout=timeout, **kwargs)
    _disable_prepared_statements(conn)
    return conn


def _is_local_pg_host(host: str) -> bool:
    h = (host or "").strip()
    return h in {"", "localhost", "127.0.0.1", "::1", "/var/run/postgresql"} or h.startswith("/")


def connect_postgres() -> psycopg.Connection:
    """Open a short-lived Postgres session.

    Local TCP to the CONNECT forwarder succeeds immediately, so libpq's
    connect_timeout does not bound the SSL/handshake wait. A hard deadline
    wraps the whole attempt. Transaction-pooler port 6543 is preferred for
    brief write sessions; session 5432 is the fallback. The IPv6-only
    direct host is skipped on this box's intercepted DNS.

    A URL pointing at localhost or a Unix socket is opened directly (no
    egress proxy). Use H4C_PG_SSLMODE=disable for a local server without TLS.
    """
    _load_pg_overrides()
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL is not set")
    user, password, host, port, dbname = _parse_db_url(url)
    if _is_local_pg_host(host):
        sock_host = host or "/var/run/postgresql"
        kw: dict[str, Any] = dict(dbname=dbname, user=user, host=sock_host, port=port, **_ssl_kwargs())
        if password:
            kw["password"] = password
        conn = _open_psycopg(connect_timeout=8, **kw)
        _disable_prepared_statements(conn)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.rollback()
        log.info("Connected to local Postgres host=%s db=%s", sock_host, dbname)
        return conn
    override_user = os.environ.get("SUPABASE_DB_USER")
    override_pw = os.environ.get("SUPABASE_DB_PASSWORD")
    if override_pw:
        password = override_pw
    ssl_kw = _ssl_kwargs()
    proxy = _detect_http_proxy()
    ref = _project_ref(host)
    region = os.environ.get("SUPABASE_REGION", "eu-west-1")
    deadline = int(os.environ.get("H4C_PG_DEADLINE", "12"))

    users: list[str] = []
    if override_user and ref:
        users.append(f"{override_user}.{ref}")
    if override_user:
        users.append(override_user)
    if ref:
        users.append(f"{user}.{ref}")
    users.append(user)
    users = list(dict.fromkeys(users))

    targets: list[dict[str, Any]] = []
    if ref:
        for ph in (f"aws-1-{region}.pooler.supabase.com", f"aws-0-{region}.pooler.supabase.com"):
            for pport, kind in ((6543, "txn"), (5432, "sess")):
                for i, u in enumerate(users):
                    targets.append({
                        "label": f"{kind}-{ph}-p{pport}-u{i}",
                        "host": ph,
                        "port": pport,
                        "user": u,
                        "via_proxy": bool(proxy),
                    })
    if not _dns_is_fake_ip(host):
        targets.append({
            "label": "direct-url",
            "host": host,
            "port": port,
            "user": override_user or user,
            "via_proxy": False,
        })

    global _last_ok_attempt
    if _last_ok_attempt:
        def _same(a: dict) -> bool:
            last = _last_ok_attempt or {}
            return (
                a["host"] == last.get("host")
                and a["port"] == last.get("port")
                and a["user"] == last.get("user")
                and a["via_proxy"] == last.get("via_proxy")
            )
        targets = [a for a in targets if _same(a)] + [a for a in targets if not _same(a)]

    def _open(att: dict[str, Any]) -> psycopg.Connection:
        kw = dict(dbname=dbname, user=att["user"], password=password, **ssl_kw)
        if att["via_proxy"] and proxy:
            # Reuse the per-target listener. Recreating it on every attempt
            # leaked LISTEN sockets (the previous listener was never closed).
            # CONNECT by public IP: CONNECT to the pooler hostname hangs on
            # this box's egress tunnel, but the same port on the resolved
            # address works. libpq still gets host=hostname for SNI/verify.
            connect_host = att["host"]
            if _dns_is_fake_ip(att["host"]) or att["port"] in (5432, 6543):
                ips = _resolve_public_ips(att["host"])
                if ips:
                    idx = int(att.get("ip_index") or 0) % len(ips)
                    connect_host = ips[idx]
            local_port = _start_local_forwarder(
                att["host"], att["port"], proxy, connect_host=connect_host
            )
            kw.update(host=att["host"], hostaddr="127.0.0.1", port=local_port)
        else:
            kw.update(host=att["host"], port=att["port"])
        return _open_psycopg(connect_timeout=8, **kw)

    last_err: Optional[Exception] = None
    # A few quick tries of the preferred route, then the rest.
    ordered = targets
    for attempt in range(3):
        for att in ordered[:1] if attempt < 2 and _last_ok_attempt else ordered:
            label = att["label"]
            log.info("Connecting to Postgres (%s) try %s", label, attempt + 1)
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(_open, att)
                    conn = fut.result(timeout=deadline)
                _disable_prepared_statements(conn)
                conn.autocommit = False
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                conn.rollback()
                _last_ok_attempt = dict(att)
                log.info("Connected to Postgres (%s)", label)
                return conn
            except Exception as exc:
                last_err = exc
                msg = str(exc)
                for secret in _secret_values() + [password]:
                    if secret:
                        msg = msg.replace(secret, "<redacted>")
                log.info("Connect attempt %s failed: %s", label, msg.split("\n")[0][:240])
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"Could not connect to Postgres: {last_err}")



def upsert(
    cur: psycopg.Cursor,
    table: str,
    rows: list[dict[str, Any]],
    pk: Iterable[str],
    mode: str = "replace",
) -> int:
    if not rows:
        return 0
    pk_list = list(pk)
    # drop generated columns if present
    cleaned = []
    for row in rows:
        cleaned.append({k: v for k, v in row.items() if k not in GENERATED_COLS})
    rows = cleaned
    cols = list(rows[0].keys())
    for row in rows:
        for c in cols:
            row.setdefault(c, None)
    placeholders = "(" + ", ".join(["%s"] * len(cols)) + ")"
    col_sql = ", ".join(cols)
    pk_sql = ", ".join(pk_list)
    updates = []
    for c in cols:
        if c in pk_list:
            continue
        if c == "extracted_at":
            updates.append(f"{c}=EXCLUDED.{c}")
        elif c == "raw" and mode == "fill":
            updates.append(
                f"{c}=CASE "
                f"WHEN {table}.{c} IS NULL OR {table}.{c} = '{{}}'::jsonb THEN EXCLUDED.{c} "
                f"WHEN EXCLUDED.{c} IS NULL OR EXCLUDED.{c} = '{{}}'::jsonb THEN {table}.{c} "
                f"ELSE EXCLUDED.{c} || {table}.{c} END"
            )
        elif mode == "fill":
            updates.append(f"{c}=COALESCE(EXCLUDED.{c}, {table}.{c})")
        else:
            updates.append(f"{c}=EXCLUDED.{c}")
    conflict = "ON CONFLICT (" + pk_sql + ") DO UPDATE SET " + ", ".join(updates)
    values = [tuple(row.get(c) for c in cols) for row in rows]
    # Multi-row INSERT batches avoid executemany()'s implicit pipeline (brittle
    # through a CONNECT tunnel) and avoid one RTT per row.
    batch_size = 100
    for i in range(0, len(values), batch_size):
        chunk = values[i : i + batch_size]
        values_sql = ", ".join([placeholders] * len(chunk))
        sql = f"INSERT INTO {table} ({col_sql}) VALUES {values_sql} {conflict}"
        flat: list[Any] = []
        for rowv in chunk:
            flat.extend(rowv)
        cur.execute(sql, flat)
    return len(values)


def conn_alive(conn: Optional[psycopg.Connection]) -> bool:
    try:
        if conn is None or conn.closed:
            return False
        # Avoid an extra round-trip that can leave a txn-pooler session in a
        # weird state; "closed" is enough, and write errors trigger reconnect.
        return True
    except Exception:
        return False


def safe_rollback(conn: Optional[psycopg.Connection]) -> None:
    try:
        if conn is not None and not conn.closed:
            conn.rollback()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class Extractor:
    def __init__(
        self,
        client: H4CClient,
        pause: float,
        club_id: str,
        refresh: bool = False,
        stage_path: Optional[Path] = None,
    ):
        self.conn: Optional[psycopg.Connection] = None
        self.client = client
        self.pause = pause
        self.club_id = club_id
        self.refresh = refresh
        self.stage = Stage(path=stage_path) if stage_path is not None else Stage()
        self.counts: dict[str, int] = {}
        self.errors: list[str] = []
        self.known_plan_ids: set[str] = set()
        self.known_club_ids: set[str] = set()
        self.known_slot_ids: set[str] = set()
        self.slots: list[dict[str, Any]] = []
        self.fitboxer_ids_seen: set[str] = set()
        self.detailed_fitboxers: set[str] = set()

    def close_pg(self) -> None:
        """Drop any open Postgres session. Never hold a DB socket during portal I/O."""
        conn = self.conn
        self.conn = None
        if conn is None:
            return
        safe_rollback(conn)
        try:
            if not conn.closed:
                conn.close()
        except Exception:
            pass

    def ensure_conn(self) -> None:
        if conn_alive(self.conn):
            return
        self.close_pg()
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                self.conn = connect_postgres()
                return
            except Exception as exc:
                last = exc
                log.warning("Postgres connect attempt %s/4 failed: %s", attempt + 1, exc)
                time.sleep(min(1.2 * (attempt + 1), 4))
        raise RuntimeError(f"Could not connect to Postgres: {last}")

    def db_cursor(self) -> psycopg.Cursor:
        self.ensure_conn()
        assert self.conn is not None
        return self.conn.cursor()

    def add_count(self, entity: str, n: int) -> None:
        if n:
            self.counts[entity] = self.counts.get(entity, 0) + n

    def note_error(self, msg: str) -> None:
        log.warning("%s", msg)
        self.errors.append(msg[:500])

    def _pause(self) -> None:
        if self.pause > 0:
            client_sleep(self.pause)

    def portal(self, fn, what: str, retries: int = 4) -> Any:
        # A long-lived pooler socket dies while we wait on the portal.
        self.close_pg()
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                data = fn()
                self._pause()
                return data
            except H4CError as exc:
                last = exc
                msg = str(exc)
                transient = (
                    exc.status in (401, 408, 429, 500, 502, 503, 504)
                    or "unauthoris" in msg.lower()
                    or "unauthorized" in msg.lower()
                    or "timed out" in msg.lower()
                    or "temporarily" in msg.lower()
                )
                if attempt < retries - 1 and transient:
                    delay = (2 ** attempt) + random.uniform(0, 0.5)
                    log.warning("Retry %s after %s (sleep %.1fs): %s", what, exc.status or "err", delay, msg[:200])
                    time.sleep(delay)
                    try:
                        self.client.login()
                    except H4CError:
                        pass
                    continue
                raise
            except Exception as exc:
                last = exc
                if attempt < retries - 1:
                    delay = (2 ** attempt) + random.uniform(0, 0.5)
                    log.warning("Retry %s after %s (sleep %.1fs)", what, type(exc).__name__, delay)
                    time.sleep(delay)
                    continue
                raise
        raise last or RuntimeError(what)

    def gql(self, query: str, variables: Optional[dict] = None, what: str = "graphql") -> Any:
        return self.portal(lambda: self.client.graphql(query, variables), what)

    def staged_gql(self, entity: str, key: str, query: str, variables: Optional[dict] = None, what: str = "graphql") -> Any:
        if not self.refresh and self.stage.exists(entity, key):
            log.info("Stage hit %s %s", entity, key)
            return self.stage.get(entity, key)
        data = self.gql(query, variables, what=what)
        self.stage.put(entity, key, data)
        return data

    def staged_call(self, entity: str, key: str, fetcher, what: str = "portal") -> Any:
        if not self.refresh and self.stage.exists(entity, key):
            log.info("Stage hit %s %s", entity, key)
            return self.stage.get(entity, key)
        data = self.portal(fetcher, what=what)
        self.stage.put(entity, key, data)
        return data

    def commit(self) -> None:
        """Commit the current batch and close the connection (short-lived sessions)."""
        conn = self.conn
        if conn is None or conn.closed:
            self.conn = None
            return
        try:
            conn.commit()
        except Exception:
            log.warning("Commit failed; closing socket")
            safe_rollback(conn)
        try:
            if not conn.closed:
                conn.close()
        except Exception:
            pass
        self.conn = None

    def write_rows(self, table: str, rows: list[dict[str, Any]], pk: Iterable[str], mode: str = "replace") -> int:
        """Idempotent upsert on a fresh short-lived connection, with quick retries."""
        if not rows:
            return 0
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                self.close_pg()
                cur = self.db_cursor()
                n = upsert(cur, table, rows, pk, mode)
                self.commit()
                return n
            except (psycopg.OperationalError, psycopg.InterfaceError, OSError) as exc:
                last = exc
                log.warning("Write %s attempt %s/4 failed: %s", table, attempt + 1, exc)
                self.close_pg()
                time.sleep(min(1.2 * (attempt + 1), 4))
            except Exception:
                self.close_pg()
                raise
        raise RuntimeError(f"Write {table} failed: {last}")

    def write_group(self, ops: list[tuple]) -> None:
        """Write several upserts on one short-lived connection, with quick retries.

        Each op is (count_key or None, table, rows, pk, optional mode).
        Used so a week's class details or a page of member details pay one
        handshake, not one per table or per row.
        """
        if not any(op[2] for op in ops):
            return
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                self.close_pg()
                cur = self.db_cursor()
                for op in ops:
                    count_key, table, rows, pk = op[0], op[1], op[2], op[3]
                    mode = op[4] if len(op) > 4 else "replace"
                    if not rows:
                        continue
                    n = upsert(cur, table, rows, pk, mode)
                    if count_key:
                        self.add_count(count_key, n)
                self.commit()
                return
            except (psycopg.OperationalError, psycopg.InterfaceError, OSError) as exc:
                last = exc
                log.warning("Write group attempt %s/4 failed: %s", attempt + 1, exc)
                self.close_pg()
                time.sleep(min(1.2 * (attempt + 1), 4))
            except Exception:
                self.close_pg()
                raise
        raise RuntimeError(f"Write group failed: {last}")

    # -- club / catalogue --------------------------------------------------

    def load_club(self) -> None:
        data = self.staged_gql("club", "club", Q_CLUB, what="club") or {}
        club = data.get("club") or {}
        if not club:
            raise RuntimeError("club query returned no data")
        cid = as_text(club.get("id")) or self.club_id
        self.club_id = cid
        cur = self.db_cursor()
        n = self.write_rows(
            "h4c.clubs",
            [
                {
                    "id": cid,
                    "alias": as_text(club.get("alias")),
                    "name": as_text(club.get("alias")),
                    "country": as_text(club.get("country")),
                    "email": as_text(club.get("email")),
                    "telephone": as_text(club.get("telephone")),
                    "currency": as_text(club.get("currency")),
                    "symbol": as_text(club.get("symbol")),
                    "address": as_text(club.get("address")),
                    "address2": as_text(club.get("address2")),
                    "zip_code": as_text(club.get("zipCode")),
                    "city": as_text(club.get("city")),
                    "parking": as_bool(club.get("parking")),
                    "latitude": as_num(club.get("latitude")),
                    "longitude": as_num(club.get("longitude")),
                    "map_center_latitude": as_num(club.get("mapCenterLatitude")),
                    "map_center_longitude": as_num(club.get("mapCenterLongitude")),
                    "zoom_map": as_int(club.get("zoomMap")),
                    "facebook_uid": as_text(club.get("facebookUid")),
                    "instagram_uid": as_text(club.get("instagramUid")),
                    "avatar": as_text(club.get("avatar")),
                    "timezone": "Europe/Madrid",
                    "raw": raw_of(club),
                    "extracted_at": datetime.now(UTC),
                }
            ],
            ["id"],
        )
        self.add_count("clubs", n)

        staff_rows = []
        perm_rows = []
        for person in club.get("staff") or []:
            if not isinstance(person, dict):
                continue
            sid = as_text(person.get("id"))
            if not sid:
                continue
            staff_rows.append(
                {
                    "id": sid,
                    "club_id": cid,
                    "fitboxer_id": as_text(person.get("fitboxerId")),
                    "trainer_id": as_text(person.get("trainerId")),
                    "name": as_text(person.get("name")),
                    "email": as_text(person.get("email")),
                    "avatar": as_text(person.get("avatar")),
                    "main_role": as_text(person.get("mainRole")),
                    "is_locked": as_bool(person.get("isLocked")),
                    "source": as_text(person.get("source")),
                    "last_login": parse_dt(person.get("lastLogin")),
                    "last_verify": parse_dt(person.get("lastVerify")),
                    "created_at": parse_dt(person.get("createdAt")),
                    "raw": raw_of(person),
                    "extracted_at": datetime.now(UTC),
                }
            )
            for perm in person.get("permissions") or []:
                if not isinstance(perm, dict):
                    continue
                pid = as_text(perm.get("id"))
                uid = as_text(perm.get("userId")) or sid
                if not pid:
                    continue
                perm_rows.append(
                    {
                        "id": pid,
                        "user_id": uid,
                        "club_id": as_text(perm.get("clubId")) or cid,
                        "role": as_text(perm.get("role")) or "",
                        "raw": raw_of(perm),
                        "extracted_at": datetime.now(UTC),
                    }
                )
        if staff_rows:
            self.add_count("staff", self.write_rows("h4c.staff", staff_rows, ["id"]))
        if perm_rows:
            self.add_count("staff_permissions", self.write_rows("h4c.staff_permissions", perm_rows, ["id"]))

        plan_rows = []
        for plan in club.get("plans") or []:
            if not isinstance(plan, dict):
                continue
            pid = as_text(plan.get("id"))
            if not pid:
                continue
            self.known_plan_ids.add(pid)
            plan_rows.append(
                {
                    "id": pid,
                    "club_id": cid,
                    "name": as_text(plan.get("name")),
                    "short_name": as_text(plan.get("shortName")),
                    "price_eur": as_num(plan.get("price")),
                    "plan_type": as_text(plan.get("type")),
                    "commitment_mode": as_text(plan.get("commitmentMode")),
                    "cash": as_bool(plan.get("cash")),
                    "book_included": as_bool(plan.get("bookIncluded")),
                    "book_amount_eur": as_num(plan.get("bookAmount")),
                    "service_amount_eur": as_num(plan.get("serviceAmount")),
                    "enrollment_fee_eur": as_num(plan.get("enrollmentFee")),
                    "cancellation_fee_eur": as_num(plan.get("cancellationFee")),
                    "display_mode": as_text(plan.get("displayMode")),
                    "offer_configs": as_json(plan.get("offerConfigs")),
                    "config": as_json(plan.get("config")),
                    "is_removed": as_bool(plan.get("isRemoved")),
                    "raw": raw_of(plan),
                    "extracted_at": datetime.now(UTC),
                }
            )
        if plan_rows:
            self.add_count("plans", self.write_rows("h4c.plans", plan_rows, ["id"]))

        coin_rows = []
        for pack in club.get("fitcoins") or []:
            if not isinstance(pack, dict):
                continue
            units = as_int(pack.get("id"))
            if units is None:
                continue
            coin_rows.append(
                {
                    "club_id": cid,
                    "units": units,
                    "price_eur": as_num(pack.get("price")),
                    "raw": raw_of(pack),
                    "extracted_at": datetime.now(UTC),
                }
            )
        if coin_rows:
            self.add_count("fitcoin_products", self.write_rows("h4c.fitcoin_products", coin_rows, ["club_id", "units"]))
        self.commit()
        if cid:
            self.known_club_ids.add(cid)
        log.info("Loaded club catalogue club=%s plans=%s staff=%s", cid, len(plan_rows), len(staff_rows))

    def _ensure_plan(self, plan_id: Optional[str], plan_name: Optional[str] = None) -> Optional[str]:
        if not plan_id:
            return None
        if plan_id in self.known_plan_ids:
            return plan_id
        self.write_rows(
            "h4c.plans",
            [
                {
                    "id": plan_id,
                    "club_id": self.club_id,
                    "name": plan_name,
                    "raw": Jsonb({"stub": True, "name": plan_name}),
                    "extracted_at": datetime.now(UTC),
                }
            ],
            ["id"],
            mode="fill",
        )
        self.known_plan_ids.add(plan_id)
        self.add_count("plans", 1)
        return plan_id

    # -- schedule ----------------------------------------------------------

    def load_schedule(self) -> None:
        data = self.staged_gql("schedule", "schedule", Q_SCHEDULE, {"addRemoved": True}, what="schedule") or {}
        rows = []
        self.slots = []
        for slot in data.get("schedule") or []:
            if not isinstance(slot, dict):
                continue
            sid = as_text(slot.get("id"))
            if not sid:
                continue
            rec = {
                "id": sid,
                "club_id": as_text(slot.get("clubId")) or self.club_id,
                "name": as_text(slot.get("name")),
                "day_of_week": as_int(slot.get("day")),
                "hour": as_text(slot.get("hour")),
                "room": as_text(slot.get("room")),
                "capacity": as_int(slot.get("capacity")),
                "aggregator_limit": as_int(slot.get("aggregatorLimit")),
                "is_removed": as_bool(slot.get("isRemoved")),
                "raw": raw_of(slot),
                "extracted_at": datetime.now(UTC),
            }
            rows.append(rec)
            self.slots.append(rec)
            self.known_slot_ids.add(sid)
        cur = self.db_cursor()
        self.add_count("schedule_slots", self.write_rows("h4c.schedule_slots", rows, ["id"]))
        self.commit()
        log.info("Loaded schedule slots=%s", len(rows))

    def _match_slot(self, starts_at: Optional[datetime]) -> Optional[str]:
        if not starts_at or not self.slots:
            return None
        local = starts_at.astimezone(MADRID)
        day = local.isoweekday()  # Mon=1 ... Sun=7
        hour = local.strftime("%H:%M")
        matches = [
            s
            for s in self.slots
            if not s.get("is_removed")
            and s.get("day_of_week") == day
            and s.get("hour") == hour
        ]
        if len(matches) == 1:
            return matches[0]["id"]
        return None

    # -- classes -----------------------------------------------------------

    def _class_row(self, item: dict, is_finished: Optional[bool] = None, extra: Optional[dict] = None) -> dict[str, Any]:
        src = dict(item)
        if extra:
            src.update({k: v for k, v in extra.items() if v is not None})
        starts = parse_dt(src.get("tim"))
        tra = src.get("tra") if isinstance(src.get("tra"), dict) else {}
        slot_id = self._match_slot(starts)
        finished_flag = is_finished
        if finished_flag is None:
            finished_flag = parse_dt(src.get("tim_fin")) is not None and parse_dt(src.get("tim_fin")) <= datetime.now(UTC)
        return {
            "id": as_text(src.get("id")),
            "club_id": self.club_id,
            "schedule_slot_id": slot_id,
            "class_type": as_text(src.get("type")),
            "starts_at": starts,
            "attendance_open_at": parse_dt(src.get("tim_att")),
            "finished_at": parse_dt(src.get("tim_fin")),
            "is_finished": bool(finished_flag),
            "is_cancelled": as_bool(src.get("off")),
            "is_private": as_bool(src.get("private")),
            "displayed": as_bool(src.get("dsp")) if not isinstance(src.get("dsp"), (int, float)) else None,
            "capacity": as_int(src.get("capacity")),
            "reserved_count": as_int(src.get("rsv_val")),
            "first_booking_count": as_int(src.get("fb_val")),
            "aggregator_count": as_int(src.get("aggregatorsCount")),
            "aggregator_limit": as_int(src.get("aggregatorLimit")),
            "trainer_id": as_text(tra.get("id")),
            "trainer_name": as_text(tra.get("name")),
            "challenge": as_text(src.get("challenge")),
            "end_class_id": as_text(src.get("endClassId")),
            "wrk_avg": as_num(src.get("wrk")),
            "bags": as_json(src.get("bags")),
            "raw": raw_of(item if not extra else src),
            "extracted_at": datetime.now(UTC),
        }

    def load_classes_window(self, start: date, end: date, upcoming: bool, finished: bool, details: bool) -> set[str]:
        ids: set[str] = set()
        if finished:
            data = self.staged_gql(
                "classes_finished",
                f"{start}:{end}",
                Q_CLASSES_FINISHED,
                {"from": ymd_slash(start), "to": ymd_slash(end)},
                what=f"classes_finished {start}..{end}",
            ) or {}
            rows = []
            for item in data.get("classes") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                rows.append(self._class_row(item, is_finished=True))
                ids.add(str(item["id"]))
            if rows:
                self.add_count("classes", self.write_rows("h4c.classes", rows, ["id"]))
                self.commit()
            log.info("Finished classes %s..%s: %s", start, end, len(rows))
            if details:
                self._load_finished_details(sorted(ids))
        if upcoming:
            data = self.staged_gql(
                "classes_upcoming",
                f"{start}:{end}",
                Q_CLASSES_UPCOMING,
                {"from": ymd_slash(start), "to": ymd_slash(end)},
                what=f"classes_upcoming {start}..{end}",
            ) or {}
            rows = []
            upcoming_ids = []
            for item in data.get("classes") or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                rows.append(self._class_row(item, is_finished=False))
                upcoming_ids.append(str(item["id"]))
                ids.add(str(item["id"]))
            if rows:
                self.add_count("classes", self.write_rows("h4c.classes", rows, ["id"]))
                self.commit()
            log.info("Upcoming classes %s..%s: %s", start, end, len(rows))
            if details:
                self._load_upcoming_details(upcoming_ids)
        return ids

    def _load_finished_details(self, class_ids: list[str]) -> None:
        # Phase 1: fetch every class into SQLite staging (no DB socket held).
        payloads: list[tuple[str, dict]] = []
        for i, cid in enumerate(class_ids, start=1):
            try:
                data = self.staged_gql("class_detail", cid, Q_CLASS, {"id": cid}, what=f"class {cid}") or {}
                detail = data.get("class") or {}
                if not detail:
                    self.note_error(f"empty class detail {cid}")
                    continue
                payloads.append((cid, detail))
            except Exception as exc:
                self.note_error(f"class detail fetch {cid}: {exc}")
            if i % 25 == 0 or i == len(class_ids):
                log.info("Fetched finished class details %s/%s", i, len(class_ids))
        # Phase 2: flush the whole week on one connection.
        class_rows: list[dict[str, Any]] = []
        participants: list[dict[str, Any]] = []
        workouts: list[dict[str, Any]] = []
        for cid, detail in payloads:
            class_rows.append(self._class_row(detail, is_finished=True))
            for wo in detail.get("workouts") or []:
                if not isinstance(wo, dict):
                    continue
                fb = wo.get("fitboxer") if isinstance(wo.get("fitboxer"), dict) else {}
                fid = as_text(fb.get("fitboxerId")) or as_text(fb.get("id")) or as_text(wo.get("fitboxerId"))
                if not fid:
                    continue
                self.fitboxer_ids_seen.add(fid)
                wid = as_text(wo.get("id"))
                participants.append(
                    {
                        "class_id": cid,
                        "fitboxer_id": fid,
                        "booking_id": None,
                        "list": "booking",
                        "attendance": "attended",
                        "checked_in": True,
                        "is_guest": None,
                        "owner_id": None,
                        "guest_ids": None,
                        "aggregator_id": as_text(fb.get("aggregatorId")),
                        "aggregator_name": as_text(fb.get("aggregatorName")),
                        "bag_number": as_int(wo.get("n_saco")),
                        "workout_id": wid,
                        "wrk": as_num(wo.get("wrk")),
                        "syn": as_num(wo.get("syn")),
                        "pow": as_num(wo.get("pow")),
                        "total_karma": as_num(wo.get("total_karma")),
                        "h4c_house": as_int(wo.get("h4cHouse")),
                        "donated_energy": as_num(wo.get("donatedEnergy")),
                        "workout_type": as_text(wo.get("type")),
                        "status_at": None,
                        "raw": raw_of(wo),
                        "extracted_at": datetime.now(UTC),
                    }
                )
                if wid:
                    workouts.append(
                        {
                            "id": wid,
                            "club_id": self.club_id,
                            "class_id": cid,
                            "fitboxer_id": fid,
                            "started_at": parse_dt(detail.get("tim")),
                            "wrk": as_num(wo.get("wrk")),
                            "syn": as_num(wo.get("syn")),
                            "pow": as_num(wo.get("pow")),
                            "bag_number": as_int(wo.get("n_saco")),
                            "rounds_ok": None,
                            "client_numeric_id": None,
                            "rounds": None,
                            "raw": raw_of(wo),
                            "extracted_at": datetime.now(UTC),
                        }
                    )
            for ns in detail.get("no_show") or []:
                if not isinstance(ns, dict):
                    continue
                fid = as_text(ns.get("fitboxerId")) or as_text(ns.get("id"))
                if not fid:
                    continue
                self.fitboxer_ids_seen.add(fid)
                participants.append(
                    {
                        "class_id": cid,
                        "fitboxer_id": fid,
                        "booking_id": as_text(ns.get("id")) if as_text(ns.get("id")) != fid else None,
                        "list": "booking",
                        "attendance": "no_show",
                        "checked_in": False,
                        "is_guest": None,
                        "owner_id": None,
                        "guest_ids": None,
                        "aggregator_id": as_text(ns.get("aggregatorId")),
                        "aggregator_name": as_text(ns.get("aggregatorName")),
                        "bag_number": None,
                        "workout_id": None,
                        "wrk": None,
                        "syn": None,
                        "pow": None,
                        "total_karma": None,
                        "h4c_house": None,
                        "donated_energy": None,
                        "workout_type": None,
                        "status_at": None,
                        "raw": raw_of(ns),
                        "extracted_at": datetime.now(UTC),
                    }
                )
        self.write_group(
            [
                ("classes", "h4c.classes", class_rows, ["id"]),
                ("class_participants", "h4c.class_participants", participants, ["class_id", "fitboxer_id"]),
                ("workouts", "h4c.workouts", workouts, ["id"], "fill"),
            ]
        )
        log.info(
            "Flushed finished class details classes=%s participants=%s workouts=%s",
            len(class_rows),
            len(participants),
            len(workouts),
        )

    def _load_upcoming_details(self, class_ids: list[str]) -> None:
        payloads: list[tuple[str, dict]] = []
        for i, cid in enumerate(class_ids, start=1):
            try:
                data = self.staged_gql("next_class", cid, Q_NEXT_CLASS, {"id": cid}, what=f"nextClass {cid}") or {}
                detail = data.get("nextClass") or {}
                if not detail:
                    self.note_error(f"empty nextClass {cid}")
                    continue
                payloads.append((cid, detail))
            except Exception as exc:
                self.note_error(f"nextClass fetch {cid}: {exc}")
            if i % 25 == 0 or i == len(class_ids):
                log.info("Fetched upcoming class details %s/%s", i, len(class_ids))
        class_rows: list[dict[str, Any]] = []
        participants: list[dict[str, Any]] = []
        for cid, detail in payloads:
            class_rows.append(self._class_row(detail, is_finished=False))
            for lst_name, people in (("booking", detail.get("bookings") or []), ("waiting", detail.get("waiting") or [])):
                for bk in people:
                    if not isinstance(bk, dict):
                        continue
                    fid = as_text(bk.get("fitboxerId")) or as_text(bk.get("id"))
                    if not fid:
                        continue
                    self.fitboxer_ids_seen.add(fid)
                    checked = as_bool(bk.get("isBookingCheckIn"))
                    attendance = "checked_in" if checked else "reserved"
                    participants.append(
                        {
                            "class_id": cid,
                            "fitboxer_id": fid,
                            "booking_id": as_text(bk.get("id")),
                            "list": lst_name,
                            "attendance": attendance,
                            "checked_in": checked,
                            "is_guest": as_bool(bk.get("isGuest")),
                            "owner_id": as_text(bk.get("ownerId")),
                            "guest_ids": as_json(bk.get("guestIds")),
                            "aggregator_id": as_text(bk.get("aggregatorId")),
                            "aggregator_name": as_text(bk.get("aggregatorName")),
                            "bag_number": None,
                            "workout_id": None,
                            "wrk": None,
                            "syn": None,
                            "pow": None,
                            "total_karma": None,
                            "h4c_house": None,
                            "donated_energy": None,
                            "workout_type": None,
                            "status_at": parse_dt(bk.get("statusTime")),
                            "raw": raw_of(bk),
                            "extracted_at": datetime.now(UTC),
                        }
                    )
        self.write_group(
            [
                ("classes", "h4c.classes", class_rows, ["id"]),
                ("class_participants", "h4c.class_participants", participants, ["class_id", "fitboxer_id"]),
            ]
        )
        log.info("Flushed upcoming class details classes=%s participants=%s", len(class_rows), len(participants))

    def load_sessions(self, start: date, end: date) -> None:
        data = self.staged_gql(
            "sessions",
            f"{start}:{end}",
            Q_SESSIONS,
            {"from": ymd_dash(start), "to": ymd_dash(end)},
            what=f"sessions {start}..{end}",
        ) or {}
        rows = []
        for ses in data.get("sessions") or []:
            if not isinstance(ses, dict) or not ses.get("id"):
                continue
            rows.append(
                {
                    "id": as_text(ses.get("id")),
                    "club_id": self.club_id,
                    "class_id": None,
                    "fitboxer_id": None,
                    "started_at": parse_dt(ses.get("tim")),
                    "wrk": as_num(ses.get("wrk")),
                    "syn": as_num(ses.get("syn")),
                    "pow": as_num(ses.get("pow")),
                    "bag_number": as_int(ses.get("n_saco")),
                    "rounds_ok": as_int(ses.get("rounds_ok")),
                    "client_numeric_id": as_int(ses.get("clt")),
                    "rounds": as_json(ses.get("rounds")),
                    "raw": raw_of(ses),
                    "extracted_at": datetime.now(UTC),
                }
            )
        if rows:
            cur = self.db_cursor()
            self.add_count("workouts", self.write_rows("h4c.workouts", rows, ["id"], mode="fill"))
            self.commit()
        log.info("Sessions %s..%s: %s", start, end, len(rows))

    # -- shop / memberships / status --------------------------------------

    def load_orders(self, start: date, end: date) -> None:
        data = self.staged_gql(
            "orders",
            f"{start}:{end}",
            Q_ORDERS,
            {"from": ymd_dash(start), "to": ymd_dash(end)},
            what=f"orders {start}..{end}",
        ) or {}
        rows = []
        for od in data.get("orders") or []:
            if not isinstance(od, dict) or not od.get("id"):
                continue
            fid = as_text(od.get("fitboxerId"))
            if fid:
                self.fitboxer_ids_seen.add(fid)
            rows.append(
                {
                    "id": as_text(od.get("id")),
                    "club_id": self.club_id,
                    "fitboxer_id": fid,
                    "product_type": as_text(od.get("productType")),
                    "product_id": as_text(od.get("productId")),
                    "description": as_text(od.get("description")),
                    "ordered_at": parse_dt(od.get("date")),
                    "amount_cents": as_int(od.get("amount")),
                    "discount_cents": as_int(od.get("discount")),
                    "payment_method": as_text(od.get("paymentMethod")),
                    "payment_reference": as_text(od.get("paymentReference")),
                    "status": as_text(od.get("status")),
                    "status_at": parse_dt(od.get("statusTime")),
                    "voucher_type": as_text(od.get("voucherType")),
                    "metadata": as_json(od.get("metadata")),
                    "raw": raw_of(od),
                    "extracted_at": datetime.now(UTC),
                }
            )
        if rows:
            cur = self.db_cursor()
            self.add_count("orders", self.write_rows("h4c.orders", rows, ["id"]))
            self.commit()
        log.info("Orders %s..%s: %s", start, end, len(rows))

    def load_unsubs(self, start: date, end: date) -> None:
        data = self.staged_gql(
            "unsubs",
            f"{start}:{end}",
            Q_UNSUBS,
            {"from": ymd_dash(start), "to": ymd_dash(end)},
            what=f"unsubs {start}..{end}",
        ) or {}
        rows = []
        cur = self.db_cursor()
        for un in data.get("unsubs") or []:
            if not isinstance(un, dict) or not un.get("id"):
                continue
            fid = as_text(un.get("fitboxerId"))
            if fid:
                self.fitboxer_ids_seen.add(fid)
            plan_id = self._ensure_plan(as_text(un.get("planId")), as_text(un.get("name")))
            rows.append(
                {
                    "id": as_text(un.get("id")),
                    "club_id": self.club_id,
                    "fitboxer_id": fid,
                    "plan_id": plan_id,
                    "plan_name": as_text(un.get("name")),
                    "amount_cents": as_int(un.get("amount")),
                    "requested_at": parse_dt(un.get("offRequestTime")),
                    "effective_at": parse_dt(un.get("offTime")),
                    "off_reason": as_text(un.get("offReason")),
                    "off_reason_text": as_text(un.get("offReasonText")),
                    "rating": as_num(un.get("valoration")),
                    "downgrade": as_bool(un.get("downgrade")),
                    "last_level": as_int(un.get("lastLevel")),
                    "raw": raw_of(un),
                    "extracted_at": datetime.now(UTC),
                }
            )
        if rows:
            self.add_count("unsubscriptions", self.write_rows("h4c.unsubscriptions", rows, ["id"]))
            self.commit()
        log.info("Unsubs %s..%s: %s", start, end, len(rows))

    def load_status(self, start: date, end: date, types: Iterable[str] = STATUS_TYPES) -> None:
        total = 0
        for typ in types:
            data = self.staged_gql(
                "status",
                f"{typ}:{start}:{end}",
                Q_STATUS,
                {"type": typ, "from": ymd_dash(start), "to": ymd_dash(end)},
                what=f"fitboxerStatus {typ} {start}..{end}",
            ) or {}
            rows = []
            for ev in data.get("fitboxerStatus") or []:
                if not isinstance(ev, dict) or not ev.get("id"):
                    continue
                fid = as_text(ev.get("fitboxerId"))
                fb = ev.get("fitboxer") if isinstance(ev.get("fitboxer"), dict) else {}
                if not fid:
                    fid = as_text(fb.get("fitboxerId")) or as_text(fb.get("id"))
                if fid:
                    self.fitboxer_ids_seen.add(fid)
                payment = ev.get("payment") if isinstance(ev.get("payment"), dict) else {}
                order = ev.get("order") if isinstance(ev.get("order"), dict) else {}
                sub = ev.get("subscription") if isinstance(ev.get("subscription"), dict) else {}
                trainer = ev.get("trainer") if isinstance(ev.get("trainer"), dict) else {}
                meta = ev.get("metadata") if isinstance(ev.get("metadata"), dict) else {}
                rows.append(
                    {
                        "id": as_text(ev.get("id")),
                        "club_id": self.club_id,
                        "fitboxer_id": fid,
                        "status_code": as_text(ev.get("status")),
                        "prev_status_code": as_text(ev.get("prevStatus")),
                        "prev_status_at": parse_dt(ev.get("prevTime")),
                        "occurred_at": parse_dt(ev.get("time")),
                        "when_at": parse_dt(ev.get("when")),
                        "elapsed": as_int(ev.get("elapsed")),
                        "source": as_text((meta or {}).get("source")) if isinstance(meta, dict) else None,
                        "query_type": typ,
                        "order_id": as_text(order.get("id")) or as_text(meta.get("orderId") if isinstance(meta, dict) else None),
                        "membership_id": as_text(meta.get("membershipId") if isinstance(meta, dict) else None),
                        "payment_id": as_text(payment.get("id")),
                        "subscription_id": as_text(sub.get("id")),
                        "trainer_id": as_text(trainer.get("id")),
                        "metadata": as_json(ev.get("metadata")),
                        "raw": raw_of(ev),
                        "extracted_at": datetime.now(UTC),
                    }
                )
            if rows:
                n = self.write_rows("h4c.fitboxer_status_events", rows, ["id"])
                self.add_count("fitboxer_status_events", n)
                total += n
                self.commit()
        log.info("Status events %s..%s upserts=%s", start, end, total)

    def load_memberships(self) -> None:
        data = self.staged_gql("memberships", "memberships", Q_MEMBERSHIPS, what="memberships") or {}
        cur = self.db_cursor()
        rows = []
        charge_rows = []
        for mb in data.get("memberships") or []:
            if not isinstance(mb, dict) or not mb.get("id"):
                continue
            mid = as_text(mb.get("id"))
            fid = as_text(mb.get("fitboxerId"))
            if fid:
                self.fitboxer_ids_seen.add(fid)
            plan_id = self._ensure_plan(as_text(mb.get("planId")), as_text(mb.get("name")))
            rows.append(
                {
                    "id": mid,
                    "club_id": self.club_id,
                    "fitboxer_id": fid,
                    "plan_id": plan_id,
                    "plan_name": as_text(mb.get("name")),
                    "started_at": parse_dt(mb.get("initTime")),
                    "amount_cents": as_int(mb.get("amount")),
                    "fee_cents": as_int(mb.get("fee")),
                    "discount_next_charge_cents": as_int(mb.get("discountNextCharge")),
                    "pay_day": as_int(mb.get("payDay")),
                    "pay_hour": as_text(mb.get("payHour")),
                    "billing_period": as_text(mb.get("billingPeriod")),
                    "voucher_type": as_text(mb.get("voucherType")),
                    "unpaidments": as_int(mb.get("unpaidments")),
                    "next_charge_at": parse_dt(mb.get("nextCharge")),
                    "payment_method": None,
                    "is_off": False,
                    "raw": raw_of(mb),
                    "extracted_at": datetime.now(UTC),
                }
            )
            for ch in mb.get("recurringSchedule") or []:
                if not isinstance(ch, dict):
                    continue
                cdate = parse_date(ch.get("date"))
                if not cdate:
                    continue
                charge_rows.append(
                    {
                        "membership_id": mid,
                        "charge_date": cdate,
                        "order_id": as_text(ch.get("orderId")),
                        "status": as_text(ch.get("status")),
                        "status_at": parse_dt(ch.get("statusTime")),
                        "raw": raw_of(ch),
                        "extracted_at": datetime.now(UTC),
                    }
                )
        if rows:
            self.add_count("memberships", self.write_rows("h4c.memberships", rows, ["id"]))
        if charge_rows:
            self.add_count(
                "membership_charges",
                self.write_rows("h4c.membership_charges", charge_rows, ["membership_id", "charge_date"]),
            )
        self.commit()
        log.info("Memberships=%s charges=%s", len(rows), len(charge_rows))

    def load_leads(self) -> None:
        data = self.staged_gql("leads", "leads", Q_LEADS, what="leads") or {}
        rows = []
        for ld in data.get("leads") or []:
            if not isinstance(ld, dict) or not ld.get("id"):
                continue
            rows.append(
                {
                    "id": as_text(ld.get("id")),
                    "club_id": self.club_id,
                    "first_name": as_text(ld.get("FirstName")),
                    "last_name": as_text(ld.get("LastName")),
                    "email": as_text(ld.get("Email")),
                    "phone": as_text(ld.get("Phone")),
                    "status": as_text(ld.get("Status")),
                    "lead_source": as_text(ld.get("LeadSource")),
                    "input_channel": as_text(ld.get("InputChannel__c")),
                    "objections": as_text(ld.get("Objections__c")),
                    "other_source": as_text(ld.get("Other_source__c")),
                    "payment_refusal_reason": as_text(ld.get("PaymentRefusalReason__c")),
                    "created_at": parse_dt(ld.get("CreatedDate")),
                    "last_modified_at": parse_dt(ld.get("LastModifiedDate")),
                    "last_activity_at": parse_dt(ld.get("LastActivityDate")),
                    "raw": raw_of(ld),
                    "extracted_at": datetime.now(UTC),
                }
            )
        if rows:
            cur = self.db_cursor()
            self.add_count("leads", self.write_rows("h4c.leads", rows, ["id"]))
            self.commit()
        log.info("Leads=%s", len(rows))

    def load_aggregators(self) -> None:
        data = self.staged_gql("aggregators", "aggregators", Q_AGGREGATORS, what="aggregators") or {}
        rows = []
        for ag in data.get("fitboxersFromAggregator") or []:
            if not isinstance(ag, dict) or not ag.get("id"):
                continue
            fid = as_text(ag.get("fitboxerId"))
            if fid:
                self.fitboxer_ids_seen.add(fid)
            rows.append(
                {
                    "id": as_text(ag.get("id")),
                    "club_id": self.club_id,
                    "fitboxer_id": fid,
                    "aggregator_id": as_text(ag.get("aggregatorId")),
                    "aggregator_name": as_text(ag.get("aggregatorName")),
                    "name": as_text(ag.get("name")),
                    "email": as_text(ag.get("eml")),
                    "avatar": as_text(ag.get("show_img")),
                    "raw": raw_of(ag),
                    "extracted_at": datetime.now(UTC),
                }
            )
        if rows:
            cur = self.db_cursor()
            self.add_count("aggregator_fitboxers", self.write_rows("h4c.aggregator_fitboxers", rows, ["id"]))
            self.commit()
        log.info("Aggregator fitboxers=%s", len(rows))

    def load_free_trials(self) -> None:
        total = 0
        for typ in FREETRIAL_TYPES:
            data = self.staged_gql("freetrials", typ, Q_FREETRIALS, {"type": typ}, what=f"freetrials {typ}") or {}
            rows = []
            for ft in data.get("freetrials") or []:
                if not isinstance(ft, dict) or not ft.get("id"):
                    continue
                used_by = ft.get("usedBy")
                used_by_id = used_by if isinstance(used_by, (str, int)) else (
                    used_by.get("id") if isinstance(used_by, dict) else None
                )
                rows.append(
                    {
                        "id": as_text(ft.get("id")),
                        "club_id": self.club_id,
                        "code": as_text(ft.get("code")),
                        "trial_type": as_text(ft.get("type")) or typ,
                        "created_at": parse_dt(ft.get("created")),
                        "updated_at": parse_dt(ft.get("updated")),
                        "issuer_id": as_text(ft.get("emisorId")),
                        "issuer_name": as_text(ft.get("emisorName")),
                        "used": as_bool(ft.get("used")) if typ != "C" else True,
                        "used_by_id": as_text(used_by_id),
                        "used_by_name": as_text(ft.get("usedByName")),
                        "amount": as_int(ft.get("imp")),
                        "web_offer": as_json(ft.get("webOffer")),
                        "raw": raw_of(ft),
                        "extracted_at": datetime.now(UTC),
                    }
                )
            if rows:
                n = self.write_rows("h4c.free_trials", rows, ["id"])
                self.add_count("free_trials", n)
                total += n
                self.commit()
        log.info("Free trials upserts=%s", total)

    # -- dashboard ---------------------------------------------------------

    def load_dashboard(self, year: int, month: int) -> None:
        data = self.staged_gql("dashboard", f"{year}-{month:02d}", Q_DASHBOARD, {"month": month, "year": year}, what=f"dashboard {year}-{month:02d}") or {}
        dash = data.get("dashboard") or {}
        if dash:
            unpaid = dash.get("unpaid") if isinstance(dash.get("unpaid"), dict) else {}
            fac = dash.get("facRatio") if isinstance(dash.get("facRatio"), dict) else {}
            con = dash.get("conRatio") if isinstance(dash.get("conRatio"), dict) else {}
            baj = dash.get("bajRatio") if isinstance(dash.get("bajRatio"), dict) else {}
            row = {
                "club_id": self.club_id,
                "year": year,
                "month": month,
                "snapshot_at": datetime.now(UTC),
                "dashboard_id": as_text(dash.get("id")),
                "symbol": as_text(dash.get("symbol")),
                "billing_eur": as_num(dash.get("totFac")),
                "billing_today_eur": as_num(dash.get("totFacHoy")),
                "members": as_int(dash.get("soc")),
                "members_today": as_int(dash.get("socHoy")),
                "clients": as_int(dash.get("clt")),
                "clients_today": as_int(dash.get("cltHoy")),
                "fitboxers_kpi": as_int(dash.get("fit")),
                "offers": as_int(dash.get("offers")),
                "offers_today": as_int(dash.get("offersHoy")),
                "first_bookings": as_int(dash.get("fb")),
                "first_bookings_today": as_int(dash.get("fbHoy")),
                "new_members_24": as_int(dash.get("addSoc24")),
                "new_members_24_today": as_int(dash.get("addSoc24Hoy")),
                "new_members": as_int(dash.get("addSoc")),
                "new_members_today": as_int(dash.get("addSocHoy")),
                "new_clients": as_int(dash.get("addClt")),
                "new_clients_today": as_int(dash.get("addCltHoy")),
                "dropouts": as_int(dash.get("baj")),
                "dropouts_today": as_int(dash.get("bajHoy")),
                "workouts_no_show": as_int(dash.get("woNoShow")),
                "first_booking_no_show": as_int(dash.get("fbNoShow")),
                "debut_no_show": as_int(dash.get("debNoShow")),
                "conversion_rate": as_num(dash.get("conSoc")),
                "engagement": as_num(dash.get("engage")),
                "unpaid_count": as_int(unpaid.get("count")),
                "unpaid_amount_eur": as_num(unpaid.get("amount")),
                "unpaid_ratio": as_num(unpaid.get("ratio")),
                "sales_ratio_value": as_num(fac.get("value")),
                "sales_ratio_target": as_num(fac.get("target")),
                "conversion_ratio_value": as_num(con.get("value")),
                "conversion_ratio_target": as_num(con.get("target")),
                "dropout_ratio_value": as_num(baj.get("value")),
                "dropout_ratio_target": as_num(baj.get("target")),
                "anywhere_mode": as_int(dash.get("AWMode")),
                "half_mode": as_int(dash.get("HALFMode")),
                "pause_mode": as_int(dash.get("pauseMode")),
                "billing_by_type": as_json(dash.get("vtaMes")),
                "billing_history": as_json(dash.get("vtaAcu")),
                "new_members_by_product": as_json(dash.get("newMbmMes")),
                "sessions_gifts": as_json(dash.get("sessionsGifts")),
                "sessions_extension": as_json(dash.get("sessionsExtension")),
                "raw": raw_of(dash),
                "extracted_at": datetime.now(UTC),
            }
            self.add_count(
                "dashboard_snapshots",
                self.write_rows("h4c.dashboard_snapshots", [row], ["club_id", "year", "month"]),
            )
            self.commit()

        data = self.staged_gql(
            "dashboard_summary",
            f"{year}-{month:02d}",
            Q_DASHBOARD_SUMMARY,
            {"month": month, "year": year},
            what=f"dashboardSummary {year}-{month:02d}",
        ) or {}
        summary = data.get("dashboardSummary") or {}
        if summary:
            period_id = as_text(summary.get("id")) or f"{year}-{month:02d}"
            row = {
                "club_id": self.club_id,
                "period_id": period_id,
                "year": year,
                "month": month,
                "offers": as_int(summary.get("offers")),
                "offers_today": as_int(summary.get("offersHoy")),
                "workout_target": as_int(summary.get("woTarget")),
                "engagement": as_num(summary.get("engage")),
                "raw": raw_of(summary),
                "extracted_at": datetime.now(UTC),
            }
            self.add_count(
                "dashboard_summaries",
                self.write_rows("h4c.dashboard_summaries", [row], ["club_id", "period_id"]),
            )
            self.commit()
        log.info("Dashboard %s-%02d loaded", year, month)

    # -- fitboxers ---------------------------------------------------------

    def _list_row(self, item: dict) -> Optional[dict[str, Any]]:
        fid = as_text(item.get("fitboxerId"))
        if not fid:
            return None
        self.fitboxer_ids_seen.add(fid)
        profile = item.get("id")
        profile_id = as_text(profile) if not isinstance(profile, int) else str(profile)
        # Prefer 24-char ids as profile_id when present
        if isinstance(profile, str) and len(profile) >= 20:
            profile_id = profile
        elif fid and len(fid) >= 20 and (not profile_id or profile_id.isdigit()):
            profile_id = fid
        return {
            "fitboxer_id": fid,
            "profile_id": profile_id,
            "club_id": self.club_id,
            "aggregator_id": as_text(item.get("aggregatorId")),
            "aggregator_name": as_text(item.get("aggregatorName")),
            "name": as_text(item.get("name")),
            "nick": as_text(item.get("nick")),
            "email": as_text(item.get("eml")),
            "phone": as_text(item.get("tfn")),
            "dni": as_text(item.get("dni")),
            "avatar": as_text(item.get("show_img")),
            "status_code": as_text(item.get("clt_tip")),
            "stripe_id": as_text(item.get("stripe_id")),
            "comments_count": as_int(item.get("commentsCount")),
            "unpaid": as_bool(item.get("unpaid")),
            "special_offer": as_bool(item.get("spe_ofe")),
            "raw": raw_of(item),
            "extracted_at": datetime.now(UTC),
        }

    def load_fitboxers_pages(self) -> list[str]:
        ids: list[str] = []
        page = 1
        while True:
            payload = self.staged_call(
                "fitboxers_page",
                str(page),
                lambda p=page: self.client.fitboxers(
                    text="", type=None, page=p, inComments=False, betweenDays=False
                ),
                what=f"fitboxers page {page}",
            )
            if not isinstance(payload, dict):
                self.note_error(f"unexpected fitboxers page {page} payload")
                break
            rows_in = payload.get("list") or payload.get("fitboxers") or []
            mapped = []
            for item in rows_in:
                if not isinstance(item, dict):
                    continue
                row = self._list_row(item)
                if row:
                    mapped.append(row)
                    ids.append(row["fitboxer_id"])
            if mapped:
                self.add_count("fitboxers", self.write_rows("h4c.fitboxers", mapped, ["fitboxer_id"], mode="fill"))
                self.commit()
            total = payload.get("total")
            ended = payload.get("end")
            log.info(
                "Fitboxers page %s: +%s (total_api=%s end=%s)",
                page,
                len(mapped),
                total,
                ended,
            )
            if not rows_in:
                break
            if ended is True:
                break
            if isinstance(total, int) and len(ids) >= total:
                break
            if len(rows_in) < 20 and ended is not False:
                break
            page += 1
            if page > 500:
                self.note_error("fitboxers pagination safety stop at page 500")
                break
        log.info("Fitboxers list complete unique=%s pages=%s", len(set(ids)), page)
        return ids

    def _detail_profile_candidates(self, fitboxer_id: str) -> list[str]:
        cands = [fitboxer_id]
        cur = self.db_cursor()
        cur.execute("SELECT profile_id FROM h4c.fitboxers WHERE fitboxer_id=%s", (fitboxer_id,))
        row = cur.fetchone()
        if row and row[0] and str(row[0]) not in cands:
            cands.insert(0, str(row[0]))
        return cands

    def load_fitboxer_details(self, fitboxer_ids: Iterable[str]) -> None:
        todo = [fid for fid in dict.fromkeys(fitboxer_ids) if fid and fid not in self.detailed_fitboxers]
        for i, fid in enumerate(todo, start=1):
            if not is_mongo_object_id(fid):
                # Salesforce-style / other non-Mongo ids cannot be fetched as a
                # ficha. Skip as a warning so --recent can still succeed.
                log.warning(
                    "Skipping fitboxer detail for non-Mongo id (len=%s prefix=%s); not a valid MongoObjectId",
                    len(str(fid)),
                    str(fid)[:3],
                )
                continue
            detail = None
            last_err = None
            for cand in self._detail_profile_candidates(fid):
                if not is_mongo_object_id(cand):
                    continue
                try:
                    data = self.staged_gql("fitboxer_detail", str(cand), Q_FITBOXER, {"id": cand}, what=f"fitboxer {fid}") or {}
                    detail = data.get("fitboxer")
                    if detail:
                        break
                except H4CError as exc:
                    last_err = exc
                    continue
            if not detail:
                if _is_invalid_mongo_object_id_error(last_err):
                    log.warning("Skipping fitboxer detail %s: %s", fid, last_err)
                    continue
                self.note_error(f"fitboxer detail {fid}: {last_err or 'empty'}")
                continue
            try:
                self._store_fitboxer_detail(detail, fallback_id=fid)
                self.detailed_fitboxers.add(fid)
                real_id = as_text(detail.get("fitboxerId")) or fid
                self.detailed_fitboxers.add(real_id)
            except Exception as exc:
                safe_rollback(self.conn)
                self.ensure_conn()
                self.note_error(f"store fitboxer {fid}: {exc}")
            if i % 50 == 0 or i == len(todo):
                log.info("Fitboxer details %s/%s", i, len(todo))

    def ensure_club_stub(self, club_id: Optional[str]) -> None:
        """Insert a placeholder club so visiting fitboxers don't fail the FK."""
        if not club_id or club_id in self.known_club_ids:
            return
        self.write_rows(
            "h4c.clubs",
            [{"id": club_id, "raw": raw_of({}), "extracted_at": datetime.now(UTC)}],
            ["id"],
            mode="fill",
        )
        self.known_club_ids.add(club_id)

    def _store_fitboxer_detail(self, fb: dict, fallback_id: str) -> None:
        fid = as_text(fb.get("fitboxerId")) or fallback_id
        club = fb.get("club") if isinstance(fb.get("club"), dict) else {}
        club_id = as_text(club.get("id")) or self.club_id
        self.ensure_club_stub(club_id)
        row = {
            "fitboxer_id": fid,
            "profile_id": as_text(fb.get("id")) or fid,
            "club_id": club_id,
            "aggregator_id": as_text(fb.get("aggregatorId")),
            "aggregator_name": as_text(fb.get("aggregatorName")),
            "first_name": as_text(fb.get("firstName")),
            "last_name": as_text(fb.get("lastName")),
            "name": as_text(fb.get("name")),
            "nick": as_text(fb.get("nick")),
            "email": as_text(fb.get("eml")),
            "phone": as_text(fb.get("tfn")),
            "dni": as_text(fb.get("dni")),
            "birthdate": parse_date(fb.get("birthdate")),
            "gender": as_text(fb.get("gender")),
            "city": as_text(fb.get("city")),
            "zip_postal_code": as_text(fb.get("zipPostalCode")),
            "avatar": as_text(fb.get("show_img")),
            "status_code": as_text(fb.get("clt_tip")),
            "level": as_int(fb.get("level")),
            "main_goal": as_text(fb.get("mainGoal")),
            "h4c_house": as_int(fb.get("h4cHouse")),
            "is_anywhere_active": as_bool(fb.get("isAnywhereActive")),
            "last_session_at": parse_dt(fb.get("lastSessionDate")),
            "last_no_show_at": parse_dt(fb.get("lastNoShow")),
            "sessions_count": as_int(fb.get("sessionsCount")),
            "available_extensions": as_int(fb.get("availableExtensions")),
            "engage": as_num(fb.get("engage")),
            "engage_history": as_json(fb.get("engageHistory")),
            "new_engagement": as_num(fb.get("newEngagement")),
            "w_avg": as_num(fb.get("w_avg")),
            "syn": as_num(fb.get("syn")),
            "pow": as_num(fb.get("pow")),
            "salesforce_id": as_text(fb.get("salesForceId")),
            "raw": raw_of(fb),
            "extracted_at": datetime.now(UTC),
        }
        # packs / rewards collected below, flushed together

        packs = []
        for pk in fb.get("sessionsPacks") or []:
            if not isinstance(pk, dict) or not pk.get("id"):
                continue
            packs.append(
                {
                    "id": as_text(pk.get("id")),
                    "club_id": self.club_id,
                    "fitboxer_id": fid,
                    "name": as_text(pk.get("name")),
                    "pack_type": as_text(pk.get("type")),
                    "expires_at": parse_dt(pk.get("expireIn")),
                    "available": as_int(pk.get("available")),
                    "total": as_int(pk.get("total")),
                    "gp_available": as_int(pk.get("gpAvailable")),
                    "global_pass": as_int(pk.get("globalPass")),
                    "raw": raw_of(pk),
                    "extracted_at": datetime.now(UTC),
                }
            )
        if packs:
            pass

        reward_rows = []
        seen_r = set()
        for key in ("rewards", "benefits"):
            for rw in fb.get(key) or []:
                if not isinstance(rw, dict) or not rw.get("id"):
                    continue
                rid = as_text(rw.get("id"))
                if rid in seen_r:
                    continue
                seen_r.add(rid)
                reward_rows.append(
                    {
                        "id": rid,
                        "fitboxer_id": fid,
                        "code": as_text(rw.get("code")),
                        "reward_type": as_text(rw.get("type")),
                        "subtype": as_text(rw.get("subtype")),
                        "name": as_text(rw.get("name")),
                        "description": json.dumps(rw.get("description"), ensure_ascii=False)
                        if isinstance(rw.get("description"), (dict, list))
                        else as_text(rw.get("description")),
                        "achieved_at": parse_dt(rw.get("dateAchieved")),
                        "expires_at": parse_dt(rw.get("expiresAt")),
                        "source_type": as_text(rw.get("sourceType")),
                        "url": as_text(rw.get("url")),
                        "metadata": as_json(rw.get("metadata")),
                        "raw": raw_of(rw),
                        "extracted_at": datetime.now(UTC),
                    }
                )
        self.write_group(
            [
                ("fitboxers", "h4c.fitboxers", [row], ["fitboxer_id"]),
                ("session_packs", "h4c.session_packs", packs, ["id"]),
                ("rewards", "h4c.rewards", reward_rows, ["id"]),
            ]
        )

    # -- modes -------------------------------------------------------------

    def _run_weeks(self, start: date, end: date, *, classes: bool, shop: bool) -> None:
        today = datetime.now(MADRID).date()
        for a, b in week_ranges(start, end):
            last_exc: Optional[Exception] = None
            for attempt in range(3):
                try:
                    if classes:
                        upcoming = b >= today
                        self.load_classes_window(a, b, upcoming=upcoming, finished=True, details=True)
                        self.load_sessions(a, b)
                    if shop:
                        self.load_orders(a, b)
                        self.load_unsubs(a, b)
                        self.load_status(a, b)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    self.close_pg()
                    log.warning("Week %s..%s attempt %s failed: %s", a, b, attempt + 1, exc)
                    time.sleep(1.5 * (attempt + 1))
            if last_exc is not None:
                self.note_error(f"week {a}..{b}: {last_exc}")
                log.exception("Week chunk failed after retries", exc_info=last_exc)

    def backfill(self, start: date, end: date, shop_start: Optional[date] = None) -> None:
        shop_start = shop_start or BACKFILL_START
        log.info("Backfill classes %s .. %s; shop/status %s .. %s", start, end, shop_start, end)
        self.load_club()
        self.load_schedule()
        self.load_memberships()
        self.load_leads()
        self.load_aggregators()
        self.load_free_trials()
        self.load_fitboxers_pages()
        for ms in month_starts(min(start, shop_start), end):
            try:
                self.load_dashboard(ms.year, ms.month)
            except Exception as exc:
                self.close_pg()
                self.note_error(f"dashboard {ms}: {exc}")
        # Class attendance first (resumable via --from-date), then commercial
        # entities over the full history window so unsubs/orders/status gaps
        # get filled even when classes already exist through an earlier date.
        self._run_weeks(start, end, classes=True, shop=False)
        self._run_weeks(shop_start, end, classes=False, shop=True)
        all_ids = list(self.fitboxer_ids_seen)
        log.info("Loading %s fitboxer details", len(all_ids))
        self.load_fitboxer_details(all_ids)
        self.close_pg()

    def incremental(self, today: Optional[date] = None) -> None:
        today = today or datetime.now(MADRID).date()
        self.refresh = True  # always re-fetch incremental windows
        class_from = today - timedelta(days=2)
        class_to = today + timedelta(days=14)
        recent_from = today - timedelta(days=3)
        log.info("Incremental today=%s classes=%s..%s recent=%s..%s", today, class_from, class_to, recent_from, today)
        self.load_club()
        self.load_schedule()
        self.load_memberships()
        self.load_leads()
        try:
            self.load_dashboard(today.year, today.month)
        except Exception as exc:
            self.close_pg()
            self.note_error(f"dashboard incremental: {exc}")
        self.load_classes_window(class_from, class_to, upcoming=True, finished=True, details=True)
        self.load_sessions(recent_from, today)
        self.load_orders(recent_from, today)
        self.load_unsubs(recent_from, today)
        self.load_status(recent_from, today)
        self.load_fitboxers_pages()
        self.load_aggregators()
        self.load_free_trials()
        self.load_fitboxer_details(list(self.fitboxer_ids_seen))
        self.close_pg()

    def recent(self, start: date, end: date) -> None:
        """Load a bounded class/session window. Safe alongside a running backfill:

        uses a separate staging SQLite file and only upserts the given window
        (club, schedule, classes, participants, workouts, sessions, and
        fitboxer details for people seen in that window).
        """
        today = datetime.now(MADRID).date()
        self.refresh = True
        session_end = min(end, today)
        log.info("Recent today=%s classes=%s..%s sessions/shop=%s..%s", today, start, end, start, session_end)
        self.load_club()
        self.load_schedule()
        self.load_memberships()
        self.load_classes_window(start, end, upcoming=True, finished=True, details=True)
        self.load_sessions(start, session_end)
        self.load_orders(start, session_end)
        self.load_unsubs(start, session_end)
        self.load_status(start, session_end)
        ids = list(self.fitboxer_ids_seen)
        log.info("Loading %s fitboxer details (recent)", len(ids))
        if ids:
            self.load_fitboxer_details(ids)
        self.close_pg()


def resolve_club_id(client: H4CClient) -> str:
    role = client.current_role or {}
    club_id = role.get("clubId")
    if isinstance(club_id, str) and len(club_id) == 24 and all(c in "0123456789abcdef" for c in club_id.lower()):
        return club_id
    district = role.get("district") if isinstance(role.get("district"), dict) else {}
    did = district.get("id")
    if isinstance(did, str) and did:
        return did
    # fallback known Ourense club
    return "6656d71b24aa9a0008d08e49"


def start_run(conn: psycopg.Connection, mode: str, window_from: date, window_to: date, club_id: Optional[str]) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO h4c.extraction_runs (started_at, status, club_id, trigger, window_from, window_to, raw)
        VALUES (now(), 'running', %s, %s, %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (club_id, mode, window_from, window_to),
    )
    run_id = cur.fetchone()[0]
    conn.commit()
    return int(run_id)


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    counts: dict[str, int],
    error: Optional[str],
    extra: Optional[dict] = None,
) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE h4c.extraction_runs
           SET finished_at = now(),
               status = %s,
               rows_upserted = %s,
               error = %s,
               raw = %s
         WHERE id = %s
        """,
        (status, Jsonb(counts), error, Jsonb(extra or {}), run_id),
    )
    conn.commit()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract My Hit4Change data into schema h4c")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--backfill", action="store_true", help="Full history from 2024-11-01 to today")
    mode.add_argument("--incremental", action="store_true", help="Default incremental refresh")
    mode.add_argument(
        "--recent",
        action="store_true",
        help="Bounded recent window (own lock and staging file; safe alongside a backfill)",
    )
    p.add_argument("--pause", type=float, default=DEFAULT_PAUSE, help="Pause between portal requests (seconds)")
    p.add_argument("--email", default=os.environ.get("H4C_EMAIL", DEFAULT_EMAIL))
    p.add_argument("--from-date", dest="from_date", default=None, help="Override class backfill start (YYYY-MM-DD)")
    p.add_argument("--to-date", dest="to_date", default=None, help="Override end date (YYYY-MM-DD)")
    p.add_argument("--shop-from", dest="shop_from", default=None, help="Override orders/unsubs/status start (YYYY-MM-DD)")
    return p.parse_args(argv)


def _resolve_mode(args: argparse.Namespace) -> str:
    if args.recent:
        return "recent"
    if args.backfill:
        return "backfill"
    return "incremental"


def main(argv: Optional[list[str]] = None) -> int:
    setup_logging()
    args = parse_args(argv)
    mode = _resolve_mode(args)
    today = datetime.now(MADRID).date()
    end = date.fromisoformat(args.to_date) if args.to_date else today
    if mode == "backfill":
        start = date.fromisoformat(args.from_date) if args.from_date else BACKFILL_START
    elif mode == "recent":
        start = date.fromisoformat(args.from_date) if args.from_date else (today - timedelta(days=2))
        if args.to_date is None:
            end = today + timedelta(days=1)
    else:
        start = date.fromisoformat(args.from_date) if args.from_date else (today - timedelta(days=3))

    shop_start = date.fromisoformat(args.shop_from) if args.shop_from else BACKFILL_START
    shop_from_log = shop_start if mode == "backfill" else start
    log.info("Starting %s extract (classes %s .. %s, shop %s .. %s, pause=%s)", mode, start, end, shop_from_log, end, args.pause)

    run_id = None
    ext = None
    recent_lock = exclusive_file_lock(RECENT_LOCK, blocking=False) if mode == "recent" else None
    if recent_lock is not None:
        try:
            recent_lock.__enter__()
        except RuntimeError as exc:
            log.error("%s", exc)
            return 3

    try:
        probe = connect_postgres()
        probe.close()
    except Exception as exc:
        log.error("Postgres connection failed: %s", exc)
        if recent_lock is not None:
            try:
                recent_lock.__exit__(None, None, None)
            except Exception:
                pass
        return 2

    try:
        client = H4CClient(email=args.email, lang="es")
        info = client.login()
        club_id = resolve_club_id(client)
        log.info("Portal login ok club_id=%s login_count=%s", club_id, (info or {}).get("login_count"))
        if mode == "recent":
            stage_path = RECENT_STAGE
        elif mode == "incremental":
            stage_path = INCREMENTAL_STAGE
        else:
            stage_path = None
        ext = Extractor(
            client,
            pause=args.pause,
            club_id=club_id,
            refresh=(mode in ("incremental", "recent")),
            stage_path=stage_path,
        )
        conn = connect_postgres()
        try:
            run_id = start_run(conn, mode, start, end, club_id)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        t0 = time.time()
        if mode == "backfill":
            ext.backfill(start, end, shop_start=shop_start)
        elif mode == "recent":
            ext.recent(start, end)
        else:
            ext.incremental(today=end)
        duration = time.time() - t0
        status = "partial" if ext.errors else "success"
        err_text = " | ".join(ext.errors) if ext.errors else None
        conn = connect_postgres()
        try:
            finish_run(
                conn,
                run_id,
                status,
                ext.counts,
                err_text,
                {"duration_seconds": round(duration, 2), "error_count": len(ext.errors)},
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
        log.info(
            "Finished %s status=%s duration=%.1fs counts=%s errors=%s",
            mode,
            status,
            duration,
            ext.counts,
            len(ext.errors),
        )
        return 0 if status == "success" else 1
    except Exception as exc:
        log.exception("Extract failed")
        if run_id is not None:
            try:
                conn = connect_postgres()
                try:
                    counts = ext.counts if ext is not None else {}
                    finish_run(conn, run_id, "failed", counts, str(exc)[:2000], {"traceback": True})
                finally:
                    conn.close()
            except Exception:
                log.exception("Could not record failed extraction_run")
        return 1
    finally:
        if ext is not None:
            ext.close_pg()
        if recent_lock is not None:
            try:
                recent_lock.__exit__(None, None, None)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
