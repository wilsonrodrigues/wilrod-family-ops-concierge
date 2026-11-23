"""FastAPI backend uniting the Family Setup / Calendar / Lists Web UI with the Google ADK App Runner."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import quote
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / "family_concierge" / ".env")
load_dotenv(BASE_DIR / ".env")

from family_concierge import gcal_sync, store  # noqa: E402
from family_concierge.agent import (  # noqa: E402
    create_persistent_runner,
    sync_adk_session_db_backup,
)
from family_concierge.model_router import (  # noqa: E402
    FAST_MODEL_ID,
    FLASH_MODEL_ID,
    PRO_MODEL_ID,
    classify_request_complexity,
)
from family_concierge.observability import (  # noqa: E402
    emit_structured_log,
    get_recent_structured_logs,
    get_recent_trace_spans,
    setup_observability,
    trace_concierge_request,
)
from family_concierge.secrets import resolve_gemini_api_key  # noqa: E402
from family_concierge.tools import (  # noqa: E402
    DEFAULT_RECURRING_OCCURRENCES,
    delete_family_calendar_event,
    find_calendar_overlaps,
    normalize_participants_and_title,
    schedule_family_calendar_event,
    upsert_family_action_item,
)

setup_observability()
resolve_gemini_api_key()

ADK_SESSION_DB = os.environ.get(
    "FAMILY_ADK_SESSION_DB",
    f"sqlite+aiosqlite:///{(BASE_DIR / 'family_adk_sessions.db').as_posix()}",
)

MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024  # 12 MB per attachment
ALLOWED_MIME_PREFIXES = ("image/", "audio/", "application/pdf")
CONFIRMATION_FC_NAME = "adk_request_confirmation"
HOUSEHOLD_ADK_USER_ID = "foobar-household"

adk_runner, session_service = create_persistent_runner(db_url=ADK_SESSION_DB)
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def _get_session_lock(session_id: str) -> asyncio.Lock:
    """Returns an isolated per-session lock so distinct chat sessions run concurrently."""
    lock = _SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[session_id] = lock
    return lock

app = FastAPI(
    title="Family Ops Concierge",
    description="Shared Family Calendar, Multimodal Voice & Invitation Intake, Errands, Wishes, and ADK Concierge.",
    version="0.10.0",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
ADMIN_COOKIE_NAME = "family_admin_auth"
MEMBER_COOKIE_NAME = "family_member_token"
COOKIE_MAX_AGE_SECONDS = 365 * 24 * 3600


@dataclass
class AuthContext:
    """Resolved per-request caller identity ('admin' or 'member') and locked family member profile."""

    access_mode: str  # "admin" | "member"
    member: Optional[dict[str, Any]]

    @property
    def speaker_name(self) -> str:
        return (self.member or {}).get("name") or "Família"

    @property
    def speaker_id(self) -> str:
        return (self.member or {}).get("member_id") or "default"

    @property
    def speaker_filter(self) -> Optional[str]:
        return self.member["name"] if self.member and self.member.get("name") else None


def resolve_auth_context(
    request: Request,
    x_household_passcode: Optional[str] = Header(default=""),
    x_member_token: Optional[str] = Header(default=""),
) -> AuthContext:
    """Authenticates either a family Member via magic-link token or the household Admin via PIN/cookie."""
    meta = store.get_household_auth_meta()
    header_member_tok = (x_member_token or "").strip()

    # 1. Valid X-Member-Token header (personal magic link active in this tab/app)
    if header_member_tok:
        member = store.get_member_by_invite_token(header_member_tok)
        if member is not None:
            return AuthContext(access_mode="member", member=member)

    # 2. Admin PIN header or persistent HttpOnly Admin cookie
    if meta["has_passcode"]:
        candidate_pin = (x_household_passcode or "").strip()
        cookie_admin = (request.cookies.get(ADMIN_COOKIE_NAME) or "").strip()
        pin_ok = bool(candidate_pin) and store.verify_household_passcode(candidate_pin)
        cookie_ok = (
            bool(cookie_admin)
            and bool(meta["admin_cookie_digest"])
            and secrets.compare_digest(cookie_admin, meta["admin_cookie_digest"])
        )
        if pin_ok or cookie_ok:
            return AuthContext(
                access_mode="admin",
                member=store.get_admin_member(meta["admin_member_id"]),
            )

    if header_member_tok and meta["is_configured"]:
        raise HTTPException(
            status_code=401,
            detail="Este link de convite não é mais válido. Peça um novo link ao administrador da família.",
        )

    # 3. Persistent HttpOnly Member cookie (e.g. PWA Home Screen launch)
    cookie_member_tok = (request.cookies.get(MEMBER_COOKIE_NAME) or "").strip()
    if cookie_member_tok:
        member = store.get_member_by_invite_token(cookie_member_tok)
        if member is not None:
            return AuthContext(access_mode="member", member=member)

    # 4. First-time unconfigured onboarding OR PIN-free local/test mode
    if not meta["is_configured"] or not meta["has_passcode"]:
        return AuthContext(
            access_mode="admin",
            member=store.get_admin_member(meta["admin_member_id"]),
        )

    raise HTTPException(
        status_code=401,
        detail="Acesso restrito da família. Use seu link pessoal de convite ou desbloqueie como Administrador.",
    )


def require_admin_auth(auth: AuthContext = Depends(resolve_auth_context)) -> AuthContext:
    """Restricts household configuration, Google Calendar OAuth, and invite-token management to Admin only."""
    if auth.access_mode != "admin":
        raise HTTPException(
            status_code=403,
            detail="Apenas o administrador da família pode acessar ou alterar as configurações.",
        )
    return auth


class PasscodeVerifyRequest(BaseModel):
    passcode: str = Field(default="")


class MemberSetupItem(BaseModel):
    member_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=60)
    role: str = Field(default="Parent")
    avatar: str = Field(default="")
    color: str = Field(default="#1b6b5a")
    email: str = Field(default="")


class HouseholdSetupPayload(BaseModel):
    household_name: str = Field(..., min_length=2, max_length=80)
    timezone: str = Field(default="America/Sao_Paulo")
    preferred_language: str = Field(default="pt-BR")
    sports_music_buffer_min: int = Field(default=30, ge=0, le=120)
    admin_member_id: Optional[str] = Field(default=None)
    new_passcode: Optional[str] = Field(default=None)
    clear_passcode: bool = Field(default=False)
    members: list[MemberSetupItem] = Field(..., min_length=1)


class CreateEventPayload(BaseModel):
    title: str = Field(..., min_length=2, max_length=140)
    start_iso: str
    end_iso: str
    participants: str = Field(default="All")
    location: Optional[str] = Field(default="")
    recurrence: str = Field(default="NONE")
    occurrences: int = Field(default=DEFAULT_RECURRING_OCCURRENCES, ge=1, le=60)
    created_by: str = Field(default="Family")
    notes: Optional[str] = Field(default="")
    allow_overlap: bool = Field(default=False)


class CreateItemPayload(BaseModel):
    title: str = Field(..., min_length=3, max_length=140)
    item_type: str = Field(..., pattern="^(NEED_TO_DO|WANT_TO_DO)$")
    deadline_or_closing_date: Optional[str] = Field(default=None)
    assigned_to: str = Field(default="Anyone")
    created_by: str = Field(default="Family")


class PatchItemPayload(BaseModel):
    status: Optional[str] = Field(default=None)
    delete: bool = Field(default=False)


class MediaAttachmentPayload(BaseModel):
    mime_type: str = Field(..., min_length=3, max_length=100)
    data_base64: str = Field(..., min_length=4)
    filename: Optional[str] = Field(default="")


class ChatRequestPayload(BaseModel):
    message: str = Field(default="")
    attachments: list[MediaAttachmentPayload] = Field(default_factory=list)
    member_id: str = Field(default="mem-parent-1")
    member_name: str = Field(default="Family")
    member_avatar: str = Field(default="🙂")
    session_id: str = Field(default="shared-household-session")
    user_location: Optional[str] = Field(default=None, max_length=160)


class ChatConfirmationPayload(BaseModel):
    confirmation_call_id: str = Field(..., min_length=2)
    confirmed: bool
    member_id: str = Field(default="mem-parent-1")
    member_name: str = Field(default="Family")
    session_id: str = Field(default="shared-household-session")


class CreateChatSessionPayload(BaseModel):
    session_id: Optional[str] = Field(default=None)
    created_by: str = Field(default="Family")


class GoogleCalendarConfigPayload(BaseModel):
    enabled: Optional[bool] = Field(default=None)
    calendar_link_or_id: Optional[str] = Field(default=None)
    oauth_client_id: Optional[str] = Field(default=None)
    oauth_client_secret: Optional[str] = Field(default=None)
    oauth_client_json: Optional[str] = Field(default=None)


def _build_oauth_redirect_uri(request: Request) -> str:
    """Computes the exact OAuth 2.0 callback URL (honoring Cloud Run X-Forwarded-Proto/Host)."""
    explicit = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
        or "localhost:8080"
    )
    if host.endswith((".proxy.googlers.com", ".run.app")):
        proto = "https"
    return f"{proto}://{host}/api/gcal/oauth/callback"


@app.on_event("startup")
def _on_startup() -> None:
    store.init_db()
    with store._connect() as conn:
        conn.execute("UPDATE chat_sessions SET status = 'idle' WHERE status = 'running'")


@app.on_event("shutdown")
def _on_shutdown() -> None:
    configured_db = store.get_db_path()
    runtime_db = store.resolve_sqlite_runtime_path(configured_db)
    store.sync_sqlite_backup(runtime_db, configured_db)
    sync_adk_session_db_backup()


@app.get("/api/auth/status")
def auth_status(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    bundle = store.get_household_bundle()
    hh = bundle["household"]
    return {
        "requires_passcode": hh["has_passcode"],
        "is_configured": hh["is_configured"],
        "household_name": hh["household_name"],
        "preferred_language": hh["preferred_language"],
    }


@app.post("/api/auth/verify")
def auth_verify(payload: PasscodeVerifyRequest, response: Response) -> dict[str, Any]:
    ok = store.verify_household_passcode(payload.passcode)
    if not ok:
        raise HTTPException(status_code=401, detail="PIN do Administrador incorreto.")
    meta = store.get_household_auth_meta()
    if meta["admin_cookie_digest"]:
        response.set_cookie(
            key=ADMIN_COOKIE_NAME,
            value=meta["admin_cookie_digest"],
            max_age=COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
        )
    response.delete_cookie(key=MEMBER_COOKIE_NAME)
    return {"status": "ok", "access_mode": "admin"}


@app.get("/api/state")
def get_full_state(
    request: Request,
    response: Response,
    include_completed: bool = True,
    auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    is_admin = auth.access_mode == "admin"
    bundle = store.get_household_bundle(include_admin_secrets=is_admin)
    full_gcal = gcal_sync.get_public_gcal_status(
        redirect_uri_hint=_build_oauth_redirect_uri(request)
    )
    public_gcal = (
        full_gcal
        if is_admin
        else {
            "active_sync": full_gcal.get("active_sync", False),
            "calendar_summary": full_gcal.get("calendar_summary", ""),
        }
    )
    return {
        "access_mode": auth.access_mode,
        "locked_member": auth.member,
        "household": bundle["household"],
        "members": bundle["members"],
        "events": gcal_sync.query_active_calendar_events(),
        "items": store.query_action_items(include_completed=include_completed),
        "memories": store.query_household_memories(limit=20),
        "chat_sessions": store.list_chat_sessions(limit=40, created_by=auth.speaker_filter),
        "gcal": public_gcal,
    }


@app.get("/api/chat/sessions")
def list_chat_sessions_endpoint(
    limit: int = 40,
    auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    return {"sessions": store.list_chat_sessions(limit=limit, created_by=auth.speaker_filter)}


@app.post("/api/chat/sessions")
def create_chat_session_endpoint(
    payload: CreateChatSessionPayload,
    auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    sid = (payload.session_id or "").strip() or f"fam-session-{uuid.uuid4().hex[:8]}"
    owner = (
        auth.speaker_name
        if auth.access_mode == "member" or not payload.created_by or payload.created_by == "Family"
        else payload.created_by
    )
    created = store.ensure_chat_session(sid, created_by=owner, status="idle")
    detail = store.get_chat_session_detail(sid, owner_name=owner)
    return {
        "session": created,
        "messages": detail["messages"] if detail else [],
        "chat_sessions": store.list_chat_sessions(limit=40, created_by=auth.speaker_filter),
    }


@app.get("/api/chat/sessions/{session_id}")
def get_chat_session_endpoint(
    session_id: str,
    auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    existing_any = store.get_chat_session_detail(session_id)
    if existing_any is not None:
        owned = store.get_chat_session_detail(session_id, owner_name=auth.speaker_filter)
        if owned is None:
            raise HTTPException(status_code=403, detail="Esta conversa pertence a outro membro da família.")
        return owned
    created = store.ensure_chat_session(session_id, created_by=auth.speaker_name, status="idle")
    return {"session": created, "messages": []}


@app.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session_endpoint(
    session_id: str,
    auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    existing_any = store.get_chat_session_detail(session_id)
    if existing_any is not None:
        owned = store.get_chat_session_detail(session_id, owner_name=auth.speaker_filter)
        if owned is None:
            raise HTTPException(status_code=403, detail="Esta conversa pertence a outro membro da família.")
    store.delete_chat_session_record(session_id, owner_name=auth.speaker_filter)
    _SESSION_LOCKS.pop(session_id, None)
    try:
        await session_service.delete_session(
            app_name="family_concierge",
            user_id=HOUSEHOLD_ADK_USER_ID,
            session_id=session_id,
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "status": "success",
        "chat_sessions": store.list_chat_sessions(limit=40, created_by=auth.speaker_filter),
    }


@app.get("/api/memories")
def get_memories_endpoint(
    q: Optional[str] = None,
    _auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    return {"memories": store.query_household_memories(query=q, limit=25)}


@app.get("/api/telemetry")
def get_telemetry_endpoint(
    limit: int = 40,
    _auth: AuthContext = Depends(require_admin_auth),
) -> dict[str, Any]:
    """Exposes recent PII-redacted structured JSON INTENT/OUTCOME logs and OpenTelemetry trace spans."""
    return {
        "logs": get_recent_structured_logs(limit=limit),
        "spans": get_recent_trace_spans(limit=limit),
    }


@app.put("/api/household/setup")
def update_household_setup(
    payload: HouseholdSetupPayload,
    response: Response,
    _auth: AuthContext = Depends(require_admin_auth),
) -> dict[str, Any]:
    saved = store.save_household_setup(
        household_name=payload.household_name,
        timezone_str=payload.timezone,
        preferred_language=payload.preferred_language,
        sports_music_buffer_min=payload.sports_music_buffer_min,
        members=[m.model_dump() for m in payload.members],
        new_passcode=payload.new_passcode,
        clear_passcode=payload.clear_passcode,
        admin_member_id=payload.admin_member_id,
    )
    meta = store.get_household_auth_meta()
    if meta["admin_cookie_digest"]:
        response.set_cookie(
            key=ADMIN_COOKIE_NAME,
            value=meta["admin_cookie_digest"],
            max_age=COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
        )
    elif payload.clear_passcode:
        response.delete_cookie(key=ADMIN_COOKIE_NAME)
    return {
        "status": "success",
        "access_mode": "admin",
        "locked_member": store.get_admin_member(meta["admin_member_id"]),
        "household": saved["household"],
        "members": saved["members"],
    }


@app.post("/api/members/{member_id}/regenerate-token")
def regenerate_member_token_endpoint(
    member_id: str,
    _auth: AuthContext = Depends(require_admin_auth),
) -> dict[str, Any]:
    updated = store.regenerate_member_invite_token(member_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Membro da família não encontrado.")
    bundle = store.get_household_bundle(include_admin_secrets=True)
    return {
        "status": "success",
        "member": updated,
        "members": bundle["members"],
    }


@app.post("/api/household/reset")
def reset_household_endpoint(
    response: Response,
    _auth: AuthContext = Depends(require_admin_auth),
) -> dict[str, Any]:
    reset_bundle = store.reset_household_to_new_user()
    _SESSION_LOCKS.clear()
    response.delete_cookie(key=ADMIN_COOKIE_NAME)
    response.delete_cookie(key=MEMBER_COOKIE_NAME)
    return {
        "status": "success",
        "household": reset_bundle["household"],
        "members": reset_bundle["members"],
    }


@app.get("/api/gcal/status")
def get_gcal_status_endpoint(
    request: Request,
    _auth: AuthContext = Depends(require_admin_auth),
) -> dict[str, Any]:
    return gcal_sync.get_public_gcal_status(
        redirect_uri_hint=_build_oauth_redirect_uri(request)
    )


@app.put("/api/gcal/config")
def update_gcal_config_endpoint(
    payload: GoogleCalendarConfigPayload,
    request: Request,
    _auth: AuthContext = Depends(require_admin_auth),
) -> dict[str, Any]:
    updates: dict[str, Any] = {}

    if payload.oauth_client_json and payload.oauth_client_json.strip():
        try:
            cid, csec = gcal_sync.parse_oauth_client_json_blob(payload.oauth_client_json.strip())
            updates["oauth_client_id"] = cid
            updates["oauth_client_secret"] = csec
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=f"Invalid OAuth client_secret JSON file: {exc}",
            ) from exc
    else:
        if payload.oauth_client_id is not None and payload.oauth_client_id.strip():
            updates["oauth_client_id"] = payload.oauth_client_id.strip()
        if payload.oauth_client_secret is not None and payload.oauth_client_secret.strip():
            updates["oauth_client_secret"] = payload.oauth_client_secret.strip()

    if payload.calendar_link_or_id is not None:
        raw_link = payload.calendar_link_or_id.strip()
        parsed_cal_id = gcal_sync.parse_google_calendar_link(raw_link)
        updates["calendar_link_input"] = raw_link
        updates["calendar_id"] = parsed_cal_id
        if parsed_cal_id and gcal_sync.get_authorized_gcal_credentials() is not None:
            try:
                updates["calendar_summary"] = gcal_sync.verify_and_resolve_calendar_summary(parsed_cal_id)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Connected Google account could not access calendar '{parsed_cal_id}': {exc}. "
                        "Verify the link/ID or make sure this calendar is shared with your connected account."
                    ),
                ) from exc
        elif not parsed_cal_id:
            updates["calendar_summary"] = ""

    if payload.enabled is not None:
        updates["enabled"] = 1 if payload.enabled else 0

    store.save_gcal_integration_row(**updates)
    return {
        "status": "success",
        "gcal": gcal_sync.get_public_gcal_status(
            redirect_uri_hint=_build_oauth_redirect_uri(request)
        ),
        "events": gcal_sync.query_active_calendar_events(),
    }


@app.get("/api/gcal/calendars", dependencies=[Depends(require_admin_auth)])
def list_gcal_calendars_endpoint() -> dict[str, Any]:
    try:
        calendars = gcal_sync.list_writable_calendars()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not list Google Calendars: {exc}") from exc
    return {"calendars": calendars}


@app.get("/api/gcal/oauth/start", dependencies=[Depends(require_admin_auth)])
def start_gcal_oauth_endpoint(request: Request) -> dict[str, Any]:
    redirect_uri = _build_oauth_redirect_uri(request)
    try:
        auth_url = gcal_sync.create_oauth_authorization_url(redirect_uri)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"auth_url": auth_url, "redirect_uri": redirect_uri}


@app.get("/api/gcal/oauth/callback")
def complete_gcal_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    """Handles browser redirect back from Google's OAuth 2.0 consent screen."""
    if error:
        return RedirectResponse(url=f"/?gcal_error={quote(error)}")
    if not code or not state:
        return RedirectResponse(url="/?gcal_error=Missing+OAuth+code+or+state")

    try:
        gcal_sync.exchange_oauth_callback_code(
            state=state,
            code=code,
            fallback_redirect_uri=_build_oauth_redirect_uri(request),
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/?gcal_error={quote(str(exc))}")

    return RedirectResponse(url="/?gcal_connected=1")


@app.post("/api/gcal/disconnect", dependencies=[Depends(require_admin_auth)])
def disconnect_gcal_endpoint(request: Request) -> dict[str, Any]:
    store.save_gcal_integration_row(
        enabled=0,
        connected_email="",
        calendar_summary="",
        oauth_tokens_json="",
    )
    return {
        "status": "success",
        "gcal": gcal_sync.get_public_gcal_status(
            redirect_uri_hint=_build_oauth_redirect_uri(request)
        ),
        "events": gcal_sync.query_active_calendar_events(),
    }


@app.post("/api/events")
def create_event_endpoint(
    payload: CreateEventPayload,
    auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    effective_author = (
        auth.speaker_name
        if auth.access_mode == "member" or not payload.created_by or payload.created_by in ("Family", "Família")
        else payload.created_by
    )
    author_ctx = SimpleNamespace(
        state={"active_member_name": effective_author},
        tool_confirmation=None,
    )
    result = schedule_family_calendar_event(
        title=payload.title,
        start_iso=payload.start_iso,
        end_iso=payload.end_iso,
        participants=payload.participants,
        location=payload.location,
        recurrence=payload.recurrence,  # type: ignore[arg-type]
        occurrences=payload.occurrences,
        notes=payload.notes,
        allow_overlap=payload.allow_overlap,
        tool_context=author_ctx,  # type: ignore[arg-type]
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result)

    result["event"]["created_by"] = effective_author
    return result


@app.delete("/api/events/{event_id}", dependencies=[Depends(resolve_auth_context)])
def remove_event_endpoint(event_id: str, delete_series: bool = False) -> dict[str, Any]:
    res = delete_family_calendar_event(
        event_id=event_id,
        delete_entire_series=delete_series,
        tool_context=None,
    )
    if res.get("status") == "error":
        raise HTTPException(status_code=404, detail=res.get("message") or "Event not found.")
    return res


@app.post("/api/items")
def create_item_endpoint(
    payload: CreateItemPayload,
    auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    effective_author = (
        auth.speaker_name
        if auth.access_mode == "member" or not payload.created_by or payload.created_by in ("Family", "Família")
        else payload.created_by
    )
    result = upsert_family_action_item(
        title=payload.title,
        item_type=payload.item_type,  # type: ignore[arg-type]
        deadline_or_closing_date=payload.deadline_or_closing_date or None,
        status="OPEN",
        assigned_to=payload.assigned_to,
        tool_context=None,
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result)
    item = result["item"]
    with store._connect() as conn:
        conn.execute(
            "UPDATE action_items SET created_by = ? WHERE item_id = ?",
            (effective_author, item["item_id"]),
        )
    item["created_by"] = effective_author
    return result


@app.patch("/api/items/{item_id}", dependencies=[Depends(resolve_auth_context)])
def patch_item_endpoint(item_id: str, payload: PatchItemPayload) -> dict[str, Any]:
    updated = store.toggle_or_delete_action_item(
        item_id=item_id,
        delete=payload.delete,
        status=payload.status,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Item not found.")
    return {"status": "success", "item": updated}


def _decode_media_parts(attachments: list[MediaAttachmentPayload]) -> list[types.Part]:
    """Validates and decodes base64 image/audio/PDF attachments into ADK GenAI Parts."""
    decoded_parts: list[types.Part] = []
    for att in attachments:
        clean_mime = att.mime_type.split(";", 1)[0].strip().lower()
        if not any(clean_mime.startswith(prefix) for prefix in ALLOWED_MIME_PREFIXES):
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported attachment type '{att.mime_type}'. Upload an image, audio recording, or PDF.",
            )
        raw_b64 = att.data_base64.strip()
        if "," in raw_b64 and raw_b64.startswith("data:"):
            raw_b64 = raw_b64.split(",", 1)[1]
        try:
            raw_bytes = base64.b64decode(raw_b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid base64 media payload: {exc}") from exc

        if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=400, detail="Attachment exceeds 12 MB limit.")
        if not raw_bytes:
            raise HTTPException(status_code=400, detail="Attachment is empty.")

        decoded_parts.append(types.Part.from_bytes(data=raw_bytes, mime_type=clean_mime))
    return decoded_parts


def _describe_pending_confirmation(fc: types.FunctionCall) -> dict[str, Any]:
    """Builds a friendly summary for an `adk_request_confirmation` client function call."""
    args = fc.args or {}
    orig = args.get("originalFunctionCall") or {}
    tool_name = str(orig.get("name") or "calendar_action")
    tool_args = orig.get("args") if isinstance(orig.get("args"), dict) else {}

    summary = f"Confirmar ação '{tool_name}'"
    warnings: list[str] = []
    if tool_name == "delete_family_calendar_event":
        event_id = str(tool_args.get("event_id") or "")
        delete_series = bool(
            tool_args.get("delete_entire_series") or tool_args.get("delete_series", False)
        )
        matched = next(
            (ev for ev in gcal_sync.query_active_calendar_events() if ev["event_id"] == event_id),
            None,
        )
        if matched:
            scope = "toda a série recorrente" if delete_series and matched.get("series_id") else "apenas esta ocorrência"
            summary = f"Remover '{matched['title']}' em {matched['start_iso'][:16].replace('T', ' ')} ({scope})"
        else:
            scope = "série recorrente" if delete_series else "ocorrência única"
            summary = f"Remover evento {event_id} ({scope})"
    elif tool_name == "schedule_family_calendar_event":
        raw_title = str(tool_args.get("title") or "Evento")
        participants = str(tool_args.get("participants") or "All")
        _, formatted_title = normalize_participants_and_title(raw_title, participants)
        recurrence = str(tool_args.get("recurrence") or "NONE")
        occurrences = int(
            tool_args.get("occurrences")
            or (1 if recurrence == "NONE" else DEFAULT_RECURRING_OCCURRENCES)
        )
        start_iso_raw = str(tool_args.get("start_iso") or "")
        end_iso_raw = str(tool_args.get("end_iso") or "")
        start_display = start_iso_raw[:16].replace("T", " ")
        scope_label = (
            "evento único"
            if recurrence == "NONE"
            else f"{recurrence} x {occurrences} repetições"
        )
        warnings = find_calendar_overlaps(
            start_iso=start_iso_raw,
            end_iso=end_iso_raw,
            participants=participants,
            title=formatted_title,
            location=str(tool_args.get("location") or ""),
            recurrence=recurrence,
            occurrences=occurrences,
        )
        summary = f"Agendar '{formatted_title}' em {start_display} ({scope_label})"

    return {
        "confirmation_call_id": fc.id,
        "tool_name": tool_name,
        "tool_args": tool_args,
        "summary": summary,
        "warnings": warnings,
    }


async def _execute_runner_turn(
    *,
    operation_name: str,
    user_id: str,
    member_name: str,
    session_id: str,
    new_message: types.Content,
    state_delta: Optional[dict[str, Any]] = None,
    include_admin_secrets: bool = False,
    speaker_filter: Optional[str] = None,
) -> dict[str, Any]:
    """Runs one ADK turn inside a root OpenTelemetry span and gathers state + traces."""
    reply_chunks: list[str] = []
    tool_calls_made: list[str] = []
    agents_invoked: list[str] = []
    pending_confirmations: list[dict[str, Any]] = []

    with trace_concierge_request(
        operation_name,
        session_id=session_id,
        member_id=user_id,
        member_name=member_name,
    ) as root_span:
        trace_id = f"{root_span.get_span_context().trace_id:032x}"
        emit_structured_log(
            event_type="HTTP_TURN",
            phase="INTENT",
            message=f"Handling {operation_name} for member '{member_name}'",
            session_id=session_id,
            correlation_id=trace_id,
            payload={"member_id": user_id, "operation": operation_name},
            span=root_span,
        )

        async for event in adk_runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=new_message,
            state_delta=state_delta,
        ):
            if event.author and event.author != "user" and event.author not in agents_invoked:
                agents_invoked.append(event.author)
            for fc in event.get_function_calls():
                if fc.name == CONFIRMATION_FC_NAME:
                    pending_confirmations.append(_describe_pending_confirmation(fc))
                elif fc.name:
                    tool_calls_made.append(fc.name)
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text and not part.thought:
                        reply_chunks.append(part.text)

        models_used: list[str] = [FLASH_MODEL_ID]
        if "weekend_planning_pipeline" in tool_calls_made:
            for m_id in (FAST_MODEL_ID, PRO_MODEL_ID):
                if m_id not in models_used:
                    models_used.append(m_id)
        if "schedule_conflict_advisor_pro" in tool_calls_made or "schedule_conflict_advisor_pro" in agents_invoked:
            if PRO_MODEL_ID not in models_used:
                models_used.append(PRO_MODEL_ID)

        reply_text = "\n".join(reply_chunks).strip()
        if not reply_text and not pending_confirmations:
            reply_text = "Feito!"

        root_span.set_attribute("concierge.tools_called_count", len(tool_calls_made))
        root_span.set_attribute("concierge.pending_confirmations_count", len(pending_confirmations))
        emit_structured_log(
            event_type="HTTP_TURN",
            phase="OUTCOME",
            message=f"Completed {operation_name} ({len(tool_calls_made)} tool call(s))",
            session_id=session_id,
            correlation_id=trace_id,
            outcome_status="PAUSED_FOR_HITL" if pending_confirmations else "SUCCESS",
            payload={
                "tools_called": tool_calls_made,
                "agents_invoked": agents_invoked,
                "models_used": models_used,
                "reply_preview": reply_text[:240],
            },
            span=root_span,
        )

    await asyncio.to_thread(sync_adk_session_db_backup)
    bundle = store.get_household_bundle(include_admin_secrets=include_admin_secrets)
    return {
        "reply": reply_text,
        "session_id": session_id,
        "trace_id": trace_id,
        "tool_calls": tool_calls_made,
        "agents_invoked": agents_invoked,
        "models_used": models_used,
        "pending_confirmations": pending_confirmations,
        "household": bundle["household"],
        "members": bundle["members"],
        "events": gcal_sync.query_active_calendar_events(),
        "items": store.query_action_items(include_completed=True),
        "memories": store.query_household_memories(limit=20),
        "chat_sessions": store.list_chat_sessions(limit=40, created_by=speaker_filter),
    }


@app.post("/api/chat")
async def chat_with_concierge(
    payload: ChatRequestPayload,
    auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    raw_user_text = payload.message.strip()
    if not raw_user_text and not payload.attachments:
        raise HTTPException(status_code=400, detail="Envie uma mensagem, áudio ou imagem.")

    effective_member_name = (
        auth.speaker_name
        if auth.access_mode == "member" or not payload.member_name or payload.member_name in ("Family", "Família")
        else payload.member_name
    )
    effective_member_id = (
        auth.speaker_id
        if auth.access_mode == "member" or not payload.member_id
        else payload.member_id
    )

    existing_sess = store.get_chat_session_detail(payload.session_id)
    if existing_sess is not None:
        owned_sess = store.get_chat_session_detail(payload.session_id, owner_name=effective_member_name)
        if owned_sess is None:
            raise HTTPException(status_code=403, detail="Esta conversa pertence a outro membro da família.")

    media_parts = _decode_media_parts(payload.attachments)

    bundle = store.get_household_bundle()
    tz_name = bundle["household"].get("timezone") or "America/Sao_Paulo"
    preferred_lang = bundle["household"].get("preferred_language") or "pt-BR"
    try:
        now_local = datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        now_local = datetime.now()

    has_audio = any(a.mime_type.lower().startswith("audio/") for a in payload.attachments)
    display_user_text = raw_user_text or (
        "Enviou uma mensagem de voz" if has_audio else "Enviou uma imagem/convite"
    )
    user_text = raw_user_text
    if not user_text:
        if has_audio:
            user_text = (
                f"(Spoken audio message attached; preferred household language: {preferred_lang}. "
                "Listen carefully and fulfill or confirm the request following household rules.)"
            )
        else:
            user_text = (
                f"(Image/invitation attached; preferred household language: {preferred_lang}. "
                "Extract the event or list details from the image, check participant and location rules, "
                "and propose or schedule it.)"
            )

    attachments_meta = [
        {"filename": a.filename or "attachment", "mime_type": a.mime_type}
        for a in payload.attachments
    ]
    today_ymd = now_local.strftime("%Y-%m-%d")
    already_replied_today = store.has_assistant_reply_on_local_date(
        member_name=effective_member_name,
        local_date_ymd=today_ymd,
        tz_name=tz_name,
        session_id=payload.session_id,
    )

    store.ensure_chat_session(
        payload.session_id,
        first_prompt=display_user_text,
        created_by=effective_member_name,
        status="running",
    )
    store.append_chat_message(
        payload.session_id,
        role="user",
        content=display_user_text,
        speaker_name=effective_member_name,
        speaker_avatar="",
        attachments_meta=attachments_meta,
    )

    date_directive = (
        "First Reply Today: NO (do NOT repeat today's date in your reply; use Current Local Date silently for relative dates)"
        if already_replied_today
        else "First Reply Today: YES (mention today's date at most once only if helpful)"
    )
    saved_default_loc = str(bundle["household"].get("default_location") or "").strip()
    incoming_device_loc = str(payload.user_location or "").strip()
    effective_location = incoming_device_loc or saved_default_loc
    location_directive = (
        f"Location: {effective_location}"
        if effective_location
        else "Location: (not set yet - ask ONLY if the user's request requires 'near me' local proximity)"
    )
    contextual_prompt = (
        f"[Active Family Member: {effective_member_name} | "
        f"Current Local Date: {now_local.strftime('%Y-%m-%d (%A) %H:%M')} | "
        f"{location_directive} | "
        f"{date_directive}]\n"
        f"{user_text}"
    )

    content = types.Content(
        role="user",
        parts=[types.Part.from_text(text=contextual_prompt), *media_parts],
    )

    route_hint = classify_request_complexity(user_text, has_media=bool(media_parts))
    session_lock = _get_session_lock(payload.session_id)
    final_status = "idle"
    try:
        async with session_lock:
            if store.get_chat_session_detail(payload.session_id) is None:
                raise HTTPException(status_code=410, detail="Chat session was deleted.")
            store.set_chat_session_status(payload.session_id, "running")
            turn_result = await _execute_runner_turn(
                operation_name="concierge.chat_request",
                user_id=HOUSEHOLD_ADK_USER_ID,
                member_name=effective_member_name,
                session_id=payload.session_id,
                new_message=content,
                state_delta={
                    "active_member_name": effective_member_name,
                    "active_member_id": effective_member_id,
                    "routed_tier": route_hint.tier.value,
                    "already_replied_today": already_replied_today,
                    "effective_user_location": effective_location,
                },
                include_admin_secrets=(auth.access_mode == "admin"),
                speaker_filter=auth.speaker_filter,
            )
            if store.get_chat_session_detail(payload.session_id) is None:
                return turn_result
            final_status = "awaiting_approval" if turn_result["pending_confirmations"] else "idle"
            store.append_chat_message(
                payload.session_id,
                role="assistant",
                content=turn_result["reply"],
                models_used=turn_result["models_used"],
                tool_calls=turn_result["tool_calls"],
                pending_confirmations=turn_result["pending_confirmations"],
            )
            store.set_chat_session_status(payload.session_id, final_status)
            turn_result["chat_sessions"] = store.list_chat_sessions(limit=40, created_by=auth.speaker_filter)
            turn_result["session_messages"] = store.get_chat_session_detail(payload.session_id)["messages"]  # type: ignore[index]
            return turn_result
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=410, detail="Chat session was deleted while running.") from exc
    finally:
        detail = store.get_chat_session_detail(payload.session_id)
        if detail and detail["session"]["status"] == "running":
            store.set_chat_session_status(payload.session_id, final_status)


@app.post("/api/chat/confirm")
async def confirm_concierge_tool(
    payload: ChatConfirmationPayload,
    auth: AuthContext = Depends(resolve_auth_context),
) -> dict[str, Any]:
    """Resumes a paused ADK Human-in-the-Loop tool call with the user's Approve or Cancel decision."""
    effective_member_name = (
        auth.speaker_name
        if auth.access_mode == "member" or not payload.member_name or payload.member_name in ("Family", "Família")
        else payload.member_name
    )
    effective_member_id = (
        auth.speaker_id
        if auth.access_mode == "member" or not payload.member_id
        else payload.member_id
    )
    existing_sess = store.get_chat_session_detail(payload.session_id)
    if existing_sess is not None:
        owned_sess = store.get_chat_session_detail(payload.session_id, owner_name=effective_member_name)
        if owned_sess is None:
            raise HTTPException(status_code=403, detail="Esta conversa pertence a outro membro da família.")

    decision_label = "✓ Aprovado" if payload.confirmed else "✕ Cancelado"
    store.resolve_message_confirmation(
        payload.session_id,
        payload.confirmation_call_id,
        decision_label,
    )
    confirmation_content = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=payload.confirmation_call_id,
                    name=CONFIRMATION_FC_NAME,
                    response={"confirmed": bool(payload.confirmed)},
                )
            )
        ],
    )
    session_lock = _get_session_lock(payload.session_id)
    final_status = "idle"
    try:
        async with session_lock:
            if store.get_chat_session_detail(payload.session_id) is None:
                raise HTTPException(status_code=410, detail="Chat session was deleted.")
            store.set_chat_session_status(payload.session_id, "running")
            turn_result = await _execute_runner_turn(
                operation_name="concierge.confirm_request",
                user_id=HOUSEHOLD_ADK_USER_ID,
                member_name=effective_member_name,
                session_id=payload.session_id,
                new_message=confirmation_content,
                state_delta={
                    "active_member_name": effective_member_name,
                    "active_member_id": effective_member_id,
                    "already_replied_today": True,
                },
                include_admin_secrets=(auth.access_mode == "admin"),
                speaker_filter=auth.speaker_filter,
            )
            if store.get_chat_session_detail(payload.session_id) is None:
                return turn_result
            final_status = "awaiting_approval" if turn_result["pending_confirmations"] else "idle"
            store.append_chat_message(
                payload.session_id,
                role="assistant",
                content=turn_result["reply"],
                models_used=turn_result["models_used"],
                tool_calls=turn_result["tool_calls"],
                pending_confirmations=turn_result["pending_confirmations"],
            )
            store.set_chat_session_status(payload.session_id, final_status)
            turn_result["chat_sessions"] = store.list_chat_sessions(limit=40, created_by=auth.speaker_filter)
            turn_result["session_messages"] = store.get_chat_session_detail(payload.session_id)["messages"]  # type: ignore[index]
            return turn_result
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=410, detail="Chat session was deleted while running.") from exc
    finally:
        detail = store.get_chat_session_detail(payload.session_id)
        if detail and detail["session"]["status"] == "running":
            store.set_chat_session_status(payload.session_id, final_status)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/manifest.json")
def serve_dynamic_manifest(request: Request, m: Optional[str] = None) -> JSONResponse:
    """Serves PWA manifest, embedding the member's personal invite token into `start_url` when present."""
    manifest_data = json.loads((STATIC_DIR / "manifest.json").read_text(encoding="utf-8"))
    tok = (m or request.cookies.get(MEMBER_COOKIE_NAME) or "").strip()
    if tok and store.get_member_by_invite_token(tok) is not None:
        manifest_data["start_url"] = f"/?m={quote(tok)}"
    return JSONResponse(manifest_data)


@app.get("/")
def serve_index(
    m: Optional[str] = None,
    admin: Optional[str] = None,
) -> FileResponse:
    resp = FileResponse(STATIC_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    tok = (m or "").strip()
    if tok and store.get_member_by_invite_token(tok) is not None:
        resp.set_cookie(
            key=MEMBER_COOKIE_NAME,
            value=tok,
            max_age=COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
        )
    elif admin == "1":
        resp.delete_cookie(key=MEMBER_COOKIE_NAME)
    return resp
