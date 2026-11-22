"""FastAPI backend uniting the Family Setup / Calendar / Lists Web UI with the Google ADK App Runner."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import os
from pathlib import Path
from typing import Any, Optional
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / "family_concierge" / ".env")
load_dotenv(BASE_DIR / ".env")

from family_concierge import store  # noqa: E402
from family_concierge.agent import create_persistent_runner  # noqa: E402
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
    version="0.6.0",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def require_household_auth(
    x_household_passcode: Optional[str] = Header(default=""),
) -> None:
    """Enforces household passcode header when a passcode has been configured."""
    if not store.verify_household_passcode(x_household_passcode or ""):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing household passcode.",
        )


class PasscodeVerifyRequest(BaseModel):
    passcode: str = Field(default="")


class MemberSetupItem(BaseModel):
    member_id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=60)
    role: str = Field(default="Parent")
    avatar: str = Field(default="🙂")
    color: str = Field(default="#1b6b5a")
    email: str = Field(default="")
    routine_notes: str = Field(default="")


class HouseholdSetupPayload(BaseModel):
    household_name: str = Field(..., min_length=2, max_length=80)
    timezone: str = Field(default="America/Sao_Paulo")
    preferred_language: str = Field(default="en")
    sports_music_buffer_min: int = Field(default=30, ge=0, le=120)
    new_passcode: Optional[str] = Field(default=None)
    clear_passcode: bool = Field(default=False)
    members: list[MemberSetupItem] = Field(..., min_length=1)


class CreateEventPayload(BaseModel):
    title: str = Field(..., min_length=2, max_length=140)
    start_iso: str
    end_iso: str
    category: str = Field(default="other")
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


class ChatConfirmationPayload(BaseModel):
    confirmation_call_id: str = Field(..., min_length=2)
    confirmed: bool
    member_id: str = Field(default="mem-parent-1")
    member_name: str = Field(default="Family")
    session_id: str = Field(default="shared-household-session")


class CreateChatSessionPayload(BaseModel):
    session_id: Optional[str] = Field(default=None)
    created_by: str = Field(default="Family")


@app.on_event("startup")
def _on_startup() -> None:
    store.init_db()
    with store._connect() as conn:
        conn.execute("UPDATE chat_sessions SET status = 'idle' WHERE status = 'running'")


@app.get("/api/auth/status")
def auth_status() -> dict[str, Any]:
    bundle = store.get_household_bundle()
    hh = bundle["household"]
    return {
        "requires_passcode": hh["has_passcode"],
        "is_configured": hh["is_configured"],
        "household_name": hh["household_name"],
        "preferred_language": hh["preferred_language"],
    }


@app.post("/api/auth/verify")
def auth_verify(payload: PasscodeVerifyRequest) -> dict[str, Any]:
    ok = store.verify_household_passcode(payload.passcode)
    if not ok:
        raise HTTPException(status_code=401, detail="Incorrect household passcode.")
    return {"status": "ok"}


@app.get("/api/state", dependencies=[Depends(require_household_auth)])
def get_full_state(include_completed: bool = True) -> dict[str, Any]:
    bundle = store.get_household_bundle()
    return {
        "household": bundle["household"],
        "members": bundle["members"],
        "events": store.query_events(),
        "items": store.query_action_items(include_completed=include_completed),
        "memories": store.query_household_memories(limit=20),
        "chat_sessions": store.list_chat_sessions(limit=40),
    }


@app.get("/api/chat/sessions", dependencies=[Depends(require_household_auth)])
def list_chat_sessions_endpoint(limit: int = 40) -> dict[str, Any]:
    return {"sessions": store.list_chat_sessions(limit=limit)}


@app.post("/api/chat/sessions", dependencies=[Depends(require_household_auth)])
def create_chat_session_endpoint(payload: CreateChatSessionPayload) -> dict[str, Any]:
    sid = (payload.session_id or "").strip() or f"fam-session-{uuid.uuid4().hex[:8]}"
    created = store.ensure_chat_session(sid, created_by=payload.created_by, status="idle")
    return {
        "session": created,
        "messages": store.get_chat_session_detail(sid)["messages"],  # type: ignore[index]
        "chat_sessions": store.list_chat_sessions(limit=40),
    }


@app.get("/api/chat/sessions/{session_id}", dependencies=[Depends(require_household_auth)])
def get_chat_session_endpoint(session_id: str) -> dict[str, Any]:
    detail = store.get_chat_session_detail(session_id)
    if detail is None:
        created = store.ensure_chat_session(session_id, status="idle")
        return {"session": created, "messages": []}
    return detail


@app.delete("/api/chat/sessions/{session_id}", dependencies=[Depends(require_household_auth)])
async def delete_chat_session_endpoint(session_id: str) -> dict[str, Any]:
    store.delete_chat_session_record(session_id)
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
        "chat_sessions": store.list_chat_sessions(limit=40),
    }


@app.get("/api/memories", dependencies=[Depends(require_household_auth)])
def get_memories_endpoint(q: Optional[str] = None) -> dict[str, Any]:
    return {"memories": store.query_household_memories(query=q, limit=25)}


@app.get("/api/telemetry", dependencies=[Depends(require_household_auth)])
def get_telemetry_endpoint(limit: int = 40) -> dict[str, Any]:
    """Exposes recent PII-redacted structured JSON INTENT/OUTCOME logs and OpenTelemetry trace spans."""
    return {
        "logs": get_recent_structured_logs(limit=limit),
        "spans": get_recent_trace_spans(limit=limit),
    }


@app.put("/api/household/setup", dependencies=[Depends(require_household_auth)])
def update_household_setup(payload: HouseholdSetupPayload) -> dict[str, Any]:
    saved = store.save_household_setup(
        household_name=payload.household_name,
        timezone_str=payload.timezone,
        preferred_language=payload.preferred_language,
        sports_music_buffer_min=payload.sports_music_buffer_min,
        members=[m.model_dump() for m in payload.members],
        new_passcode=payload.new_passcode,
        clear_passcode=payload.clear_passcode,
    )
    return {
        "status": "success",
        "household": saved["household"],
        "members": saved["members"],
    }


@app.post("/api/events", dependencies=[Depends(require_household_auth)])
def create_event_endpoint(payload: CreateEventPayload) -> dict[str, Any]:
    result = schedule_family_calendar_event(
        title=payload.title,
        start_iso=payload.start_iso,
        end_iso=payload.end_iso,
        participants=payload.participants,
        category=payload.category,  # type: ignore[arg-type]
        location=payload.location,
        recurrence=payload.recurrence,  # type: ignore[arg-type]
        occurrences=payload.occurrences,
        notes=payload.notes,
        allow_overlap=payload.allow_overlap,
        tool_context=None,
    )
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result)

    with store._connect() as conn:
        if result.get("series_id"):
            conn.execute(
                "UPDATE calendar_events SET created_by = ? WHERE series_id = ?",
                (payload.created_by, result["series_id"]),
            )
        else:
            conn.execute(
                "UPDATE calendar_events SET created_by = ? WHERE event_id = ?",
                (payload.created_by, result["event"]["event_id"]),
            )
    result["event"]["created_by"] = payload.created_by
    return result


@app.delete("/api/events/{event_id}", dependencies=[Depends(require_household_auth)])
def remove_event_endpoint(event_id: str, delete_series: bool = False) -> dict[str, Any]:
    deleted = store.delete_event_by_id(event_id, delete_series=delete_series)
    if deleted is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    return {"status": "success", "deleted_event": deleted}


@app.post("/api/items", dependencies=[Depends(require_household_auth)])
def create_item_endpoint(payload: CreateItemPayload) -> dict[str, Any]:
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
            (payload.created_by, item["item_id"]),
        )
    item["created_by"] = payload.created_by
    return result


@app.patch("/api/items/{item_id}", dependencies=[Depends(require_household_auth)])
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

    summary = f"Confirm tool '{tool_name}'"
    warnings: list[str] = []
    if tool_name == "delete_family_calendar_event":
        event_id = str(tool_args.get("event_id") or "")
        delete_series = bool(
            tool_args.get("delete_entire_series") or tool_args.get("delete_series", False)
        )
        matched = next((ev for ev in store.query_events() if ev["event_id"] == event_id), None)
        if matched:
            scope = "entire recurring series" if delete_series and matched.get("series_id") else "single occurrence"
            summary = f"Delete '{matched['title']}' on {matched['start_iso'][:16].replace('T', ' ')} ({scope})"
        else:
            scope = "recurring series" if delete_series else "single occurrence"
            summary = f"Delete calendar event {event_id} ({scope})"
    elif tool_name == "schedule_family_calendar_event":
        raw_title = str(tool_args.get("title") or "Event")
        participants = str(tool_args.get("participants") or "All")
        _, formatted_title = normalize_participants_and_title(raw_title, participants)
        category = str(tool_args.get("category") or "other")
        recurrence = str(tool_args.get("recurrence") or "NONE")
        occurrences = int(
            tool_args.get("occurrences")
            or (1 if recurrence == "NONE" else DEFAULT_RECURRING_OCCURRENCES)
        )
        start_iso_raw = str(tool_args.get("start_iso") or "")
        end_iso_raw = str(tool_args.get("end_iso") or "")
        start_display = start_iso_raw[:16].replace("T", " ")
        scope_label = (
            "one-time event"
            if recurrence == "NONE"
            else f"{recurrence} x {occurrences} occurrences"
        )
        warnings = find_calendar_overlaps(
            start_iso=start_iso_raw,
            end_iso=end_iso_raw,
            category=category,
            recurrence=recurrence,
            occurrences=occurrences,
        )
        summary = f"Schedule '{formatted_title}' starting {start_display} ({scope_label})"

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
        if not reply_text:
            if pending_confirmations:
                reply_text = (
                    "Human-in-the-Loop confirmation required before modifying the shared Family Calendar. "
                    "Please review the action below and click Approve or Cancel."
                )
            else:
                reply_text = "Done!"

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

    bundle = store.get_household_bundle()
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
        "events": store.query_events(),
        "items": store.query_action_items(include_completed=True),
        "memories": store.query_household_memories(limit=20),
        "chat_sessions": store.list_chat_sessions(limit=40),
    }


@app.post("/api/chat", dependencies=[Depends(require_household_auth)])
async def chat_with_concierge(payload: ChatRequestPayload) -> dict[str, Any]:
    raw_user_text = payload.message.strip()
    if not raw_user_text and not payload.attachments:
        raise HTTPException(status_code=400, detail="Provide a message, voice recording, or image attachment.")

    media_parts = _decode_media_parts(payload.attachments)

    bundle = store.get_household_bundle()
    tz_name = bundle["household"].get("timezone") or "America/Sao_Paulo"
    preferred_lang = bundle["household"].get("preferred_language") or "en"
    try:
        now_local = datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        now_local = datetime.now()

    has_audio = any(a.mime_type.lower().startswith("audio/") for a in payload.attachments)
    display_user_text = raw_user_text or (
        "Sent a voice message" if has_audio else "Attached an invitation/image"
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
    store.ensure_chat_session(
        payload.session_id,
        first_prompt=display_user_text,
        created_by=payload.member_name,
        status="running",
    )
    store.append_chat_message(
        payload.session_id,
        role="user",
        content=display_user_text,
        speaker_name=payload.member_name,
        speaker_avatar=payload.member_avatar,
        attachments_meta=attachments_meta,
    )

    contextual_prompt = (
        f"[Active Family Member: {payload.member_name} | "
        f"Current Local Date: {now_local.strftime('%Y-%m-%d (%A) %H:%M')}]\n"
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
                member_name=payload.member_name,
                session_id=payload.session_id,
                new_message=content,
                state_delta={
                    "active_member_name": payload.member_name,
                    "active_member_id": payload.member_id,
                    "routed_tier": route_hint.tier.value,
                },
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
            turn_result["chat_sessions"] = store.list_chat_sessions(limit=40)
            turn_result["session_messages"] = store.get_chat_session_detail(payload.session_id)["messages"]  # type: ignore[index]
            return turn_result
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=410, detail="Chat session was deleted while running.") from exc
    finally:
        detail = store.get_chat_session_detail(payload.session_id)
        if detail and detail["session"]["status"] == "running":
            store.set_chat_session_status(payload.session_id, final_status)


@app.post("/api/chat/confirm", dependencies=[Depends(require_household_auth)])
async def confirm_concierge_tool(payload: ChatConfirmationPayload) -> dict[str, Any]:
    """Resumes a paused ADK Human-in-the-Loop tool call with the user's Approve or Cancel decision."""
    decision_label = f"{'✓ Approved' if payload.confirmed else '✕ Cancelled'} by {payload.member_name}"
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
                member_name=payload.member_name,
                session_id=payload.session_id,
                new_message=confirmation_content,
                state_delta={
                    "active_member_name": payload.member_name,
                    "active_member_id": payload.member_id,
                },
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
            turn_result["chat_sessions"] = store.list_chat_sessions(limit=40)
            turn_result["session_messages"] = store.get_chat_session_detail(payload.session_id)["messages"]  # type: ignore[index]
            return turn_result
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=410, detail="Chat session was deleted while running.") from exc
    finally:
        detail = store.get_chat_session_detail(payload.session_id)
        if detail and detail["session"]["status"] == "running":
            store.set_chat_session_status(payload.session_id, final_status)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
