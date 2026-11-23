"""Custom ADK tools for Family Ops Concierge with strict Pydantic input AND output JSON schemas."""

import calendar
from datetime import datetime, timedelta
import re
from typing import Annotated, Any, Literal, Optional
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.adk.features import FeatureName, override_feature_enabled
from google.adk.tools import FunctionTool, ToolContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import gcal_sync, store

# Activate ADK framework-level Pydantic argument & JSON schema validation on all FunctionTools
override_feature_enabled(FeatureName.JSON_SCHEMA_FOR_FUNC_DECL, True)
override_feature_enabled(FeatureName.FUNCTION_TOOL_ARG_VALIDATION, True)

EventCategory = Literal[
    "school_exam",
    "sports",
    "music",
    "birthday_party",
    "family_outing",
    "errand",
    "other",
]
RecurrencePattern = Literal["NONE", "DAILY", "WEEKLY", "BIWEEKLY", "MONTHLY"]
ActionItemType = Literal["NEED_TO_DO", "WANT_TO_DO"]
ActionItemStatus = Literal["OPEN", "DONE"]
MemberRole = Literal["Parent", "Teen", "Child", "Relative"]

DEFAULT_RECURRING_OCCURRENCES = 30

_WHOLE_FAMILY_ALIASES = {
    "all",
    "whole family",
    "everyone",
    "todos",
    "familia",
    "família",
    "familia toda",
    "família toda",
    "toda a familia",
    "toda a família",
}

_UNCLEAR_PARTICIPANT_TOKENS = {
    "",
    "?",
    "unknown",
    "tbd",
    "unclear",
    "unspecified",
    "a definir",
    "nao sei",
    "não sei",
    "nao informado",
    "não informado",
}

_PLACEHOLDER_LOCATIONS = {
    "unknown",
    "tbd",
    "n/a",
    "na",
    "none",
    "null",
    "unspecified",
    "not specified",
    "not provided",
    "a definir",
    "nao informado",
    "não informado",
    "sem local",
    "desconhecido",
}


# ============================================================================
# STRICT INPUT SCHEMAS (Validate LLM arguments & generate JSON Schema constraints)
# ============================================================================


class HouseholdProfileQueryInput(BaseModel):
    """Strict empty/query input schema for retrieving household profile."""

    model_config = ConfigDict(extra="forbid")


class FamilyMemberInput(BaseModel):
    """Strict input schema for configuring a family member in household setup."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=60, description="Display name of the family member.")
    role: MemberRole = Field(default="Parent", description="Household role: Parent, Teen, Child, or Relative.")
    avatar: str = Field(default="🙂", max_length=8, description="Single emoji avatar representing the member.")
    routine_notes: str = Field(default="", max_length=240, description="Activities, buffers, or routine notes.")


class EventListFilterInput(BaseModel):
    """Strict input schema for filtering scheduled family calendar events."""

    model_config = ConfigDict(extra="forbid")

    date_filter: Optional[str] = Field(default=None, description="Optional YYYY-MM-DD date filter.")
    participant_filter: Optional[str] = Field(default=None, description="Optional participant name filter.")


class CalendarEventInput(BaseModel):
    """Strict input schema for scheduling single or recurring family calendar events."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=2, max_length=140, description="Event title (prefixed with '[Participants]' or '[All]').")
    start_iso: str = Field(..., description="First event start in ISO-8601 format YYYY-MM-DDTHH:MM:SS.")
    end_iso: str = Field(..., description="First event end in ISO-8601 format YYYY-MM-DDTHH:MM:SS.")
    participants: str = Field(..., min_length=1, max_length=100, description="Exact participant name(s) or 'All' for whole family.")
    category: EventCategory = Field(default="other", description="Domain category used to enforce buffer rules.")
    location: Optional[str] = Field(default=None, max_length=160, description="Identified event location/venue, or empty if unknown.")
    recurrence: RecurrencePattern = Field(default="NONE", description="NONE, DAILY, WEEKLY, BIWEEKLY, or MONTHLY.")
    occurrences: int = Field(default=DEFAULT_RECURRING_OCCURRENCES, ge=1, le=60, description="Repetitions when recurring (default 30).")
    notes: Optional[str] = Field(default=None, description="Optional logistics or preparation notes.")
    allow_overlap: bool = Field(
        default=False,
        description="Set True ONLY when the human has already explicitly confirmed saving despite a schedule or buffer overlap.",
    )


class DeleteEventInput(BaseModel):
    """Strict input schema for removing a calendar event or recurring series."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=3, max_length=64, description="Exact event_id to delete.")
    delete_entire_series: bool = Field(default=False, description="True to delete all occurrences in the series.")


class ActionItemFilterInput(BaseModel):
    """Strict input schema for listing Need-To-Do errands and Want-To-Do wishes."""

    model_config = ConfigDict(extra="forbid")

    item_type: Optional[ActionItemType] = Field(default=None, description="Optional NEED_TO_DO or WANT_TO_DO filter.")
    include_completed: bool = Field(default=False, description="Whether to include DONE items.")


class ActionItemInput(BaseModel):
    """Strict input schema for creating or updating Need-To-Do errands and Want-To-Do wishes."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=3, max_length=140, description="Description of the errand or wish-list item.")
    item_type: ActionItemType = Field(..., description="NEED_TO_DO for obligations; WANT_TO_DO for desired plans.")
    deadline_or_closing_date: Optional[str] = Field(
        default=None,
        description="Optional deadline or window-closing date in YYYY-MM-DD format.",
    )
    status: ActionItemStatus = Field(default="OPEN", description="OPEN or DONE.")
    assigned_to: str = Field(default="Anyone", description="Family member responsible or 'Anyone'.")
    item_id: Optional[str] = Field(default=None, description="Existing item_id when updating an item.")


# ============================================================================
# STRICT OUTPUT SCHEMAS (Validate tool return payloads & constrain LLM responses)
# ============================================================================


class GuidedToolErrorOutput(BaseModel):
    """Strict output schema returned when validation or conflict checks fail."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["error"] = "error"
    error_code: str = Field(..., description="Machine-readable error identifier.")
    message: str = Field(..., description="Clear explanation of what failed.")
    recovery_hint: str = Field(..., description="Actionable instruction telling the LLM how to self-correct.")


class HouseholdSettingsSchema(BaseModel):
    """Strict output schema for household configuration metadata."""

    model_config = ConfigDict(extra="forbid")

    id: int
    household_name: str
    timezone: str
    preferred_language: str
    sports_music_buffer_min: int
    is_configured: bool
    has_passcode: bool
    updated_at: str


class FamilyMemberRecordSchema(BaseModel):
    """Strict output schema for a configured family member."""

    model_config = ConfigDict(extra="forbid")

    member_id: str
    name: str
    role: str
    avatar: str
    color: str
    email: str
    routine_notes: str
    created_at: str


class HouseholdProfileOutput(BaseModel):
    """Strict output schema for `get_family_household_profile`."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    current_local_datetime: str
    current_weekday: str
    active_speaker: str
    household: HouseholdSettingsSchema
    members: list[FamilyMemberRecordSchema]


class MemberUpsertOutput(BaseModel):
    """Strict output schema for `upsert_family_member`."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    member: FamilyMemberRecordSchema


class CalendarEventRecordSchema(BaseModel):
    """Strict output schema for a persisted shared calendar event."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    series_id: str = ""
    recurrence: str = "NONE"
    title: str
    start_iso: str
    end_iso: str
    buffer_start_iso: str
    category: str
    participants: str
    location: str = ""
    created_by: str
    notes: str = ""
    created_at: str


class EventListOutput(BaseModel):
    """Strict output schema for `list_family_calendar_events`."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    count: int
    events: list[CalendarEventRecordSchema]


class ScheduleEventOutput(BaseModel):
    """Strict output schema for `schedule_family_calendar_event`."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    occurrences_created: int
    recurrence: RecurrencePattern
    series_id: str
    buffer_applied_minutes: int
    overlap_warnings: list[str] = Field(default_factory=list)
    event: CalendarEventRecordSchema
    last_occurrence_start_iso: str


class DeletedEventSummarySchema(BaseModel):
    """Strict output schema summarizing a deleted calendar event or series."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    series_id: str = ""
    recurrence: str = "NONE"
    title: str
    start_iso: str
    end_iso: str
    buffer_start_iso: str
    category: str
    participants: str
    location: str = ""
    created_by: str
    notes: str = ""
    created_at: str
    deleted_count: int = 1


class DeleteEventOutput(BaseModel):
    """Strict output schema for `delete_family_calendar_event`."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    deleted_event: DeletedEventSummarySchema


class ActionItemRecordSchema(BaseModel):
    """Strict output schema for a Need-To-Do errand or Want-To-Do wish item."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str
    item_type: ActionItemType
    deadline_or_closing_date: Optional[str] = None
    status: ActionItemStatus
    assigned_to: str
    created_by: str
    created_at: str


class ActionItemListOutput(BaseModel):
    """Strict output schema for `list_family_action_items`."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    count: int
    items: list[ActionItemRecordSchema]


class ActionItemUpsertOutput(BaseModel):
    """Strict output schema for `upsert_family_action_item`."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    action: Literal["created", "updated"]
    item: ActionItemRecordSchema


def _guided_error(error_code: str, message: str, recovery_hint: str) -> dict[str, Any]:
    """Builds and validates a strict `GuidedToolErrorOutput` payload."""
    return GuidedToolErrorOutput(
        error_code=error_code,
        message=message,
        recovery_hint=recovery_hint,
    ).model_dump()


def normalize_participants_and_title(raw_title: str, raw_participants: str) -> tuple[str, str]:
    """Normalizes participant label ('All' vs member names) and enforces '[Tag] Title' prefix."""
    cleaned_part = (raw_participants or "").strip().strip("[]").strip()
    if cleaned_part.lower() in _WHOLE_FAMILY_ALIASES:
        normalized_participants = "All"
    else:
        normalized_participants = cleaned_part

    stripped_title = re.sub(r"^\s*\[[^\]]+\]\s*", "", (raw_title or "").strip()).strip()
    formatted_title = f"[{normalized_participants}] {stripped_title}"
    return normalized_participants, formatted_title


def sanitize_location(raw_location: Optional[str]) -> str:
    """Strips fabricated/placeholder location strings so unknown locations stay cleanly blank."""
    if not raw_location:
        return ""
    cleaned = raw_location.strip()
    if cleaned.lower() in _PLACEHOLDER_LOCATIONS:
        return ""
    return cleaned


def _advance_datetime(base_dt: datetime, recurrence: RecurrencePattern, index: int) -> datetime:
    """Computes occurrence timestamp `index` steps forward from `base_dt`."""
    if index == 0 or recurrence == "NONE":
        return base_dt
    if recurrence == "DAILY":
        return base_dt + timedelta(days=index)
    if recurrence == "WEEKLY":
        return base_dt + timedelta(weeks=index)
    if recurrence == "BIWEEKLY":
        return base_dt + timedelta(weeks=2 * index)
    if recurrence == "MONTHLY":
        total_months = (base_dt.month - 1) + index
        year = base_dt.year + total_months // 12
        month = (total_months % 12) + 1
        max_day = calendar.monthrange(year, month)[1]
        return base_dt.replace(year=year, month=month, day=min(base_dt.day, max_day))
    return base_dt


def _active_author(tool_context: Optional[ToolContext], explicit_author: Optional[str] = None) -> str:
    if explicit_author and explicit_author.strip():
        return explicit_author.strip()
    if tool_context is not None and tool_context.state:
        speaker = tool_context.state.get("active_member_name")
        if speaker:
            return str(speaker)
    return "Family"


def get_family_household_profile(
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Retrieves the configured household profile, current local date/weekday, and family member roster.

    Call this tool at the start of planning or setup conversations so you know the exact
    local date/weekday, each family member's name, role, routine notes, and arrival-buffer policy.

    Args:
        tool_context: ADK runtime context providing active speaker metadata.

    Returns:
        A validated `HouseholdProfileOutput` dictionary with `status`, `current_local_datetime`,
        `current_weekday`, `active_speaker`, `household`, and `members`.
    """
    HouseholdProfileQueryInput()
    bundle = store.get_household_bundle()
    tz_name = bundle["household"].get("timezone") or "America/Sao_Paulo"
    try:
        now_local = datetime.now(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        now_local = datetime.now()

    output = HouseholdProfileOutput(
        current_local_datetime=now_local.strftime("%Y-%m-%dT%H:%M:%S"),
        current_weekday=now_local.strftime("%A"),
        active_speaker=_active_author(tool_context),
        household=HouseholdSettingsSchema.model_validate(bundle["household"]),
        members=[FamilyMemberRecordSchema.model_validate(m) for m in bundle["members"]],
    )
    return output.model_dump()


def upsert_family_member(
    name: Annotated[str, Field(description="Display name of the family member.", min_length=2)],
    role: Annotated[MemberRole, Field(description="Role in household: Parent, Teen, Child, or Relative.")] = "Parent",
    avatar: Annotated[str, Field(description="Emoji icon for this member, e.g. '⚽' or '🎸'.")] = "🙂",
    routine_notes: Annotated[
        str,
        Field(description="Recurring activities or constraints (e.g. 'Band rehearsal, soccer')."),
    ] = "",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Adds or updates a family member in the shared household configuration.

    Args:
        name: Display name of the family member.
        role: Household role (`Parent`, `Teen`, `Child`, or `Relative`).
        avatar: Emoji avatar shown on the member switcher.
        routine_notes: Personal activity or scheduling preferences for this member.
        tool_context: ADK runtime context.

    Returns:
        A validated `MemberUpsertOutput` dictionary with `status` and `member`, or a
        `GuidedToolErrorOutput` dictionary with `recovery_hint`.
    """
    try:
        validated = FamilyMemberInput(name=name, role=role, avatar=avatar, routine_notes=routine_notes)
    except ValidationError as exc:
        return _guided_error(
            error_code="INVALID_MEMBER_INPUT",
            message=f"Invalid family member details: {exc}",
            recovery_hint="Provide a name (2-60 chars) and role in ['Parent', 'Teen', 'Child', 'Relative'].",
        )

    palette = {"Parent": "#1b6b5a", "Teen": "#8c5319", "Child": "#2b5b84", "Relative": "#7a3e65"}
    saved = store.upsert_member_row(
        name=validated.name,
        role=validated.role,
        avatar=validated.avatar,
        color=palette.get(validated.role, "#1b6b5a"),
        routine_notes=validated.routine_notes,
    )
    return MemberUpsertOutput(member=FamilyMemberRecordSchema.model_validate(saved)).model_dump()


def list_family_calendar_events(
    date_filter: Annotated[
        Optional[str],
        Field(description="Optional date prefix in YYYY-MM-DD format to filter a single day."),
    ] = None,
    participant_filter: Annotated[
        Optional[str],
        Field(description="Optional family member name to filter events for a person (includes '[All]' events)."),
    ] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Lists scheduled events on the shared Family Calendar, including locations, recurrence, and buffers.

    Call this tool before proposing new time slots or answering questions about
    family availability, school exams, sports matches, or weekend plans.

    Args:
        date_filter: Optional `YYYY-MM-DD` date string to inspect a specific day.
        participant_filter: Optional family member name to filter relevant events.
        tool_context: ADK runtime context.

    Returns:
        A validated `EventListOutput` dictionary with `status`, `count`, and chronologically sorted `events`.
    """
    try:
        validated_filter = EventListFilterInput(
            date_filter=date_filter,
            participant_filter=participant_filter,
        )
    except ValidationError as exc:
        return _guided_error(
            error_code="INVALID_EVENT_FILTER",
            message=f"Invalid calendar filter input: {exc}",
            recovery_hint="Pass optional date_filter ('YYYY-MM-DD') and participant_filter as plain strings.",
        )
    if validated_filter.date_filter:
        try:
            datetime.strptime(validated_filter.date_filter, "%Y-%m-%d")
        except ValueError:
            return _guided_error(
                error_code="INVALID_DATE_FILTER",
                message=f"Invalid date_filter '{validated_filter.date_filter}'.",
                recovery_hint="Pass date_filter in strict YYYY-MM-DD format (e.g. '2026-12-05') or omit it.",
            )

    events = gcal_sync.query_active_calendar_events(
        date_filter=validated_filter.date_filter,
        participant_filter=validated_filter.participant_filter,
    )
    return EventListOutput(
        count=len(events),
        events=[CalendarEventRecordSchema.model_validate(e) for e in events],
    ).model_dump()


def schedule_family_calendar_event(
    title: Annotated[
        str,
        Field(
            description=(
                "Event title prefixed with participant(s) in brackets, e.g. "
                "'[Kid1] Birthday Party' or '[All] Sunday Family Lunch'."
            ),
            min_length=2,
        ),
    ],
    start_iso: Annotated[str, Field(description="First occurrence start in ISO-8601 format YYYY-MM-DDTHH:MM:SS.")],
    end_iso: Annotated[str, Field(description="First occurrence end in ISO-8601 format YYYY-MM-DDTHH:MM:SS.")],
    participants: Annotated[
        str,
        Field(
            description=(
                "Confirmed participant name(s) (e.g. 'Kid1' or 'Papa, Mama') or 'All' "
                "if the whole family participates. Do NOT guess: ask the user first if unsure."
            )
        ),
    ],
    category: Annotated[
        EventCategory,
        Field(description="Event category: school_exam, sports, music, birthday_party, family_outing, errand, other."),
    ] = "other",
    location: Annotated[
        Optional[str],
        Field(
            description=(
                "Identified event venue or address ONLY if mentioned by the user. "
                "If unknown, leave None/empty - NEVER invent or put 'TBD'/'Unknown'."
            )
        ),
    ] = None,
    recurrence: Annotated[
        RecurrencePattern,
        Field(
            description=(
                "Recurrence rule: 'NONE' for one-time events, or 'DAILY', 'WEEKLY', "
                "'BIWEEKLY', 'MONTHLY' when the user indicates a repeating schedule (e.g. 'every Tuesday')."
            )
        ),
    ] = "NONE",
    occurrences: Annotated[
        int,
        Field(
            description=(
                "Number of repetitions to add when recurrence is not 'NONE'. "
                "Always default to 30 repetitions for recurring events."
            ),
            ge=1,
            le=60,
        ),
    ] = DEFAULT_RECURRING_OCCURRENCES,
    notes: Annotated[Optional[str], Field(description="Optional preparation or reminder notes.")] = None,
    allow_overlap: Annotated[
        bool,
        Field(
            description=(
                "Set True ONLY if you already warned the user about a time or 30-min buffer overlap "
                "and the user explicitly confirmed adding the event anyway. Leave False initially so "
                "the user is warned and asked for confirmation."
            )
        ),
    ] = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Validates household rules and schedules single or recurring (30x) events on the Family Calendar.

    Enforces:
    1. Mandatory bracketed participant prefix on `title` (`[Name] ...` or `[All] ...`).
    2. Non-fabricated `location` (leaves blank when location is not stated).
    3. Automatic generation of 30 repetitions whenever `recurrence != 'NONE'`.
    4. Non-blocking warning + human confirmation for overlapping events or 30-minute sports/music arrival buffers.

    Args:
        title: Event title (automatically normalized to `[<participants>] <title>` or `[All] <title>`).
        start_iso: First event start timestamp in `YYYY-MM-DDTHH:MM:SS` format.
        end_iso: First event end timestamp in `YYYY-MM-DDTHH:MM:SS` format.
        participants: Confirmed family member name(s) attending, or `'All'` for the whole family.
        category: Domain category controlling automatic buffer calculation.
        location: Venue or address explicitly identified from user context; omit if unknown.
        recurrence: `'NONE'`, `'DAILY'`, `'WEEKLY'`, `'BIWEEKLY'`, or `'MONTHLY'`.
        occurrences: Number of occurrences when `recurrence != 'NONE'` (defaults to 30).
        notes: Optional preparation details.
        allow_overlap: True when the human has already accepted an overlap or arrival-buffer warning.
        tool_context: ADK runtime context providing active speaker and HITL confirmation state.

    Returns:
        A validated `ScheduleEventOutput` dictionary on success (including any accepted `overlap_warnings`),
        or `GuidedToolErrorOutput` with `recovery_hint` when input validation fails or overlap confirmation is needed.
    """
    raw_part_check = (participants or "").strip().strip("[]").strip().lower()
    if raw_part_check in _UNCLEAR_PARTICIPANT_TOKENS:
        return _guided_error(
            error_code="PARTICIPANTS_UNCLEAR",
            message="Participants were not specified or were marked unknown.",
            recovery_hint=(
                "Ask the user which family member(s) will participate in this event "
                "(or whether it is for the whole family '[All]') before calling schedule_family_calendar_event."
            ),
        )

    try:
        validated = CalendarEventInput(
            title=title,
            start_iso=start_iso,
            end_iso=end_iso,
            participants=participants,
            category=category,
            location=location,
            recurrence=recurrence,
            occurrences=occurrences,
            notes=notes,
            allow_overlap=allow_overlap,
        )
        first_start = datetime.fromisoformat(validated.start_iso)
        first_end = datetime.fromisoformat(validated.end_iso)
    except (ValidationError, ValueError) as exc:
        return _guided_error(
            error_code="INVALID_EVENT_PARAMETERS",
            message=f"Could not parse event parameters: {exc}",
            recovery_hint=(
                "Provide start_iso and end_iso in strict ISO-8601 format 'YYYY-MM-DDTHH:MM:SS' "
                "(for example '2026-11-24T18:00:00') and include confirmed participants."
            ),
        )

    if first_end <= first_start:
        return _guided_error(
            error_code="END_BEFORE_START",
            message=f"end_iso ({validated.end_iso}) must be after start_iso ({validated.start_iso}).",
            recovery_hint="Adjust end_iso so it is strictly later than start_iso and retry.",
        )

    normalized_participants, bracketed_title = normalize_participants_and_title(
        validated.title, validated.participants
    )
    cleaned_location = sanitize_location(validated.location)

    household = store.get_household_bundle()["household"]
    configured_buffer = int(household.get("sports_music_buffer_min", 30))
    buffer_minutes = configured_buffer if validated.category in ("sports", "music") else 0
    duration = first_end - first_start

    total_count = 1 if validated.recurrence == "NONE" else validated.occurrences
    series_id = f"ser-{uuid.uuid4().hex[:8]}" if total_count > 1 else ""
    author = _active_author(tool_context)

    overlap_warnings = find_calendar_overlaps(
        start_iso=validated.start_iso,
        end_iso=validated.end_iso,
        category=validated.category,
        recurrence=validated.recurrence,
        occurrences=total_count,
    )
    hitl_approved = bool(
        tool_context is not None
        and getattr(tool_context, "tool_confirmation", None) is not None
        and tool_context.tool_confirmation.confirmed
    )
    effective_allow_overlap = bool(validated.allow_overlap or hitl_approved)

    if overlap_warnings and not effective_allow_overlap:
        return _guided_error(
            error_code="OVERLAP_CONFIRMATION_REQUIRED",
            message="Schedule or 30-min arrival-buffer overlap detected: " + " | ".join(overlap_warnings),
            recovery_hint=(
                "Do NOT forbid this entry! Overlaps are allowed on a family calendar when confirmed. "
                "Warn the user clearly about the overlap or 30-min arrival-buffer notice above and ask if "
                "they want to schedule it anyway. If the user confirms, call schedule_family_calendar_event "
                "again with allow_overlap=True."
            ),
        )

    if gcal_sync.is_gcal_sync_active():
        first_buf_iso = (first_start - timedelta(minutes=buffer_minutes)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            gcal_record, gcal_series_id, gcal_last_start = gcal_sync.insert_gcal_event(
                title=bracketed_title,
                start_iso=first_start.strftime("%Y-%m-%dT%H:%M:%S"),
                end_iso=first_end.strftime("%Y-%m-%dT%H:%M:%S"),
                buffer_start_iso=first_buf_iso,
                buffer_minutes=buffer_minutes,
                category=validated.category,
                participants=normalized_participants,
                location=cleaned_location,
                recurrence=validated.recurrence,
                occurrences=total_count,
                created_by=author,
                notes=validated.notes or "",
            )
        except Exception as exc:  # noqa: BLE001
            return _guided_error(
                error_code="GOOGLE_CALENDAR_API_ERROR",
                message=f"Failed to write event to Google Calendar: {exc}",
                recovery_hint=(
                    "Check Family Setup -> Google Calendar Integration to confirm the personal Google account "
                    "is connected and has write permission on the selected shared calendar."
                ),
            )
        return ScheduleEventOutput(
            occurrences_created=total_count,
            recurrence=validated.recurrence,
            series_id=gcal_series_id,
            buffer_applied_minutes=buffer_minutes,
            overlap_warnings=overlap_warnings,
            event=CalendarEventRecordSchema.model_validate(gcal_record),
            last_occurrence_start_iso=gcal_last_start,
        ).model_dump()

    to_insert: list[dict[str, Any]] = []
    for idx in range(total_count):
        occ_start = _advance_datetime(first_start, validated.recurrence, idx)
        occ_end = occ_start + duration
        occ_effective_start = occ_start - timedelta(minutes=buffer_minutes)
        to_insert.append(
            {
                "series_id": series_id,
                "recurrence": validated.recurrence,
                "title": bracketed_title,
                "start_iso": occ_start.isoformat(),
                "end_iso": occ_end.isoformat(),
                "buffer_start_iso": occ_effective_start.isoformat(),
                "category": validated.category,
                "participants": normalized_participants,
                "location": cleaned_location,
                "created_by": author,
                "notes": validated.notes or "",
            }
        )

    created_records = store.insert_events_batch(to_insert)
    return ScheduleEventOutput(
        occurrences_created=len(created_records),
        recurrence=validated.recurrence,
        series_id=series_id,
        buffer_applied_minutes=buffer_minutes,
        overlap_warnings=overlap_warnings,
        event=CalendarEventRecordSchema.model_validate(created_records[0]),
        last_occurrence_start_iso=created_records[-1]["start_iso"],
    ).model_dump()


def delete_family_calendar_event(
    event_id: Annotated[str, Field(description="Exact event_id from list_family_calendar_events to cancel.")],
    delete_entire_series: Annotated[
        bool,
        Field(description="Set True to delete all occurrences of a recurring event series."),
    ] = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Removes a single scheduled event or its entire recurring series from the Family Calendar.

    Args:
        event_id: Identifier of the event to delete (retrieve via `list_family_calendar_events`).
        delete_entire_series: When True and the event belongs to a recurring series, removes all occurrences.
        tool_context: ADK runtime context.

    Returns:
        A validated `DeleteEventOutput` dictionary on success, or `GuidedToolErrorOutput` with `recovery_hint`.
    """
    try:
        validated = DeleteEventInput(
            event_id=event_id.strip(),
            delete_entire_series=delete_entire_series,
        )
    except ValidationError as exc:
        return _guided_error(
            error_code="INVALID_EVENT_ID",
            message=f"Invalid delete parameters: {exc}",
            recovery_hint="Call list_family_calendar_events first to obtain a valid event_id.",
        )

    if gcal_sync.is_gcal_sync_active():
        try:
            removed = gcal_sync.delete_gcal_event(
                validated.event_id,
                delete_series=validated.delete_entire_series,
            )
        except Exception as exc:  # noqa: BLE001
            return _guided_error(
                error_code="GOOGLE_CALENDAR_API_ERROR",
                message=f"Failed to delete event from Google Calendar: {exc}",
                recovery_hint="Call list_family_calendar_events first to verify the active Google Calendar event_id.",
            )
    else:
        removed = store.delete_event_by_id(validated.event_id, delete_series=validated.delete_entire_series)
    if removed is None:
        return _guided_error(
            error_code="EVENT_ID_NOT_FOUND",
            message=f"No calendar event found with event_id '{validated.event_id}'.",
            recovery_hint="Call list_family_calendar_events first to obtain the exact event_id before deleting.",
        )
    return DeleteEventOutput(
        deleted_event=DeletedEventSummarySchema.model_validate(removed)
    ).model_dump()


def list_family_action_items(
    item_type: Annotated[
        Optional[ActionItemType],
        Field(description="Optional filter: 'NEED_TO_DO' for errands or 'WANT_TO_DO' for wish-list plans."),
    ] = None,
    include_completed: Annotated[bool, Field(description="Set True to include DONE items.")] = False,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Retrieves shared Need-To-Do errands and Want-To-Do wish-list items with deadlines.

    Args:
        item_type: Optional filter (`NEED_TO_DO` or `WANT_TO_DO`).
        include_completed: Whether to return items already marked `DONE`.
        tool_context: ADK runtime context.

    Returns:
        A validated `ActionItemListOutput` dictionary containing `status`, `count`, and matching `items`.
    """
    try:
        validated = ActionItemFilterInput(item_type=item_type, include_completed=include_completed)
    except ValidationError as exc:
        return _guided_error(
            error_code="INVALID_ITEM_FILTER",
            message=f"Invalid action item filter: {exc}",
            recovery_hint="Use item_type='NEED_TO_DO' or 'WANT_TO_DO', or omit item_type.",
        )

    items = store.query_action_items(
        item_type=validated.item_type,
        include_completed=validated.include_completed,
    )
    return ActionItemListOutput(
        count=len(items),
        items=[ActionItemRecordSchema.model_validate(i) for i in items],
    ).model_dump()


def upsert_family_action_item(
    title: Annotated[str, Field(description="Description of the errand or wish-list item.", min_length=3)],
    item_type: Annotated[
        ActionItemType,
        Field(description="'NEED_TO_DO' for required errands/obligations; 'WANT_TO_DO' for desired family wishes."),
    ],
    deadline_or_closing_date: Annotated[
        Optional[str],
        Field(description="Optional deadline or cinema/exhibit closing date in YYYY-MM-DD format."),
    ] = None,
    status: Annotated[ActionItemStatus, Field(description="'OPEN' or 'DONE'.")] = "OPEN",
    assigned_to: Annotated[str, Field(description="Family member responsible or 'Anyone'.")] = "Anyone",
    item_id: Annotated[Optional[str], Field(description="Optional existing item_id to update.")] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Creates or updates a shared Need-To-Do errand or Want-To-Do wish-list item.

    Args:
        title: Clear description of the errand or wish-list activity.
        item_type: Strictly `NEED_TO_DO` (errands) or `WANT_TO_DO` (wish-list).
        deadline_or_closing_date: Optional `YYYY-MM-DD` date when the errand is due or wish expires.
        status: `OPEN` or `DONE`.
        assigned_to: Family member responsible, or `'Anyone'`.
        item_id: Existing `item_id` if marking an existing item `DONE` or updating it.
        tool_context: ADK runtime context providing active speaker metadata.

    Returns:
        A validated `ActionItemUpsertOutput` dictionary with `status` and `item`, or
        `GuidedToolErrorOutput` with `recovery_hint`.
    """
    try:
        validated = ActionItemInput(
            title=title,
            item_type=item_type,
            deadline_or_closing_date=deadline_or_closing_date,
            status=status,
            assigned_to=assigned_to,
            item_id=item_id,
        )
        if validated.deadline_or_closing_date:
            datetime.strptime(validated.deadline_or_closing_date, "%Y-%m-%d")
    except (ValidationError, ValueError) as exc:
        return _guided_error(
            error_code="INVALID_ACTION_ITEM",
            message=f"Invalid action item input: {exc}",
            recovery_hint=(
                "Ensure item_type is 'NEED_TO_DO' or 'WANT_TO_DO' and deadline_or_closing_date "
                "uses strict 'YYYY-MM-DD' format (e.g. '2026-11-30')."
            ),
        )

    try:
        action, saved = store.upsert_action_item_row(
            title=validated.title,
            item_type=validated.item_type,
            deadline_or_closing_date=validated.deadline_or_closing_date,
            status=validated.status,
            assigned_to=validated.assigned_to,
            created_by=_active_author(tool_context),
            item_id=validated.item_id,
        )
    except KeyError:
        return _guided_error(
            error_code="ITEM_ID_NOT_FOUND",
            message=f"No action item found with item_id '{validated.item_id}'.",
            recovery_hint="Call list_family_action_items first to retrieve valid item_id values, or omit item_id to create a new item.",
        )

    return ActionItemUpsertOutput(
        action=action,  # type: ignore[arg-type]
        item=ActionItemRecordSchema.model_validate(saved),
    ).model_dump()


# Registry of compiled Input & Output JSON Schemas across all tools for static & runtime inspection
TOOL_INPUT_OUTPUT_JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_family_household_profile": {
        "input_schema": HouseholdProfileQueryInput.model_json_schema(),
        "output_schema": HouseholdProfileOutput.model_json_schema(),
        "error_schema": GuidedToolErrorOutput.model_json_schema(),
    },
    "upsert_family_member": {
        "input_schema": FamilyMemberInput.model_json_schema(),
        "output_schema": MemberUpsertOutput.model_json_schema(),
        "error_schema": GuidedToolErrorOutput.model_json_schema(),
    },
    "list_family_calendar_events": {
        "input_schema": EventListFilterInput.model_json_schema(),
        "output_schema": EventListOutput.model_json_schema(),
        "error_schema": GuidedToolErrorOutput.model_json_schema(),
    },
    "schedule_family_calendar_event": {
        "input_schema": CalendarEventInput.model_json_schema(),
        "output_schema": ScheduleEventOutput.model_json_schema(),
        "error_schema": GuidedToolErrorOutput.model_json_schema(),
    },
    "delete_family_calendar_event": {
        "input_schema": DeleteEventInput.model_json_schema(),
        "output_schema": DeleteEventOutput.model_json_schema(),
        "error_schema": GuidedToolErrorOutput.model_json_schema(),
    },
    "list_family_action_items": {
        "input_schema": ActionItemFilterInput.model_json_schema(),
        "output_schema": ActionItemListOutput.model_json_schema(),
        "error_schema": GuidedToolErrorOutput.model_json_schema(),
    },
    "upsert_family_action_item": {
        "input_schema": ActionItemInput.model_json_schema(),
        "output_schema": ActionItemUpsertOutput.model_json_schema(),
        "error_schema": GuidedToolErrorOutput.model_json_schema(),
    },
}

def find_calendar_overlaps(
    *,
    start_iso: str,
    end_iso: str,
    category: str = "other",
    recurrence: str = "NONE",
    occurrences: int = 1,
) -> list[str]:
    """Detects direct schedule overlaps and 30-min arrival-buffer overlaps as non-blocking warnings."""
    try:
        first_start = datetime.fromisoformat(start_iso)
        first_end = datetime.fromisoformat(end_iso)
    except ValueError:
        return []
    if first_end <= first_start:
        return []

    household = store.get_household_bundle()["household"]
    configured_buffer = int(household.get("sports_music_buffer_min", 30))
    buffer_minutes = configured_buffer if category in ("sports", "music") else 0
    duration = first_end - first_start
    rec_mode: RecurrencePattern = recurrence if recurrence in ("NONE", "DAILY", "WEEKLY", "BIWEEKLY", "MONTHLY") else "NONE"  # type: ignore[assignment]
    total_count = 1 if rec_mode == "NONE" else max(1, min(int(occurrences or 1), 60))

    existing_by_day: dict[str, list[dict[str, Any]]] = {}
    for ev in gcal_sync.query_active_calendar_events():
        existing_by_day.setdefault(ev["start_iso"][:10], []).append(ev)

    warnings: list[str] = []
    for idx in range(total_count):
        occ_start = _advance_datetime(first_start, rec_mode, idx)
        occ_end = occ_start + duration
        occ_effective_start = occ_start - timedelta(minutes=buffer_minutes)
        day_key = occ_start.date().isoformat()

        for existing in existing_by_day.get(day_key, []):
            ex_raw_start = datetime.fromisoformat(existing["start_iso"])
            ex_buf_start = datetime.fromisoformat(existing.get("buffer_start_iso", existing["start_iso"]))
            ex_end = datetime.fromisoformat(existing["end_iso"])

            if occ_start < ex_end and occ_end > ex_raw_start:
                warnings.append(
                    f"Direct time overlap on {day_key} ({occ_start.strftime('%H:%M')}-{occ_end.strftime('%H:%M')}) "
                    f"with '{existing['title']}' ({ex_raw_start.strftime('%H:%M')}-{ex_end.strftime('%H:%M')})"
                )
            elif occ_effective_start < ex_end and occ_end > ex_buf_start:
                warnings.append(
                    f"{configured_buffer}-min arrival-buffer overlap on {day_key} "
                    f"(buffer window {occ_effective_start.strftime('%H:%M')}-{occ_end.strftime('%H:%M')}) "
                    f"with '{existing['title']}' ({ex_raw_start.strftime('%H:%M')}-{ex_end.strftime('%H:%M')}, "
                    f"buffer {ex_buf_start.strftime('%H:%M')})"
                )

    if len(warnings) <= 3:
        return warnings
    extra = len(warnings) - 3
    return [*warnings[:3], f"(+{extra} additional recurring date overlap(s))"]


def should_confirm_schedule_event(
    start_iso: str = "",
    end_iso: str = "",
    category: EventCategory = "other",
    recurrence: RecurrencePattern = "NONE",
    occurrences: int = 1,
    allow_overlap: bool = False,
    **kwargs: Any,
) -> bool:
    """Requires Human-in-the-Loop confirmation on recurring series OR unconfirmed schedule/buffer overlaps."""
    del kwargs
    total_count = 1 if recurrence == "NONE" else int(occurrences or 1)
    if recurrence != "NONE" and total_count >= 5:
        return True
    if not allow_overlap and start_iso and end_iso:
        overlaps = find_calendar_overlaps(
            start_iso=start_iso,
            end_iso=end_iso,
            category=category,
            recurrence=recurrence,
            occurrences=total_count,
        )
        if overlaps:
            return True
    return False


FAMILY_CONCIERGE_TOOLS = [
    FunctionTool(get_family_household_profile),
    FunctionTool(upsert_family_member),
    FunctionTool(list_family_calendar_events),
    FunctionTool(
        schedule_family_calendar_event,
        require_confirmation=should_confirm_schedule_event,
    ),
    FunctionTool(
        delete_family_calendar_event,
        require_confirmation=True,
    ),
    FunctionTool(list_family_action_items),
    FunctionTool(upsert_family_action_item),
]
