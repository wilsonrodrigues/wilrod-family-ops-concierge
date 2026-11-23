"""Google Calendar API v3 OAuth 2.0 & Read/Write Synchronization for Family Ops Concierge.

Supports connecting a personal `@gmail.com` or Google Family Group shared calendar via:
1. Interactive OAuth 2.0 Web Server Flow (`Connect Google Account` button in Family Setup)
   with automatic offline refresh-token persistence, OR
2. Stateless Cloud Run injection via Secret Manager / environment variables
   (`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`, `GOOGLE_CALENDAR_ID`).

All recurring events written to Google Calendar use native RFC-5545 `RRULE` recurrence rules
(e.g. `RRULE:FREQ=WEEKLY;COUNT=30`) and store `[Participant]` tags, `category`, and arrival-buffer
metadata inside `extendedProperties.private` so phone Google Calendar apps and Family Ops Concierge
stay cleanly synchronized.
"""

from __future__ import annotations

import base64
import calendar as py_calendar
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import store
from .secrets import get_secret

logger = logging.getLogger(__name__)

# Prevent oauthlib from raising ScopeChanged exceptions when Google returns granted scopes in a different order
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

GCAL_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

_BRACKET_PREFIX_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(.+)$")
_SPORTS_KEYWORDS_RE = re.compile(
    r"\b(soccer|football|volleyball|tennis|beach\s+tennis|basketball|swim|swimming|gym|match|practice|futebol|v[oô]lei|treino)\b",
    re.IGNORECASE,
)
_MUSIC_KEYWORDS_RE = re.compile(
    r"\b(band|music|rehearsal|piano|guitar|drums|bass|singing|choir|violin|audition|ensaio|m[uú]sica|aula\s+de\s+canto)\b",
    re.IGNORECASE,
)


def parse_google_calendar_link(raw_input: str) -> str:
    """Extracts a canonical Google Calendar `calendarId` from any link format or raw ID.

    Accepts:
    - Embed / HTML URLs: `https://calendar.google.com/calendar/embed?src=abc%40group.calendar.google.com`
    - Shareable `cid=` URLs: `https://calendar.google.com/calendar/u/0?cid=YWJjQGdyb3VwLmNhbGVuZGFyLmdvb2dsZS5jb20`
    - iCal feed URLs: `https://calendar.google.com/calendar/ical/abc%40group.calendar.google.com/public/basic.ics`
    - Raw Calendar IDs: `family12345@group.calendar.google.com`, `user@gmail.com`, or `primary`
    """
    cleaned = (raw_input or "").strip()
    if not cleaned:
        return ""

    if cleaned.startswith(("http://", "https://")) or "calendar.google.com" in cleaned:
        target_url = cleaned if "://" in cleaned else f"https://{cleaned}"
        parsed = urlparse(target_url)
        qs = parse_qs(parsed.query)

        if "src" in qs and qs["src"]:
            return unquote(qs["src"][0]).strip()

        if "cid" in qs and qs["cid"]:
            raw_cid = unquote(qs["cid"][0]).strip()
            if "@" in raw_cid or raw_cid == "primary":
                return raw_cid
            try:
                padded = raw_cid + "=" * (-len(raw_cid) % 4)
                decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").strip()
                if "@" in decoded or decoded == "primary":
                    return decoded
            except Exception:  # noqa: BLE001
                pass

        ical_match = re.search(r"/ical/([^/]+)/", parsed.path)
        if ical_match:
            return unquote(ical_match.group(1)).strip()

        cal_path_match = re.search(r"/calendars/([^/]+)", parsed.path)
        if cal_path_match:
            return unquote(cal_path_match.group(1)).strip()

    return unquote(cleaned).strip()


def parse_oauth_client_json_blob(raw_json: str) -> tuple[str, str]:
    """Extracts `(client_id, client_secret)` from a Google Cloud `client_secret_*.json` download."""
    data = json.loads(raw_json)
    section = data.get("web") or data.get("installed") or data
    client_id = str(section.get("client_id") or "").strip()
    client_secret = str(section.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("JSON does not contain 'client_id' and 'client_secret'.")
    return client_id, client_secret


def resolve_oauth_client_pair() -> tuple[str, str]:
    """Resolves `(client_id, client_secret)` from Family Setup DB, Secret Manager, or `.env`."""
    row = store.get_gcal_integration_row()
    db_cid = (row.get("oauth_client_id") or "").strip()
    db_sec = (row.get("oauth_client_secret") or "").strip()
    if db_cid and db_sec:
        return db_cid, db_sec

    env_cid = (
        get_secret(
            ("GOOGLE_OAUTH_CLIENT_ID",),
            gcp_secret_id_env="GOOGLE_OAUTH_CLIENT_ID_SECRET",
            default_gcp_secret_id="family-ops-gcal-oauth-client-id",
        )
        or ""
    ).strip()
    env_sec = (
        get_secret(
            ("GOOGLE_OAUTH_CLIENT_SECRET",),
            gcp_secret_id_env="GOOGLE_OAUTH_CLIENT_SECRET_SECRET",
            default_gcp_secret_id="family-ops-gcal-oauth-client-secret",
        )
        or ""
    ).strip()
    return (db_cid or env_cid), (db_sec or env_sec)


def resolve_target_calendar_id() -> str:
    """Returns the configured target Google Calendar ID (from DB or `GOOGLE_CALENDAR_ID` env)."""
    row = store.get_gcal_integration_row()
    db_cal = (row.get("calendar_id") or "").strip()
    if db_cal:
        return db_cal
    env_cal = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    return parse_google_calendar_link(env_cal) if env_cal else ""


def get_authorized_gcal_credentials() -> Optional[Credentials]:
    """Builds and auto-refreshes `google.oauth2.credentials.Credentials` for the connected account."""
    client_id, client_secret = resolve_oauth_client_pair()
    row = store.get_gcal_integration_row()
    tokens_raw = (row.get("oauth_tokens_json") or "").strip()

    creds: Optional[Credentials] = None
    if tokens_raw:
        try:
            tok = json.loads(tokens_raw)
            eff_cid = client_id or str(tok.get("client_id") or "")
            eff_sec = client_secret or str(tok.get("client_secret") or "")
            creds = Credentials(
                token=tok.get("token"),
                refresh_token=tok.get("refresh_token"),
                token_uri=tok.get("token_uri") or _TOKEN_URI,
                client_id=eff_cid,
                client_secret=eff_sec,
                scopes=tok.get("scopes") or GCAL_SCOPES,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to deserialize stored Google Calendar token JSON: %s", type(exc).__name__)

    if creds is None:
        env_refresh = (
            get_secret(
                ("GOOGLE_OAUTH_REFRESH_TOKEN",),
                gcp_secret_id_env="GOOGLE_OAUTH_REFRESH_TOKEN_SECRET",
                default_gcp_secret_id="family-ops-gcal-refresh-token",
            )
            or ""
        ).strip()
        if env_refresh and client_id and client_secret:
            creds = Credentials(
                token=None,
                refresh_token=env_refresh,
                token_uri=_TOKEN_URI,
                client_id=client_id,
                client_secret=client_secret,
                scopes=GCAL_SCOPES,
            )

    if creds is None:
        return None

    if not creds.valid and creds.refresh_token and creds.client_id and creds.client_secret:
        try:
            creds.refresh(GoogleAuthRequest())
            _persist_credentials(creds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google Calendar OAuth token refresh failed: %s", type(exc).__name__)
            return None

    return creds if (creds.valid or creds.token) else None


def _persist_credentials(creds: Credentials) -> None:
    """Saves refreshed OAuth token payload safely in SQLite."""
    existing_row = store.get_gcal_integration_row()
    prev_refresh = ""
    if existing_row.get("oauth_tokens_json"):
        try:
            prev_refresh = str(json.loads(existing_row["oauth_tokens_json"]).get("refresh_token") or "")
        except json.JSONDecodeError:
            prev_refresh = ""

    payload = {
        "token": creds.token,
        "refresh_token": creds.refresh_token or prev_refresh,
        "token_uri": creds.token_uri or _TOKEN_URI,
        "client_id": creds.client_id or "",
        "client_secret": creds.client_secret or "",
        "scopes": list(creds.scopes or GCAL_SCOPES),
    }
    store.save_gcal_integration_row(oauth_tokens_json=json.dumps(payload))


def is_gcal_sync_active() -> bool:
    """Returns True when Google Calendar integration is enabled, pointed at a calendar, and authorized."""
    row = store.get_gcal_integration_row()
    env_forced = bool(
        os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
        and os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    )
    if not (bool(row.get("enabled")) or env_forced):
        return False
    cal_id = resolve_target_calendar_id()
    if not cal_id:
        return False
    return get_authorized_gcal_credentials() is not None


def get_public_gcal_status(redirect_uri_hint: str = "") -> dict[str, Any]:
    """Returns non-sensitive Google Calendar connection metadata for the Family Setup screen."""
    row = store.get_gcal_integration_row()
    cid, csec = resolve_oauth_client_pair()
    creds = get_authorized_gcal_credentials()
    cal_id = resolve_target_calendar_id()
    has_refresh = False
    if row.get("oauth_tokens_json"):
        try:
            has_refresh = bool(json.loads(row["oauth_tokens_json"]).get("refresh_token"))
        except json.JSONDecodeError:
            has_refresh = False
    elif os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip():
        has_refresh = True

    return {
        "enabled": bool(row.get("enabled")) or bool(os.environ.get("GOOGLE_CALENDAR_ID", "").strip()),
        "is_connected": creds is not None,
        "has_oauth_client": bool(cid and csec),
        "oauth_client_id_preview": f"{cid[:18]}..." if len(cid) > 18 else cid,
        "has_refresh_token": has_refresh,
        "connected_email": row.get("connected_email") or "",
        "calendar_link_input": row.get("calendar_link_input") or "",
        "calendar_id": cal_id,
        "calendar_summary": row.get("calendar_summary") or "",
        "active_sync": bool((row.get("enabled") or os.environ.get("GOOGLE_CALENDAR_ID")) and cal_id and creds is not None),
        "redirect_uri_hint": os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip() or redirect_uri_hint,
    }


def _build_oauth_client_config(redirect_uri: str) -> dict[str, Any]:
    client_id, client_secret = resolve_oauth_client_pair()
    if not client_id or not client_secret:
        raise ValueError(
            "App owner configuration needed: set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET "
            "in the backend .env file (or Google Cloud Secret Manager)."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }


def create_oauth_authorization_url(redirect_uri: str) -> str:
    """Starts an OAuth 2.0 Web Flow with PKCE and returns the Google consent screen URL."""
    eff_redirect = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip() or redirect_uri
    client_cfg = _build_oauth_client_config(eff_redirect)
    flow = Flow.from_client_config(
        client_cfg,
        scopes=GCAL_SCOPES,
        redirect_uri=eff_redirect,
        autogenerate_code_verifier=True,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    store.save_oauth_state(
        state_token=state,
        code_verifier=flow.code_verifier or "",
        redirect_uri=eff_redirect,
    )
    return str(auth_url)


def exchange_oauth_callback_code(*, state: str, code: str, fallback_redirect_uri: str) -> dict[str, Any]:
    """Completes the OAuth 2.0 callback, stores refresh tokens, and auto-detects the Family calendar."""
    saved_state = store.pop_oauth_state(state)
    if saved_state is None:
        raise ValueError("Invalid or expired OAuth state token. Please start the connection again from Family Setup.")

    eff_redirect = saved_state.get("redirect_uri") or fallback_redirect_uri
    client_cfg = _build_oauth_client_config(eff_redirect)
    flow = Flow.from_client_config(
        client_cfg,
        scopes=GCAL_SCOPES,
        state=state,
        redirect_uri=eff_redirect,
    )
    if saved_state.get("code_verifier"):
        flow.code_verifier = saved_state["code_verifier"]

    flow.fetch_token(code=code)
    creds = flow.credentials
    _persist_credentials(creds)

    connected_email = ""
    try:
        oauth2_svc = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        info = oauth2_svc.userinfo().get().execute()
        connected_email = str(info.get("email") or "")
    except Exception:  # noqa: BLE001
        pass

    cal_svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    row = store.get_gcal_integration_row()
    chosen_cal_id = (row.get("calendar_id") or "").strip()
    chosen_summary = (row.get("calendar_summary") or "").strip()

    try:
        cal_list = cal_svc.calendarList().list(minAccessRole="writer").execute().get("items", [])
        if chosen_cal_id:
            matched = next((c for c in cal_list if c.get("id") == chosen_cal_id), None)
            if matched:
                chosen_summary = str(matched.get("summary") or chosen_cal_id)
            else:
                meta = cal_svc.calendars().get(calendarId=chosen_cal_id).execute()
                chosen_summary = str(meta.get("summary") or chosen_cal_id)
        else:
            # Prefer a shared 'Family' calendar if one exists, otherwise primary
            fam_cal = next(
                (
                    c
                    for c in cal_list
                    if "family" in str(c.get("summary") or "").lower()
                    or "família" in str(c.get("summary") or "").lower()
                    or "familia" in str(c.get("summary") or "").lower()
                ),
                None,
            )
            picked = fam_cal or next((c for c in cal_list if c.get("primary")), None) or (cal_list[0] if cal_list else None)
            if picked:
                chosen_cal_id = str(picked.get("id") or "primary")
                chosen_summary = str(picked.get("summary") or chosen_cal_id)
                if not connected_email and picked.get("primary") and "@" in chosen_cal_id:
                    connected_email = chosen_cal_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("Calendar discovery after OAuth succeeded with warning: %s", type(exc).__name__)
        if not chosen_cal_id:
            chosen_cal_id = "primary"
            chosen_summary = "Primary Calendar"

    store.save_gcal_integration_row(
        enabled=1,
        calendar_id=chosen_cal_id,
        calendar_summary=chosen_summary,
        connected_email=connected_email,
    )
    return get_public_gcal_status(redirect_uri_hint=eff_redirect)


def list_writable_calendars() -> list[dict[str, Any]]:
    """Lists writable calendars on the connected personal Google account."""
    creds = get_authorized_gcal_credentials()
    if creds is None:
        raise ValueError("Google account is not connected yet.")
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    items = svc.calendarList().list().execute().get("items", [])
    writable: list[dict[str, Any]] = []
    for item in items:
        role = str(item.get("accessRole") or "")
        if role in ("owner", "writer"):
            writable.append(
                {
                    "calendar_id": str(item.get("id") or ""),
                    "summary": str(item.get("summaryOverride") or item.get("summary") or item.get("id") or ""),
                    "primary": bool(item.get("primary", False)),
                    "access_role": role,
                }
            )
    return writable


def verify_and_resolve_calendar_summary(calendar_id: str) -> str:
    """Verifies read access to `calendar_id` on Google Calendar and returns its human title."""
    creds = get_authorized_gcal_credentials()
    if creds is None:
        return ""
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    meta = svc.calendars().get(calendarId=calendar_id).execute()
    return str(meta.get("summary") or calendar_id)


def _household_tz_and_buffer() -> tuple[str, ZoneInfo, int]:
    hh = store.get_household_bundle()["household"]
    tz_name = str(hh.get("timezone") or "America/Sao_Paulo")
    try:
        tz_obj = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz_name = "UTC"
        tz_obj = ZoneInfo("UTC")
    buf_min = int(hh.get("sports_music_buffer_min", 30))
    return tz_name, tz_obj, buf_min


def _to_local_naive_iso(gcal_time_obj: dict[str, Any], tz_obj: ZoneInfo, is_end: bool = False) -> str:
    """Converts a Google Calendar `start` or `end` dict into local naive `YYYY-MM-DDTHH:MM:SS`."""
    dt_str = gcal_time_obj.get("dateTime")
    if dt_str:
        normalized = str(dt_str).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(tz_obj).replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%dT%H:%M:%S")

    date_only = str(gcal_time_obj.get("date") or "")
    if date_only:
        if is_end:
            # Google all-day `end.date` is exclusive (next day); map to 23:59:00 of the inclusive end date
            try:
                end_dt = datetime.strptime(date_only, "%Y-%m-%d") - timedelta(minutes=1)
                return end_dt.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return f"{date_only}T23:59:00"
        return f"{date_only}T00:00:00"

    return datetime.now(tz_obj).strftime("%Y-%m-%dT%H:%M:%S")


def _infer_category(summary: str, priv_props: dict[str, Any]) -> str:
    explicit = str(priv_props.get("category") or "").strip()
    if explicit in ("school_exam", "sports", "music", "birthday_party", "family_outing", "errand", "other"):
        return explicit
    if _SPORTS_KEYWORDS_RE.search(summary):
        return "sports"
    if _MUSIC_KEYWORDS_RE.search(summary):
        return "music"
    if "birthday" in summary.lower() or "anivers" in summary.lower():
        return "birthday_party"
    if "exam" in summary.lower() or "prova" in summary.lower():
        return "school_exam"
    return "other"


def _map_gcal_item_to_record(item: dict[str, Any], tz_obj: ZoneInfo, configured_buffer: int) -> dict[str, Any]:
    """Maps a Google Calendar API `Event` resource into our `CalendarEventRecordSchema` dictionary."""
    raw_summary = str(item.get("summary") or "Untitled Event").strip()
    priv = (item.get("extendedProperties") or {}).get("private") or {}

    m_prefix = _BRACKET_PREFIX_RE.match(raw_summary)
    if m_prefix:
        participants = str(priv.get("participants") or m_prefix.group(1).strip())
        formatted_title = raw_summary
    else:
        participants = str(priv.get("participants") or "All").strip() or "All"
        formatted_title = f"[{participants}] {raw_summary}"

    start_iso = _to_local_naive_iso(item.get("start") or {}, tz_obj, is_end=False)
    end_iso = _to_local_naive_iso(item.get("end") or {}, tz_obj, is_end=True)
    category = _infer_category(raw_summary, priv)

    buf_min = int(priv["buffer_minutes"]) if str(priv.get("buffer_minutes", "")).isdigit() else (
        configured_buffer if category in ("sports", "music") else 0
    )
    try:
        buf_dt = datetime.fromisoformat(start_iso) - timedelta(minutes=buf_min)
        buffer_start_iso = buf_dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        buffer_start_iso = start_iso

    recurring_id = str(item.get("recurringEventId") or "")
    recurrence_val = str(priv.get("recurrence") or ("WEEKLY" if recurring_id else "NONE"))
    if recurrence_val not in ("NONE", "DAILY", "WEEKLY", "BIWEEKLY", "MONTHLY"):
        recurrence_val = "WEEKLY" if recurring_id else "NONE"

    creator_email = str((item.get("creator") or {}).get("email") or "")
    created_by = str(priv.get("created_by") or creator_email or "Google Calendar")

    return {
        "event_id": str(item.get("id") or ""),
        "series_id": recurring_id,
        "recurrence": recurrence_val,
        "title": formatted_title,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "buffer_start_iso": buffer_start_iso,
        "category": category,
        "participants": participants,
        "location": str(item.get("location") or ""),
        "created_by": created_by,
        "notes": str(item.get("description") or ""),
        "created_at": str(item.get("created") or datetime.now(timezone.utc).isoformat()),
    }


def query_gcal_events(
    *,
    date_filter: Optional[str] = None,
    participant_filter: Optional[str] = None,
    service: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Lists single and expanded recurring events directly from the linked Google Calendar."""
    cal_id = resolve_target_calendar_id()
    tz_name, tz_obj, configured_buffer = _household_tz_and_buffer()

    if service is None:
        creds = get_authorized_gcal_credentials()
        if creds is None or not cal_id:
            return store.query_events(date_filter=date_filter, participant_filter=participant_filter)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now_local = datetime.now(tz_obj)
    if date_filter:
        day_start = datetime.strptime(date_filter, "%Y-%m-%d").replace(tzinfo=tz_obj)
        time_min = day_start.isoformat()
        time_max = (day_start + timedelta(days=1)).isoformat()
    else:
        time_min = (now_local - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        time_max = (now_local + timedelta(days=240)).replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    raw_items: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=cal_id,
                singleEvents=True,
                orderBy="startTime",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=250,
                timeZone=tz_name,
                pageToken=page_token,
            )
            .execute()
        )
        for it in resp.get("items", []):
            if it.get("status") != "cancelled":
                raw_items.append(it)
        page_token = resp.get("nextPageToken")
        if not page_token or len(raw_items) >= 500:
            break

    mapped = [_map_gcal_item_to_record(it, tz_obj, configured_buffer) for it in raw_items]
    if date_filter:
        mapped = [ev for ev in mapped if ev["start_iso"].startswith(date_filter)]
    if participant_filter and participant_filter.strip():
        needle = participant_filter.strip().lower()
        mapped = [
            ev
            for ev in mapped
            if needle in ev["participants"].lower()
            or ev["participants"].strip().lower() == "all"
            or "[all]" in ev["title"].lower()
        ]
    mapped.sort(key=lambda r: r["start_iso"])
    return mapped


def build_rrule_for_recurrence(recurrence: str, count: int) -> list[str]:
    """Builds a native RFC-5545 `RRULE` list with `COUNT=<count>` (default 30 occurrences)."""
    if recurrence == "NONE" or count <= 1:
        return []
    if recurrence == "DAILY":
        return [f"RRULE:FREQ=DAILY;COUNT={count}"]
    if recurrence == "WEEKLY":
        return [f"RRULE:FREQ=WEEKLY;COUNT={count}"]
    if recurrence == "BIWEEKLY":
        return [f"RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT={count}"]
    if recurrence == "MONTHLY":
        return [f"RRULE:FREQ=MONTHLY;COUNT={count}"]
    return []


def _advance_local_dt(base_dt: datetime, recurrence: str, idx: int) -> datetime:
    if idx <= 0 or recurrence == "NONE":
        return base_dt
    if recurrence == "DAILY":
        return base_dt + timedelta(days=idx)
    if recurrence == "WEEKLY":
        return base_dt + timedelta(weeks=idx)
    if recurrence == "BIWEEKLY":
        return base_dt + timedelta(weeks=2 * idx)
    if recurrence == "MONTHLY":
        total_months = (base_dt.month - 1) + idx
        year = base_dt.year + total_months // 12
        month = (total_months % 12) + 1
        max_day = py_calendar.monthrange(year, month)[1]
        return base_dt.replace(year=year, month=month, day=min(base_dt.day, max_day))
    return base_dt


def insert_gcal_event(
    *,
    title: str,
    start_iso: str,
    end_iso: str,
    buffer_start_iso: str,
    buffer_minutes: int,
    category: str,
    participants: str,
    location: str,
    recurrence: str,
    occurrences: int,
    created_by: str,
    notes: str,
    service: Optional[Any] = None,
) -> tuple[dict[str, Any], str, str]:
    """Inserts a single event or native `RRULE` recurring series into Google Calendar.

    Returns:
        `(first_event_record, series_id, last_occurrence_start_iso)`
    """
    cal_id = resolve_target_calendar_id()
    tz_name, tz_obj, configured_buffer = _household_tz_and_buffer()

    if service is None:
        creds = get_authorized_gcal_credentials()
        if creds is None or not cal_id:
            raise ValueError("Google Calendar integration is not connected.")
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    first_start = datetime.fromisoformat(start_iso)
    first_end = datetime.fromisoformat(end_iso)
    aware_start = first_start.replace(tzinfo=tz_obj)
    aware_end = first_end.replace(tzinfo=tz_obj)

    total_count = 1 if recurrence == "NONE" else max(1, occurrences)
    rrule_list = build_rrule_for_recurrence(recurrence, total_count)

    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": aware_start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": aware_end.isoformat(), "timeZone": tz_name},
        "extendedProperties": {
            "private": {
                "app": "family-ops-concierge",
                "category": category,
                "participants": participants,
                "created_by": created_by,
                "buffer_minutes": str(buffer_minutes),
                "recurrence": recurrence,
            }
        },
    }
    if location:
        body["location"] = location
    if notes:
        body["description"] = notes
    if rrule_list:
        body["recurrence"] = rrule_list

    created = service.events().insert(calendarId=cal_id, body=body).execute()
    master_id = str(created.get("id") or "")
    series_id = master_id if total_count > 1 else ""

    record = _map_gcal_item_to_record(created, tz_obj, configured_buffer)
    record["series_id"] = series_id
    record["recurrence"] = recurrence
    record["buffer_start_iso"] = buffer_start_iso
    record["created_by"] = created_by

    last_dt = _advance_local_dt(first_start, recurrence, total_count - 1)
    return record, series_id, last_dt.strftime("%Y-%m-%dT%H:%M:%S")


def delete_gcal_event(
    event_id: str,
    *,
    delete_series: bool = False,
    service: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """Deletes a single event instance or its entire recurring master series from Google Calendar."""
    cal_id = resolve_target_calendar_id()
    _, tz_obj, configured_buffer = _household_tz_and_buffer()

    if service is None:
        creds = get_authorized_gcal_credentials()
        if creds is None or not cal_id:
            return None
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    try:
        fetched = service.events().get(calendarId=cal_id, eventId=event_id).execute()
    except HttpError as exc:
        if getattr(exc.resp, "status", None) == 404:
            return None
        raise

    record = _map_gcal_item_to_record(fetched, tz_obj, configured_buffer)
    master_id = str(fetched.get("recurringEventId") or "")
    is_master_recurring = bool(fetched.get("recurrence"))

    if delete_series and (master_id or is_master_recurring):
        target_delete_id = master_id or event_id
        service.events().delete(calendarId=cal_id, eventId=target_delete_id).execute()
        record["series_id"] = target_delete_id
        record["deleted_count"] = 30
        return record

    service.events().delete(calendarId=cal_id, eventId=event_id).execute()
    record["deleted_count"] = 1
    return record


def query_active_calendar_events(
    *,
    date_filter: Optional[str] = None,
    participant_filter: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Unified read gateway: reads live Google Calendar when active, falling back cleanly to SQLite."""
    if is_gcal_sync_active():
        try:
            return query_gcal_events(date_filter=date_filter, participant_filter=participant_filter)
        except Exception as exc:  # noqa: BLE001
            logger.error("Google Calendar read failed (%s); falling back to local SQLite.", exc)
    return store.query_events(date_filter=date_filter, participant_filter=participant_filter)
