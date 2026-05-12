#!/usr/bin/env python3
"""iCloud CalDAV weekly digest with AI briefing, weather, travel estimates, and email."""

from __future__ import annotations

import argparse
import json
import logging
import math
import smtplib
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None  # type: ignore[misc, assignment]

LOG = logging.getLogger("calendar_digest")
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
GEOCODE_CACHE_PATH = SCRIPT_DIR / "geocode_cache.json"
PREVIEW_PATH = SCRIPT_DIR / "preview.html"
DISPLAY_TZ = ZoneInfo("Europe/London")
NOMINATIM_UA = "calendar-digest/1.0 (https://example.com/contact)"
_GOOGLE_CAL_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
_GOOGLE_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"

# caldav + lxml are heavy; load on first use so `--list-calendars` can print a hint first.
_caldav_bundle: tuple[Any, Any, Any] | None = None


def _load_caldav() -> tuple[Any, Any, Any]:
    global _caldav_bundle
    if _caldav_bundle is None:
        import caldav as cd
        from caldav.collection import (
            _extract_calendar_home_set_from_results as ex_home,
            _extract_calendars_from_propfind_results as ex_cals,
        )

        _caldav_bundle = (cd, ex_home, ex_cals)
    return _caldav_bundle


_google_bundle: tuple[Any, Any] | None = None


def _load_google() -> tuple[Any, Any]:
    global _google_bundle
    if _google_bundle is None:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
        _google_bundle = (build, service_account)
    return _google_bundle


def _google_calendar_service(cfg: dict[str, Any]) -> Any:
    build, service_account = _load_google()
    g = cfg["google"]
    sa_path = Path(g["service_account_file"])
    if not sa_path.is_absolute():
        sa_path = SCRIPT_DIR / sa_path
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=[_GOOGLE_CAL_SCOPE]
    ).with_subject(g["impersonate"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _google_gmail_service(cfg: dict[str, Any]) -> Any:
    build, service_account = _load_google()
    g = cfg["google"]
    sa_path = Path(g["service_account_file"])
    if not sa_path.is_absolute():
        sa_path = SCRIPT_DIR / sa_path
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=[_GOOGLE_GMAIL_SCOPE]
    ).with_subject(g["impersonate"])
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _stderr_progress(msg: str, t0: float) -> None:
    import sys

    dt = time.monotonic() - t0
    sys.stderr.write(f"calendar-digest [{dt:5.1f}s] {msg}\n")
    sys.stderr.flush()
WMO_DESCRIPTIONS: list[tuple[tuple[int, int], str]] = [
    ((0, 0), "clear sky"),
    ((1, 3), "partly cloudy"),
    ((45, 48), "fog"),
    ((51, 55), "drizzle"),
    ((61, 65), "rain"),
    ((71, 75), "snow"),
    ((80, 82), "showers"),
    ((95, 99), "thunderstorm"),
]
CALENDAR_COLORS = {"personal": "#2563eb", "family": "#059669", "runna": "#ea580c"}
# Lighter than caldav's default list (drops getctag, calendar-color, etc.) — smaller iCloud responses.
_CALENDAR_LIST_PROPS_MIN = (
    "{DAV:}resourcetype",
    "{DAV:}displayname",
)

BRIEFING_SECTIONS = (
    "At a Glance",
    "Clashes & Watch Points",
    "Weather & Outdoors",
    "Prep & Logistics",
    "Patterns",
    "Month Outlook",
)


def setup_logging(*, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(levelname)s %(name)s %(message)s" if verbose else "%(levelname)s %(message)s"
    logging.basicConfig(level=level, format=fmt, force=True)


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _disable_http3_on_caldav_client(client: Any) -> None:
    """Force TCP TLS for CalDAV (HTTP/1.1 or HTTP/2), not HTTP/3 over QUIC.

    caldav uses niquests (an HTTP/3-capable requests fork backed by urllib3-future).
    iCloud advertises HTTP/3 via Alt-Svc; the QUIC handshake can stall on some
    networks. We subclass niquests' own HTTPAdapter so response objects stay
    compatible with niquests' internals (avoids 'no .extension' AttributeError on
    401 retries that occurs when a requests.HTTPAdapter is mounted on a niquests
    session by mistake).
    """
    session = getattr(client, "session", None)
    if session is None:
        return

    try:
        from niquests.adapters import HTTPAdapter as NiquestsHTTPAdapter
        from niquests.adapters import HttpVersion

        class _NoHttp3Adapter(NiquestsHTTPAdapter):
            def init_poolmanager(self, connections, maxsize, block=False, quic_cache_layer=None, **pool_kwargs):
                pool_kwargs = dict(pool_kwargs)
                disabled = set(pool_kwargs.get("disabled_svn") or ())
                disabled.add(HttpVersion.h3)
                pool_kwargs["disabled_svn"] = disabled
                super().init_poolmanager(connections, maxsize, block=block, quic_cache_layer=None, **pool_kwargs)

        session.mount("https://", _NoHttp3Adapter())
    except ImportError:
        # niquests not available; fall back to urllib3-future approach for
        # requests-based sessions.
        try:
            from urllib3.backend import HttpVersion
            from requests.adapters import HTTPAdapter

            class _NoHttp3AdapterRequests(HTTPAdapter):
                def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
                    pool_kwargs = dict(pool_kwargs)
                    disabled = set(pool_kwargs.get("disabled_svn") or ())
                    disabled.add(HttpVersion.h3)
                    pool_kwargs["disabled_svn"] = disabled
                    super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

            session.mount("https://", _NoHttp3AdapterRequests())
        except ImportError:
            pass


def caldav_client(cfg: dict[str, Any], *, timeout: float | tuple[float, float]) -> Any:
    """CalDAV client: RFC6764 discovery off for explicit HTTPS URLs; HTTP connect/read timeouts."""
    cd, _, _ = _load_caldav()
    ic = cfg["icloud"]
    client = cd.DAVClient(
        ic["url"],
        username=ic["username"],
        password=ic["app_password"],
        enable_rfc6764=False,
        timeout=timeout,
    )
    _disable_http3_on_caldav_client(client)
    return client


def week_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    now = now.astimezone(timezone.utc)
    if now.weekday() == 6:
        week_start_date = (now + timedelta(days=1)).date()
    else:
        week_start_date = (now - timedelta(days=now.weekday())).date()
    week_start = datetime.combine(week_start_date, dt_time.min, tzinfo=timezone.utc)
    week_end = week_start + timedelta(days=7)
    return week_start, week_end


def is_first_sunday(now: datetime) -> bool:
    return now.weekday() == 6 and now.day <= 7


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def wmo_label(code: int) -> str:
    for (lo, hi), label in WMO_DESCRIPTIONS:
        if lo <= code <= hi:
            return label
    return "mixed conditions"


def weather_icon(code: int, precip: float) -> str:
    if code <= 3 and precip < 0.5:
        return "☀"
    if code <= 48:
        return "🌫"
    if code <= 67 or (71 <= code <= 77):
        return "🌧"
    if 80 <= code <= 82:
        return "🌦"
    if code >= 95:
        return "⛈"
    return "☁"


def _normalize_dt(value: datetime | date, default_tz: Any) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            base = default_tz if default_tz is not None else timezone.utc
            dt = dt.replace(tzinfo=base)
        return dt.astimezone(timezone.utc), False
    d = value
    return datetime.combine(d, dt_time.min, tzinfo=timezone.utc), True


@dataclass
class DigestEvent:
    title: str
    start: datetime
    end: datetime
    location: str
    calendar_label: str
    all_day: bool
    lat: float | None
    lon: float | None


@dataclass
class TravelSegment:
    from_event: str
    to_event: str
    from_end_local: str
    to_start_local: str
    to_start: datetime
    distance_km: float
    estimated_mins: int
    gap_mins: int
    is_tight: bool
    day_key: str
    to_location: str


def parse_vevent(component: IEvent, calendar_label: str) -> DigestEvent | None:
    try:
        title = str(component.get("summary", "(no title)"))
        loc = component.get("location")
        location = str(loc) if loc else ""

        dtstart = component.get("dtstart")
        if not dtstart:
            return None
        start_raw = dtstart.dt
        def_tz = start_raw.tzinfo if isinstance(start_raw, datetime) else None
        if not isinstance(start_raw, (datetime, date)):
            return None
        start, all_day = _normalize_dt(start_raw, def_tz)

        end = start + timedelta(hours=1)
        dtend = component.get("dtend")
        if dtend:
            end_raw = dtend.dt
            if isinstance(end_raw, datetime):
                end, _ = _normalize_dt(end_raw, def_tz)
            elif isinstance(end_raw, date):
                end = datetime.combine(end_raw, dt_time.min, tzinfo=timezone.utc)
                if all_day:
                    end = end - timedelta(seconds=1)
        dur = component.get("duration")
        if dur and not dtend:
            end = start + dur.dt

        return DigestEvent(
            title=title,
            start=start,
            end=end,
            location=location,
            calendar_label=calendar_label,
            all_day=all_day,
            lat=None,
            lon=None,
        )
    except Exception as e:  # noqa: BLE001
        LOG.warning("Skipping event parse failure: %s", e)
        return None


def _parse_google_event(item: dict[str, Any], calendar_label: str) -> DigestEvent | None:
    try:
        title = item.get("summary", "(no title)")
        location = item.get("location", "")
        start_info = item.get("start", {})
        end_info = item.get("end", {})
        if "date" in start_info:
            start_d = date.fromisoformat(start_info["date"])
            start = datetime.combine(start_d, dt_time.min, tzinfo=timezone.utc)
            end_d = date.fromisoformat(end_info.get("date", start_info["date"]))
            end = datetime.combine(end_d, dt_time.min, tzinfo=timezone.utc) - timedelta(seconds=1)
            all_day = True
        else:
            start = datetime.fromisoformat(start_info["dateTime"]).astimezone(timezone.utc)
            end = datetime.fromisoformat(
                end_info.get("dateTime", start_info["dateTime"])
            ).astimezone(timezone.utc)
            all_day = False
        return DigestEvent(
            title=title,
            start=start,
            end=end,
            location=location,
            calendar_label=calendar_label,
            all_day=all_day,
            lat=None,
            lon=None,
        )
    except Exception as e:  # noqa: BLE001
        LOG.warning("Skipping Google event parse failure: %s", e)
        return None


def fetch_icloud_events(cfg: dict[str, Any], start: datetime, end: datetime) -> list[DigestEvent]:
    icloud = cfg["icloud"]
    events: list[DigestEvent] = []
    try:
        client = caldav_client(cfg, timeout=(15.0, 120.0))
        principal = client.principal()
        cals = principal.calendars()
    except Exception as e:  # noqa: BLE001
        LOG.error("CalDAV connection failed: %s", e)
        return []

    cal_map: dict[str, str] = icloud["calendars"]
    by_name = {c.name: c for c in cals if getattr(c, "name", None)}
    missing = [n for n in cal_map if n not in by_name]
    if missing:
        LOG.warning("Calendar(s) not found: %s. Available: %s", missing, sorted(by_name.keys()))

    for name, label in cal_map.items():
        cal = by_name.get(name)
        if not cal:
            continue
        try:
            raw = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as e:  # noqa: BLE001
            LOG.error("Calendar search failed for %s: %s", name, e)
            continue
        for ev in raw:
            try:
                ical = ICalendar.from_ical(ev.data)
                for comp in ical.walk("VEVENT"):
                    de = parse_vevent(comp, label)
                    if de and de.start < end and de.end > start:
                        events.append(de)
            except Exception as e:  # noqa: BLE001
                LOG.warning("Skipping malformed calendar data in %s: %s", name, e)

    events.sort(key=lambda e: (e.start, e.end, e.title))
    return events


def fetch_google_events(cfg: dict[str, Any], start: datetime, end: datetime) -> list[DigestEvent]:
    if not cfg.get("google"):
        return []
    try:
        svc = _google_calendar_service(cfg)
        cal_id_map: dict[str, str] = {}
        page_token: str | None = None
        while True:
            resp = svc.calendarList().list(pageToken=page_token).execute()
            for item in resp.get("items", []):
                cal_id_map[item["summary"]] = item["id"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        time_min = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        events: list[DigestEvent] = []

        for name, label in cfg["google"]["calendars"].items():
            cal_id = cal_id_map.get(name)
            if not cal_id:
                LOG.warning(
                    "Google calendar not found: %r. Available: %s",
                    name,
                    sorted(cal_id_map),
                )
                continue
            page_token = None
            while True:
                resp = svc.events().list(
                    calendarId=cal_id,
                    singleEvents=True,
                    orderBy="startTime",
                    timeMin=time_min,
                    timeMax=time_max,
                    pageToken=page_token,
                ).execute()
                for item in resp.get("items", []):
                    de = _parse_google_event(item, label)
                    if de and de.start < end and de.end > start:
                        events.append(de)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        return events
    except Exception as e:  # noqa: BLE001
        LOG.error("Google Calendar fetch failed: %s", e)
        return []


def list_icloud_calendars(cfg: dict[str, Any]) -> None:
    """List calendar display names with minimal PROPFIND payload (faster on iCloud than full caldav list)."""
    t0 = time.monotonic()
    import sys

    sys.stderr.write(
        "calendar-digest: loading CalDAV/XML libraries (first time in a new environment "
        "often takes 15–45s; you will then see timed steps below).\n"
    )
    sys.stderr.flush()
    _, _extract_cal_home, _extract_calendars = _load_caldav()
    _stderr_progress("libraries ready; opening HTTPS session to iCloud…", t0)

    try:
        # (connect, read) — avoid hanging indefinitely on dead routes or huge responses.
        client = caldav_client(cfg, timeout=(12.0, 55.0))
        _stderr_progress("fetching current-user-principal…", t0)
        principal = client.principal()
        _stderr_progress(f"principal resolved; locating calendar home… ({principal.url})", t0)
        home: str | None = None
        try:
            r1 = client.propfind(str(principal.url), props=client.CALENDAR_HOME_SET_PROPS, depth=0)
            home = _extract_cal_home(r1.results)
        except Exception as e:  # noqa: BLE001
            LOG.debug("Minimal calendar-home PROPFIND failed, falling back: %s", e)

        names: list[str] = []
        if home:
            home = client._make_absolute_url(home)
            try:
                _stderr_progress("fetching calendar list (light PROPFIND)…", t0)
                r2 = client.propfind(home, props=list(_CALENDAR_LIST_PROPS_MIN), depth=1)
                for info in _extract_calendars(r2.results):
                    label = (info.name or "").strip() or (info.cal_id or "").strip()
                    if label:
                        names.append(label)
            except Exception as e:  # noqa: BLE001
                LOG.debug("Minimal calendar list PROPFIND failed, falling back: %s", e)
                names = []

        if not names:
            _stderr_progress(
                "light query returned no names; running full calendar scan (can take 30–120s on large accounts)…",
                t0,
            )
            for c in principal.calendars():
                n = getattr(c, "name", None)
                if n:
                    names.append(str(n).strip())

        _stderr_progress(f"done; printing {len(set(names))} calendar name(s).\n", t0)
        for n in sorted(set(names)):
            print(n)
    except Exception as e:  # noqa: BLE001
        LOG.error("Failed to list calendars: %s", e)
        raise SystemExit(1) from e


def fetch_weather(cfg: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]] | None]:
    w = cfg.get("weather") or {}
    if not w.get("enabled", True):
        return None, None
    params = {
        "latitude": w["latitude"],
        "longitude": w["longitude"],
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode,windspeed_10m_max",
        "timezone": "Europe/London",
        "forecast_days": 7,
    }
    name = w.get("location_name", "Location")
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        LOG.warning("Weather fetch failed: %s", e)
        return None, None

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    precip = daily.get("precipitation_sum") or []
    codes = daily.get("weathercode") or []
    wind = daily.get("windspeed_10m_max") or []
    lines = [f"WEATHER FORECAST ({name}):"]
    cells: list[dict[str, Any]] = []
    for i, day in enumerate(times):
        code = int(codes[i]) if i < len(codes) else 0
        pr = float(precip[i]) if i < len(precip) else 0.0
        desc = wmo_label(code)
        t_hi = tmax[i] if i < len(tmax) else ""
        t_lo = tmin[i] if i < len(tmin) else ""
        wspd = wind[i] if i < len(wind) else ""
        d = date.fromisoformat(day)
        label = d.strftime("%A %d %b")
        lines.append(f"  {label}: {t_hi}°C / {t_lo}°C, {desc}, {pr}mm, wind {wspd}km/h")
        cells.append(
            {
                "date": d,
                "label": d.strftime("%a %d"),
                "hi": t_hi,
                "lo": t_lo,
                "precip": pr,
                "code": code,
                "icon": weather_icon(code, pr),
                "wind": wspd,
            }
        )
    return "\n".join(lines), cells


def load_geocode_cache(path: Path) -> dict[str, dict[str, float]]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_geocode_cache(path: Path, cache: dict[str, dict[str, float]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def geocode_locations(events: list[DigestEvent], travel_enabled: bool, cache_path: Path) -> None:
    if not travel_enabled:
        return
    cache = load_geocode_cache(cache_path)
    locs = sorted({e.location.strip() for e in events if e.location.strip()})
    session = requests.Session()
    session.headers["User-Agent"] = NOMINATIM_UA
    dirty = False
    for loc in locs:
        if loc in cache:
            lat, lon = cache[loc]["lat"], cache[loc]["lon"]
            for e in events:
                if e.location.strip() == loc:
                    e.lat, e.lon = lat, lon
            continue
        try:
            r = session.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": loc, "format": "json", "limit": 1},
                timeout=30,
            )
            r.raise_for_status()
            arr = r.json()
            time.sleep(1)
            if not arr:
                LOG.debug("Geocode miss for %r", loc)
                continue
            lat = float(arr[0]["lat"])
            lon = float(arr[0]["lon"])
            cache[loc] = {"lat": lat, "lon": lon}
            dirty = True
            for e in events:
                if e.location.strip() == loc:
                    e.lat, e.lon = lat, lon
        except Exception as e:  # noqa: BLE001
            LOG.debug("Geocode error for %r: %s", loc, e)
            time.sleep(1)
    if dirty:
        save_geocode_cache(cache_path, cache)


def compute_travel_segments(
    week_events: list[DigestEvent],
    speed_kmh: float,
    buffer_mins: int,
) -> list[TravelSegment]:
    segs: list[TravelSegment] = []
    by_day: dict[str, list[DigestEvent]] = {}
    for e in week_events:
        if e.all_day:
            continue
        dk = e.start.astimezone(DISPLAY_TZ).date().isoformat()
        by_day.setdefault(dk, []).append(e)
    for dk, day_events in by_day.items():
        day_events.sort(key=lambda x: x.start)
        coords = [x for x in day_events if x.lat is not None and x.lon is not None]
        dlabel = date.fromisoformat(dk).strftime("%A %d %b")
        for a, b in zip(coords, coords[1:]):
            dist = haversine_km(a.lat, a.lon, b.lat, b.lon)
            est = int(math.ceil((dist / speed_kmh) * 60)) if speed_kmh > 0 else 0
            gap = int((b.start - a.end).total_seconds() // 60)
            tight = gap < est + buffer_mins
            segs.append(
                TravelSegment(
                    from_event=a.title,
                    to_event=b.title,
                    from_end_local=a.end.astimezone(DISPLAY_TZ).strftime("%H:%M"),
                    to_start_local=b.start.astimezone(DISPLAY_TZ).strftime("%H:%M"),
                    to_start=b.start,
                    distance_km=round(dist, 1),
                    estimated_mins=est,
                    gap_mins=gap,
                    is_tight=tight,
                    day_key=dlabel,
                    to_location=b.location,
                )
            )
    return segs


def format_events_block(events: list[DigestEvent], title: str) -> str:
    lines = [f"{title}:"]
    by_day: dict[date, list[DigestEvent]] = {}
    for e in events:
        d = e.start.astimezone(DISPLAY_TZ).date()
        by_day.setdefault(d, []).append(e)
    for d in sorted(by_day):
        lines.append(f"  {d.strftime('%A %d %b')}")
        day_list = sorted(by_day[d], key=lambda x: (not x.all_day, x.start, x.title))
        for e in day_list:
            badge = f"[{e.calendar_label}]"
            if e.all_day:
                t = "All day"
            else:
                st = e.start.astimezone(DISPLAY_TZ).strftime("%H:%M")
                en = e.end.astimezone(DISPLAY_TZ).strftime("%H:%M")
                t = f"{st}-{en}"
            loc = f" @ {e.location}" if e.location else ""
            lines.append(f"    {t} | {e.title} {badge}{loc}")
    return "\n".join(lines)


def format_travel_block(segments: list[TravelSegment]) -> str:
    if not segments:
        return ""
    by_day: dict[str, list[TravelSegment]] = {}
    for s in segments:
        by_day.setdefault(s.day_key, []).append(s)
    lines = ["TRAVEL ANALYSIS:"]
    for day in sorted(by_day.keys()):
        lines.append(f"  {day}:")
        for s in by_day[day]:
            status = "TIGHT" if s.is_tight else "OK"
            lines.append(
                f"    {s.from_event} (ends {s.from_end_local}) → {s.to_event} "
                f"(starts {s.to_start_local}) @ {s.to_location}"
            )
            lines.append(
                f"      Distance: {s.distance_km}km, est. travel: {s.estimated_mins} mins, "
                f"available gap: {s.gap_mins} mins — {status}"
            )
    return "\n".join(lines)


def build_ai_prompt(
    cfg: dict[str, Any],
    week_events: list[DigestEvent],
    month_events: list[DigestEvent],
    weather_text: str | None,
    travel_text: str,
    include_month: bool,
) -> str:
    parts: list[str] = []
    ctx = (cfg.get("context") or "").strip()
    if ctx:
        parts.append("CONTEXT:\n" + ctx)
    if weather_text:
        parts.append(weather_text)
    parts.append(format_events_block(week_events, "WEEK AHEAD"))
    if travel_text:
        parts.append(travel_text)
    if include_month and month_events:
        parts.append(format_events_block(month_events, "MONTH AHEAD"))
    return "\n\n".join(parts)


def generate_briefing(cfg: dict[str, Any], user_prompt: str) -> str:
    if not cfg.get("ai_summary_enabled", True):
        return ""
    key = (cfg.get("anthropic_api_key") or "").strip()
    if not key or Anthropic is None:
        LOG.warning("Anthropic API key missing or SDK not installed; skipping briefing")
        return ""
    system = (
        "You are a concise executive assistant. Produce a calendar briefing for the reader as 'you'. "
        "Use exactly these section titles on their own line, followed by one or two short paragraphs each: "
        "At a Glance; Clashes & Watch Points; Weather & Outdoors; Prep & Logistics; Patterns"
        + ("; Month Outlook" if "MONTH AHEAD:" in user_prompt else "")
        + ". No bullet points or numbered lists. Under 400 words total. Direct tone."
    )
    try:
        client = Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        block = msg.content[0]
        if block.type != "text":
            return ""
        return block.text.strip()
    except Exception as e:  # noqa: BLE001
        LOG.error("Anthropic API error: %s", e)
        return ""


def format_briefing_html(text: str) -> str:
    if not text:
        return ""
    paras: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line in BRIEFING_SECTIONS and buf:
            paras.append("<p>" + " ".join(buf) + "</p>")
            buf = []
        if line in BRIEFING_SECTIONS:
            paras.append(f'<p style="margin:12px 0 4px;font-weight:bold;color:#92400e">{line}</p>')
        elif line:
            buf.append(line)
    if buf:
        paras.append("<p>" + " ".join(buf) + "</p>")
    inner = "\n".join(paras)
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px">'
        '<tr><td style="background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:14px;color:#78350f">'
        f"{inner}</td></tr></table>"
    )


def badge_color(label: str) -> str:
    return CALENDAR_COLORS.get(label.lower(), "#6b7280")


def travel_warn_key(title: str, start: datetime) -> tuple[str, str]:
    return (title, start.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M"))


def build_travel_warnings(segments: list[TravelSegment]) -> dict[tuple[str, str], str]:
    warn: dict[tuple[str, str], str] = {}
    for s in segments:
        if s.is_tight:
            k = travel_warn_key(s.to_event, s.to_start)
            warn.setdefault(
                k,
                f"⚠ Tight transition: ~{s.estimated_mins} min travel from “{s.from_event}”.",
            )
    return warn


def render_weather_bar(cells: list[dict[str, Any]] | None) -> str:
    if not cells:
        return ""
    tds = []
    for c in cells:
        tds.append(
            "<td style=\"width:14%;text-align:center;padding:6px;border:1px solid #e5e7eb;"
            "font-size:12px;vertical-align:top\">"
            f'<div style="font-size:20px">{c["icon"]}</div>'
            f'<div style="font-weight:bold">{c["label"]}</div>'
            f'<div>{c["hi"]}° / {c["lo"]}°</div>'
            f'<div style="color:#6b7280">{c["precip"]}mm</div>'
            "</td>"
        )
    row = "<tr>" + "".join(tds) + "</tr>"
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:16px 0;border-collapse:collapse;max-width:640px">'
        f"{row}</table>"
    )


def render_html(
    briefing: str,
    weather_cells: list[dict[str, Any]] | None,
    week_events: list[DigestEvent],
    month_events: list[DigestEvent],
    segments: list[TravelSegment],
    week_start: datetime,
    include_month: bool,
    generated_at: datetime,
) -> str:
    twarn = build_travel_warnings(segments)
    counts: dict[str, int] = {}
    for e in week_events:
        counts[e.calendar_label] = counts.get(e.calendar_label, 0) + 1

    def day_sections(evts: list[DigestEvent], start_d: date, days: int) -> str:
        blocks: list[str] = []
        for i in range(days):
            d = start_d + timedelta(days=i)
            day_ev = [e for e in evts if e.start.astimezone(DISPLAY_TZ).date() == d]
            day_ev.sort(key=lambda x: (not x.all_day, x.start, x.title))
            head = f'<p style="margin:16px 0 6px;font-size:16px;font-weight:bold">{d.strftime("%A %d %B")}</p>'
            if not day_ev:
                blocks.append(head + '<p style="margin:0;color:#6b7280;font-style:italic">Nothing scheduled</p>')
                continue
            rows = []
            for e in day_ev:
                col = badge_color(e.calendar_label)
                if e.all_day:
                    t = "All day"
                else:
                    st = e.start.astimezone(DISPLAY_TZ).strftime("%H:%M")
                    en = e.end.astimezone(DISPLAY_TZ).strftime("%H:%M")
                    t = f"{st}–{en}"
                loc_html = ""
                if e.location:
                    key = travel_warn_key(e.title, e.start)
                    extra = ""
                    if key in twarn:
                        extra = (
                            f'<div style="color:#b45309;font-size:12px;margin-top:2px">{twarn[key]}</div>'
                        )
                    loc_html = (
                        f'<div style="color:#374151;font-size:13px;margin-top:2px">📍 {e.location}</div>{extra}'
                    )
                rows.append(
                    "<tr><td style=\"padding:8px 0;border-bottom:1px solid #e5e7eb\">"
                    f'<span style="color:#111827;font-weight:600">{t}</span> '
                    f'<span style="color:#111827">{e.title}</span> '
                    f'<span style="background:{col};color:#fff;padding:2px 6px;border-radius:4px;'
                    f'font-size:11px;margin-left:6px">{e.calendar_label}</span>'
                    f"{loc_html}</td></tr>"
                )
            blocks.append(head + '<table role="presentation" width="100%">' + "".join(rows) + "</table>")
        return "".join(blocks)

    week_start_d = week_start.astimezone(DISPLAY_TZ).date()
    week_body = day_sections(week_events, week_start_d, 7)

    summary_bits = [f"{len(week_events)} events total"]
    for lab, n in sorted(counts.items()):
        summary_bits.append(f"{lab}: {n}")
    week_summary = "<p style=\"margin:12px 0;color:#374151\">" + " · ".join(summary_bits) + "</p>"

    month_html = ""
    if include_month and month_events:
        chunk_start = week_start + timedelta(days=7)
        weeks_html: list[str] = []
        for w in range(5):
            ws = chunk_start + timedelta(days=7 * w)
            we = ws + timedelta(days=7)
            chunk = [e for e in month_events if ws <= e.start < we]
            label = f"{ws.astimezone(DISPLAY_TZ).strftime('%d %b')} – {(we - timedelta(days=1)).astimezone(DISPLAY_TZ).strftime('%d %b')}"
            inner = day_sections(chunk, ws.astimezone(DISPLAY_TZ).date(), 7)
            weeks_html.append(
                f'<p style="margin:18px 0 6px;font-weight:bold">Week of {label} ({len(chunk)} events)</p>{inner}'
            )
        month_html = (
            '<h2 style="margin-top:28px;font-size:18px">Month ahead</h2>'
            + "".join(weeks_html)
            + f'<p style="margin-top:12px;color:#374151">{len(month_events)} events in the next 30 days after this week.</p>'
        )

    header_ts = generated_at.astimezone(DISPLAY_TZ).strftime("%d %b %Y %H:%M %Z")
    briefing_html = format_briefing_html(briefing)
    weather_html = render_weather_bar(weather_cells)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Calendar Digest</title></head>
<body style="margin:0;padding:16px;background:#f9fafb;font-family:Arial,Helvetica,sans-serif;color:#111827">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;padding:20px">
<tr><td>
<h1 style="margin:0 0 6px;font-size:22px">Your Calendar Digest</h1>
<p style="margin:0 0 16px;color:#6b7280;font-size:13px">Generated {header_ts}</p>
{briefing_html}
{weather_html}
<h2 style="margin-top:8px;font-size:18px">Week ahead</h2>
{week_body}
{week_summary}
{month_html}
</td></tr></table></body></html>"""


def send_html_email(cfg: dict[str, Any], subject: str, html: str) -> None:
    smtp_cfg = cfg["smtp"]
    em = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = em["from"]
    msg["To"] = em["to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"])) as s:
        s.starttls()
        s.login(smtp_cfg["username"], smtp_cfg["password"])
        s.sendmail(em["from"], [em["to"]], msg.as_string())


def subject_for(week_start: datetime, include_month: bool) -> str:
    d = week_start.astimezone(DISPLAY_TZ)
    if include_month:
        return f"📅 Week + Month Ahead — {d.strftime('%B %Y')}"
    return f"📅 Week Ahead — {d.strftime('%d %b %Y')}"


def run_digest(cfg: dict[str, Any], preview: bool) -> None:
    now = datetime.now(timezone.utc)
    week_start, week_end = week_bounds_utc(now)
    first_sun = is_first_sunday(now.astimezone(DISPLAY_TZ))

    week_events = fetch_icloud_events(cfg, week_start, week_end)
    month_events: list[DigestEvent] = []
    if first_sun:
        month_events = fetch_icloud_events(cfg, week_end, week_end + timedelta(days=30))

    travel_cfg = cfg.get("travel") or {}
    travel_on = travel_cfg.get("enabled", True)
    if travel_on:
        geocode_locations(week_events + month_events, True, GEOCODE_CACHE_PATH)
        segments = compute_travel_segments(
            week_events,
            float(travel_cfg.get("default_speed_kmh", 45)),
            int(travel_cfg.get("buffer_mins", 10)),
        )
    else:
        segments = []

    weather_text, weather_cells = fetch_weather(cfg)
    travel_text = format_travel_block(segments) if travel_on else ""

    prompt = build_ai_prompt(cfg, week_events, month_events, weather_text, travel_text, first_sun)
    briefing = generate_briefing(cfg, prompt)

    html = render_html(
        briefing,
        weather_cells if weather_text else None,
        week_events,
        month_events,
        segments,
        week_start,
        first_sun and bool(month_events),
        now,
    )
    subj = subject_for(week_start, first_sun and bool(month_events))

    if preview:
        PREVIEW_PATH.write_text(html, encoding="utf-8")
        LOG.info("Wrote %s", PREVIEW_PATH)
        return

    send_html_email(cfg, subj, html)
    LOG.info("Email sent: %s", subj)


def main() -> None:
    p = argparse.ArgumentParser(description="iCloud calendar weekly digest")
    p.add_argument("--preview", action="store_true", help="Write preview.html instead of emailing")
    p.add_argument("--list-calendars", action="store_true", help="Print iCloud calendar names")
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="DEBUG logging (includes caldav HTTP details; use with --list-calendars when diagnosing)",
    )
    args = p.parse_args()
    setup_logging(verbose=args.verbose)

    if not CONFIG_PATH.exists():
        LOG.error("Missing %s — copy config.example.json", CONFIG_PATH)
        raise SystemExit(1)
    cfg = load_config(CONFIG_PATH)

    if args.list_calendars:
        list_icloud_calendars(cfg)
        return

    run_digest(cfg, preview=args.preview)


if __name__ == "__main__":
    main()
