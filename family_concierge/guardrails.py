"""ADK Guardrail, Policy & Self-Evaluation Plugin enforcing input safety, tool policies, and output compliance.

Implements two complementary ADK guardrail mechanisms:
1. `FamilySafetyAndPolicyPlugin(BasePlugin)`:
   - `on_user_message_callback`: Flags prompt-injection / exfiltration patterns in session state.
   - `before_model_callback`: Applies Gemini `SafetySetting` thresholds (`BLOCK_LOW_AND_ABOVE`) and
     short-circuits prompt-injection, system-prompt extraction, or SQL-destruction attacks with a
     deterministic `LlmResponse` refusal.
   - `before_tool_callback`: Enforces deterministic household tool policies (explicit participant tags,
     no fabricated placeholder locations, bounded recurrence counts, no SQL/shell injection payloads).
   - `after_model_callback`: Runs structured output self-evaluation (`evaluate_response_compliance` ->
     `SelfEvalVerdict`) to scrub leaked credentials, strip placeholder locations, and enforce clean formatting.
   - `after_run_callback`: Triggers non-blocking async memory consolidation via `memory_ops`.
2. `build_tool_reflection_plugin()`: Configures ADK's built-in `ReflectAndRetryToolPlugin` for
   concurrent-safe tool self-reflection and automatic error recovery.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.plugins.reflect_retry_tool_plugin import (
    ReflectAndRetryToolPlugin,
)
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from .memory_ops import schedule_async_memory_consolidation
from .observability import emit_structured_log

logger = logging.getLogger(__name__)

# Prompt-injection, jailbreak, secret-exfiltration, and destructive SQL patterns
_PROMPT_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "PROMPT_INJECTION_OVERRIDE",
        re.compile(
            r"(ignore|disregard|bypass|forget)\s+(all\s+)?(previous|prior|above|system)\s+"
            r"(instructions|rules|constitution|constraints|guardrails)",
            re.IGNORECASE,
        ),
    ),
    (
        "SYSTEM_PROMPT_EXFILTRATION",
        re.compile(
            r"(reveal|print|dump|output|show|repeat)\s+(your\s+|the\s+)?"
            r"(exact\s+|full\s+|raw\s+)?(system\s+prompt|constitution|hidden\s+instructions)",
            re.IGNORECASE,
        ),
    ),
    (
        "SECRET_OR_KEY_EXFILTRATION",
        re.compile(
            r"(print|reveal|show|leak|output|dump|read)\s+.*?"
            r"(GEMINI_API_KEY|GOOGLE_API_KEY|passcode_hash|\.env\b|api[_\s-]?key|secret[_\s-]?key)",
            re.IGNORECASE,
        ),
    ),
    (
        "DESTRUCTIVE_SQL_INJECTION",
        re.compile(
            r"\b(DROP\s+TABLE|TRUNCATE\s+TABLE|DELETE\s+FROM\s+calendar_events|ALTER\s+TABLE)\b",
            re.IGNORECASE,
        ),
    ),
)

# Output secret / credential leak patterns scrubbed during `after_model_callback` self-evaluation
_SECRET_LEAK_RE = re.compile(
    r"((?:AIza)[0-9A-Za-z_-]{30,}|AQ\.[0-9A-Za-z._-]{20,}|GOCSPX-[0-9A-Za-z_-]{15,}|ya29\.[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{20,})"
)

_LOCATION_PLACEHOLDER_CLAIM_RE = re.compile(
    r"\bLocation:\s*(TBD|Unknown|To be determined|N/A)\b",
    re.IGNORECASE,
)

_DISALLOWED_PLACEHOLDER_LOCATIONS = {
    "tbd",
    "to be determined",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "a definir",
    "desconhecido",
}

_AMBIGUOUS_PARTICIPANT_VALUES = {
    "",
    "?",
    "unknown",
    "tbd",
    "unsure",
    "indefinido",
    "nao sei",
    "não sei",
}

_DEFAULT_GEMINI_SAFETY_SETTINGS: list[types.SafetySetting] = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
]


class SelfEvalVerdict(BaseModel):
    """Structured self-evaluation verdict produced on every model response before delivery."""

    model_config = ConfigDict(extra="forbid")

    passed: bool = Field(..., description="True when no policy or secret-hygiene violations were detected.")
    violations: list[str] = Field(default_factory=list, description="Rule codes triggered during self-eval.")
    sanitized_text: str = Field(..., description="Compliant response text safe to deliver to the user.")


def evaluate_response_compliance(raw_text: str) -> SelfEvalVerdict:
    """Self-evaluates generated model text against credential-leak, location, and formatting rules."""
    violations: list[str] = []
    cleaned = raw_text

    if _SECRET_LEAK_RE.search(cleaned):
        violations.append("CREDENTIAL_LEAK_REDACTED")
        cleaned = _SECRET_LEAK_RE.sub("[REDACTED_CREDENTIAL]", cleaned)

    if _LOCATION_PLACEHOLDER_CLAIM_RE.search(cleaned):
        violations.append("PLACEHOLDER_LOCATION_REMOVED")
        cleaned = _LOCATION_PLACEHOLDER_CLAIM_RE.sub("Location: (not specified)", cleaned)

    if "\u2014" in cleaned or "\u2013" in cleaned:
        violations.append("TYPOGRAPHY_LONG_DASH_NORMALIZED")
        cleaned = cleaned.replace("\u2014", "-").replace("\u2013", "-")

    return SelfEvalVerdict(
        passed=len(violations) == 0,
        violations=violations,
        sanitized_text=cleaned,
    )


def detect_policy_violation(text: str) -> Optional[str]:
    """Returns the triggered policy rule code if `text` matches an unsafe pattern, else `None`."""
    if not text:
        return None
    for code, pattern in _PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            return code
    return None


def _extract_text_from_content(content: Optional[types.Content]) -> str:
    if not content or not content.parts:
        return ""
    return "\n".join(p.text for p in content.parts if p.text)


class FamilySafetyAndPolicyPlugin(BasePlugin):
    """Application-wide ADK security, policy enforcement, and output self-evaluation plugin."""

    def __init__(self, name: str = "family_safety_and_policy_plugin") -> None:
        super().__init__(name=name)

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> Optional[types.Content]:
        """Inspects raw user input at the start of an invocation and records policy telemetry."""
        raw_text = _extract_text_from_content(user_message)
        violation = detect_policy_violation(raw_text)
        if violation and invocation_context.session is not None:
            invocation_context.session.state["last_guardrail_block"] = violation
            logger.warning("Guardrail flagged user input (%s) on session %s", violation, invocation_context.session.id)
        return None

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        """Applies Gemini safety settings and blocks prompt-injection / exfiltration before LLM call."""
        if llm_request.config is None:
            llm_request.config = types.GenerateContentConfig()
        if not llm_request.config.safety_settings:
            llm_request.config.safety_settings = list(_DEFAULT_GEMINI_SAFETY_SETTINGS)

        for content in reversed(llm_request.contents or []):
            if content.role == "user":
                user_text = _extract_text_from_content(content)
                violation = detect_policy_violation(user_text)
                if violation:
                    callback_context.state["last_guardrail_block"] = violation
                    emit_structured_log(
                        event_type="GUARDRAIL_INTERCEPTION",
                        phase="OUTCOME",
                        severity="WARNING",
                        message=f"Blocked unsafe LLM prompt ({violation})",
                        session_id=callback_context.session.id if callback_context.session else "",
                        invocation_id=callback_context.invocation_id,
                        agent_name=callback_context.agent_name,
                        outcome_status="GUARDRAIL_BLOCKED",
                        payload={"violation_code": violation, "user_excerpt": user_text[:200]},
                    )
                    refusal_text = (
                        "I cannot fulfill instructions that attempt to bypass household safety rules, "
                        "extract internal system prompts or API credentials, or run raw database commands. "
                        "Let me know how I can help with your Family Calendar, Need-To-Do errands, or Want-To-Do list!"
                    )
                    return LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[types.Part.from_text(text=refusal_text)],
                        )
                    )
                break
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict[str, Any]]:
        """Deterministic pre-execution tool policy checks across household mutations."""
        del tool_context

        for arg_name, arg_val in (tool_args or {}).items():
            if isinstance(arg_val, str) and detect_policy_violation(arg_val):
                return {
                    "status": "error",
                    "error_code": "GUARDRAIL_TOOL_ARG_BLOCKED",
                    "message": f"Blocked unsafe payload in tool argument '{arg_name}'.",
                    "recovery_hint": "Provide clean household text without system or SQL directives.",
                }

        if tool.name == "schedule_family_calendar_event":
            raw_participants = str(tool_args.get("participants", "")).strip()
            if raw_participants.lower() in _AMBIGUOUS_PARTICIPANT_VALUES:
                return {
                    "status": "error",
                    "error_code": "GUARDRAIL_AMBIGUOUS_PARTICIPANTS",
                    "message": "Calendar policy requires explicit participant name(s) or 'All'.",
                    "recovery_hint": "Ask the user which family member(s) will attend before calling schedule_family_calendar_event.",
                }

            raw_location = str(tool_args.get("location") or "").strip()
            if raw_location and raw_location.lower() in _DISALLOWED_PLACEHOLDER_LOCATIONS:
                tool_args["location"] = ""

            occurrences = tool_args.get("occurrences")
            if isinstance(occurrences, int) and occurrences > 60:
                return {
                    "status": "error",
                    "error_code": "GUARDRAIL_MAX_OCCURRENCES_EXCEEDED",
                    "message": f"Requested {occurrences} occurrences exceeds the household maximum of 60.",
                    "recovery_hint": "Use occurrences=30 (household standard) or <= 60 for recurring events.",
                }

        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> Optional[LlmResponse]:
        """Runs structured self-evaluation (`SelfEvalVerdict`) on model output before delivery."""
        if not llm_response.content or not llm_response.content.parts:
            return None

        mutated = False
        recorded_violations: list[str] = []
        new_parts: list[types.Part] = []
        for part in llm_response.content.parts:
            if part.text and not part.thought:
                verdict = evaluate_response_compliance(part.text)
                if not verdict.passed:
                    mutated = True
                    recorded_violations.extend(verdict.violations)
                    new_parts.append(types.Part.from_text(text=verdict.sanitized_text))
                    continue
            new_parts.append(part)

        if callback_context is not None and recorded_violations:
            callback_context.state["last_self_eval_remediations"] = recorded_violations

        if mutated:
            return llm_response.model_copy(
                update={
                    "content": types.Content(
                        role=llm_response.content.role or "model",
                        parts=new_parts,
                    )
                }
            )
        return None

    async def after_run_callback(
        self,
        *,
        invocation_context: InvocationContext,
    ) -> None:
        """Schedules non-blocking async memory consolidation at the end of an ADK run."""
        session = invocation_context.session
        if session is None or not session.events:
            return

        latest_user = ""
        latest_reply = ""
        for ev in reversed(session.events):
            if not latest_reply and ev.author != "user" and ev.content and ev.content.parts:
                txt = " ".join(p.text for p in ev.content.parts if p.text and not p.thought).strip()
                if txt:
                    latest_reply = txt
            if not latest_user and ev.author == "user" and ev.content and ev.content.parts:
                txt = " ".join(p.text for p in ev.content.parts if p.text).strip()
                if txt:
                    latest_user = txt
            if latest_user and latest_reply:
                break

        if latest_user:
            speaker = str(session.state.get("active_member_name") or session.user_id or "Family")
            schedule_async_memory_consolidation(
                user_text=latest_user,
                assistant_reply=latest_reply,
                member_name=speaker,
                session_id=session.id,
            )


def build_tool_reflection_plugin() -> ReflectAndRetryToolPlugin:
    """Creates ADK's built-in self-healing reflection plugin for transient tool errors."""
    return ReflectAndRetryToolPlugin(
        name="family_tool_self_reflection_plugin",
        max_retries=2,
        throw_exception_if_retry_exceeded=False,
    )
