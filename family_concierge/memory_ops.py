"""Asynchronous non-blocking memory service and background consolidation for Family Ops Concierge.

Implements two complementary layers required for production AgentOps memory management:
1. `SqliteHouseholdMemoryService(BaseMemoryService)`: An ADK-compliant async memory service
   that offloads SQLite reads/writes via `asyncio.to_thread` so the event loop never blocks.
2. `schedule_async_memory_consolidation(...)`: Spawns background `asyncio.Task` workers after
   each conversational turn (and via ADK `after_run_callback`) to extract durable household
   preferences, routines, and logistics using `gemini-3.5-flash-lite` without delaying UI replies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import json
import logging
import os
import re
from typing import Any, Optional

from google import genai
from google.adk.events.event import Event
from google.adk.memory.base_memory_service import (
    BaseMemoryService,
    SearchMemoryResponse,
)
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from . import store
from .model_router import FAST_MODEL_ID, RETRY_HTTP_OPTIONS
from .pii_redactor import default_pii_redactor
from .secrets import resolve_gemini_api_key

logger = logging.getLogger(__name__)

# Strong references to in-flight background consolidation tasks so GC never drops them mid-flight.
_BACKGROUND_MEMORY_TASKS: set[asyncio.Task[Any]] = set()

# Fast signal check so trivial greetings ('hi', 'oi', 'thanks') skip background LLM calls.
_MEMORY_SIGNAL_PATTERNS = re.compile(
    r"\b("
    r"prefer\w*|always|never|usually|every\s+\w+|routine|allerg\w*|favorite|hate\w*|avoid\w*|remember|"
    r"prefere\w*|preferimos|sempre|nunca|costuma\w*|toda\s+\w+|rotina|alergia\w*|favorit\w*|lembre\w*|gosta\w*"
    r")\b",
    re.IGNORECASE,
)


class ExtractedMemoryItem(BaseModel):
    """Strict schema for a single distilled household memory fact."""

    model_config = ConfigDict(extra="forbid")

    member_name: str = Field(
        default="All",
        description="Family member this fact applies to (e.g. 'Papa', 'Kid1', or 'All').",
    )
    category: str = Field(
        default="preference",
        description="Category: 'preference', 'routine', 'constraint', or 'logistics'.",
    )
    memory_text: str = Field(
        ...,
        min_length=5,
        max_length=240,
        description="Concise, durable third-person household fact worth remembering across sessions.",
    )


class ExtractedMemoryBatch(BaseModel):
    """Batch container returned by background LLM memory extraction."""

    model_config = ConfigDict(extra="forbid")

    memories: list[ExtractedMemoryItem] = Field(default_factory=list)


def _extract_deterministic_heuristics(user_text: str, member_name: str) -> list[ExtractedMemoryItem]:
    """Deterministic fallback extractor for explicit 'remember that...' / preference statements."""
    stripped_header = re.sub(r"^\[Active Family Member:[^\]]*\]\s*", "", user_text.strip())
    cleaned = " ".join(stripped_header.split())
    if len(cleaned) < 12 or not _MEMORY_SIGNAL_PATTERNS.search(cleaned):
        return []

    # Split into sentences and retain concise durable preference/routine clauses
    candidates: list[ExtractedMemoryItem] = []
    for raw_sentence in re.split(r"(?<=[.!?])\s+|\n+", cleaned):
        sent = raw_sentence.strip(" -•\t")
        if 10 <= len(sent) <= 220 and _MEMORY_SIGNAL_PATTERNS.search(sent):
            # Skip pure questions
            if sent.endswith("?"):
                continue
            category = "routine" if re.search(r"\b(every|routine|toda|rotina|sempre|always)\b", sent, re.I) else "preference"
            candidates.append(
                ExtractedMemoryItem(
                    member_name=member_name or "All",
                    category=category,
                    memory_text=f"{member_name or 'Family'}: {sent}",
                )
            )
    return candidates[:3]


async def _extract_memories_with_fast_llm(
    *,
    user_text: str,
    assistant_reply: str,
    member_name: str,
) -> list[ExtractedMemoryItem]:
    """Uses `gemini-3.5-flash-lite` asynchronously to distill PII-scrubbed household facts."""
    scrubbed_user = default_pii_redactor.scrub_text(user_text)
    scrubbed_reply = default_pii_redactor.scrub_text(assistant_reply)
    api_key = resolve_gemini_api_key()
    if not api_key:
        return _extract_deterministic_heuristics(scrubbed_user, member_name)

    prompt = (
        "Extract ONLY durable, long-term household preferences, recurring habits, allergies, "
        "transport logistics, or scheduling constraints stated by the user in this turn. "
        "Never include emails, phone numbers, government IDs, or credentials. "
        "Do NOT extract one-off calendar events already scheduled or ephemeral small talk. "
        "Return JSON matching schema `{\"memories\": [{\"member_name\": str, \"category\": str, \"memory_text\": str}]}`. "
        "If nothing durable was stated, return `{\"memories\": []}`.\n\n"
        f"Active Speaker: {member_name}\n"
        f"User Message: {scrubbed_user}\n"
        f"Assistant Reply: {scrubbed_reply}\n"
    )
    try:
        client = genai.Client(api_key=api_key, http_options=RETRY_HTTP_OPTIONS)
        response = await client.aio.models.generate_content(
            model=FAST_MODEL_ID,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=ExtractedMemoryBatch,
            ),
        )
        raw_json = (response.text or "").strip()
        if not raw_json:
            return []
        parsed = ExtractedMemoryBatch.model_validate(json.loads(raw_json))
        return parsed.memories[:4]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Fast LLM memory extraction fell back to heuristics: %s", exc)
        return _extract_deterministic_heuristics(scrubbed_user, member_name)


async def consolidate_turn_memories_async(
    *,
    user_text: str,
    assistant_reply: str = "",
    member_name: str = "Family",
    session_id: str = "",
    use_llm_extractor: bool = True,
) -> list[dict[str, Any]]:
    """Background coroutine that extracts and persists durable household memories without blocking UI."""
    cleaned_user = (user_text or "").strip()
    if len(cleaned_user) < 10 or not _MEMORY_SIGNAL_PATTERNS.search(cleaned_user):
        return []

    if use_llm_extractor:
        extracted = await _extract_memories_with_fast_llm(
            user_text=cleaned_user,
            assistant_reply=assistant_reply,
            member_name=member_name,
        )
    else:
        extracted = _extract_deterministic_heuristics(cleaned_user, member_name)

    saved_records: list[dict[str, Any]] = []
    for item in extracted:
        safe_mem_text = default_pii_redactor.scrub_text(item.memory_text)
        record = await asyncio.to_thread(
            store.upsert_household_memory,
            safe_mem_text,
            item.category,
            item.member_name,
            session_id,
        )
        saved_records.append(record)
    return saved_records


def schedule_async_memory_consolidation(
    *,
    user_text: str,
    assistant_reply: str = "",
    member_name: str = "Family",
    session_id: str = "",
    use_llm_extractor: bool = True,
) -> Optional[asyncio.Task[list[dict[str, Any]]]]:
    """Fires-and-forgets a non-blocking `asyncio.Task` to consolidate household memories.

    Returns immediately (`0 ms` added to user turn latency) while keeping a strong reference
    in `_BACKGROUND_MEMORY_TASKS` until completion.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    task = loop.create_task(
        consolidate_turn_memories_async(
            user_text=user_text,
            assistant_reply=assistant_reply,
            member_name=member_name,
            session_id=session_id,
            use_llm_extractor=use_llm_extractor,
        ),
        name=f"household-memory-consolidate-{session_id or 'turn'}",
    )
    _BACKGROUND_MEMORY_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_MEMORY_TASKS.discard)
    return task


async def wait_for_pending_memory_tasks(timeout: float = 5.0) -> int:
    """Awaits any in-flight background memory tasks (used in tests and graceful shutdown)."""
    pending = list(_BACKGROUND_MEMORY_TASKS)
    if not pending:
        return 0
    await asyncio.wait(pending, timeout=timeout)
    return len(pending)


def build_recalled_memories_xml(limit: int = 10) -> str:
    """Formats persisted long-term household memories as an XML block for agent context."""
    memories = store.query_household_memories(limit=limit)
    if not memories:
        return (
            "<household_long_term_memory>\n"
            "  No learned cross-session household preferences recorded yet.\n"
            "</household_long_term_memory>"
        )
    lines = ["<household_long_term_memory>"]
    for mem in memories:
        lines.append(
            f"  - [{mem['member_name']} | {mem['category']}] {mem['memory_text']}"
        )
    lines.append("</household_long_term_memory>")
    return "\n".join(lines)


class SqliteHouseholdMemoryService(BaseMemoryService):
    """ADK `BaseMemoryService` backed by non-blocking `asyncio.to_thread` SQLite operations."""

    async def add_session_to_memory(self, session: Session) -> None:
        """Ingests a session's user turns asynchronously into long-term household memory."""
        await self.add_events_to_memory(
            app_name=session.app_name,
            user_id=session.user_id,
            events=session.events,
            session_id=session.id,
        )

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Schedules non-blocking memory extraction over incremental session event deltas."""
        del app_name, custom_metadata
        user_texts: list[str] = []
        assistant_texts: list[str] = []
        for ev in events:
            if not (ev.content and ev.content.parts):
                continue
            chunk = " ".join(p.text for p in ev.content.parts if p.text)
            if not chunk.strip():
                continue
            if ev.author == "user":
                user_texts.append(chunk)
            else:
                assistant_texts.append(chunk)

        if user_texts:
            schedule_async_memory_consolidation(
                user_text="\n".join(user_texts[-2:]),
                assistant_reply="\n".join(assistant_texts[-2:]),
                member_name=user_id or "Family",
                session_id=session_id or "",
            )

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Persists explicit `MemoryEntry` items into SQLite off the event loop."""
        del app_name, custom_metadata
        for entry in memories:
            text_parts = [
                p.text for p in (entry.content.parts or []) if p.text
            ] if entry.content else []
            joined = " ".join(text_parts).strip()
            if not joined:
                continue
            category = str((entry.custom_metadata or {}).get("category", "preference"))
            member = str((entry.custom_metadata or {}).get("member_name", entry.author or user_id or "All"))
            await asyncio.to_thread(
                store.upsert_household_memory,
                joined,
                category,
                member,
                "",
            )

    async def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
    ) -> SearchMemoryResponse:
        """Searches household memories asynchronously without blocking the event loop."""
        del app_name, user_id
        rows = await asyncio.to_thread(store.query_household_memories, query, 12)
        entries = [
            MemoryEntry(
                id=row["memory_id"],
                author=row["member_name"],
                timestamp=row["updated_at"],
                custom_metadata={
                    "category": row["category"],
                    "member_name": row["member_name"],
                    "source_session_id": row["source_session_id"],
                },
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=row["memory_text"])],
                ),
            )
            for row in rows
        ]
        return SearchMemoryResponse(memories=entries)

    async def delete_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memory_id: str | None = None,
    ) -> None:
        """Deletes a household memory item asynchronously by ID."""
        del app_name, user_id
        if memory_id:
            await asyncio.to_thread(store.delete_household_memory, memory_id)


household_memory_service = SqliteHouseholdMemoryService()
