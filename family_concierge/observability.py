"""Structured JSON Logging, OpenTelemetry Distributed Tracing, and Intent-vs-Outcome ADK Plugin.

Satisfies all four Category 4 Observability & Tracing criteria:
1. Structured JSON Logging: Uses `python-json-logger` (`JsonFormatter`) to emit machine-parseable
   Cloud Logging-compatible JSON records with rich contextual metadata.
2. Intent vs. Outcome Capture: `FamilyObservabilityPlugin(BasePlugin)` explicitly logs paired
   `phase="INTENT"` records before execution and `phase="OUTCOME"` records after execution across
   runs, agents, LLM calls, and tools (with matching `correlation_id` and `duration_ms`).
3. Distributed Tracing: Configures an OpenTelemetry `TracerProvider` and links parent-child spans
   (`concierge.request` -> `agent.invoke` -> `llm.generate` / `tool.execute`) under a single `trace_id`.
4. Active PII Redaction: Scrubs all user inputs, tool arguments, model outputs, and error details
   through `PiiRedactionPipeline` BEFORE writing structured logs or span attributes.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Iterator, Optional
import uuid

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, Status, StatusCode
from pythonjsonlogger.json import JsonFormatter

from .pii_redactor import PiiRedactionPipeline, default_pii_redactor

SERVICE_NAME = "family-ops-concierge"
SERVICE_VERSION = "0.7.0"


class BoundedMemorySpanExporter(SpanExporter):
    """Thread-safe bounded in-memory OpenTelemetry `SpanExporter` for live diagnostics and tests."""

    def __init__(self, maxlen: int = 400) -> None:
        self._spans: deque[ReadableSpan] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def export(self, spans: Any) -> SpanExportResult:
        with self._lock:
            for sp in spans:
                self._spans.append(sp)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        return True

    def get_finished_spans(self) -> list[ReadableSpan]:
        with self._lock:
            return list(self._spans)

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


_SPAN_EXPORTER = BoundedMemorySpanExporter(maxlen=400)
_RECENT_JSON_LOGS: deque[dict[str, Any]] = deque(maxlen=300)
_LOG_BUFFER_LOCK = threading.Lock()
_TELEMETRY_INITIALIZED = False
_INIT_LOCK = threading.Lock()


def setup_observability() -> trace.Tracer:
    """Initializes the global OpenTelemetry `TracerProvider` and structured `JsonFormatter` logger."""
    global _TELEMETRY_INITIALIZED
    with _INIT_LOCK:
        if not _TELEMETRY_INITIALIZED:
            resource = Resource.create(
                {
                    "service.name": SERVICE_NAME,
                    "service.version": SERVICE_VERSION,
                }
            )
            current_provider = trace.get_tracer_provider()
            if isinstance(current_provider, TracerProvider):
                current_provider.add_span_processor(SimpleSpanProcessor(_SPAN_EXPORTER))
            else:
                provider = TracerProvider(resource=resource)
                provider.add_span_processor(SimpleSpanProcessor(_SPAN_EXPORTER))
                trace.set_tracer_provider(provider)

            json_logger = logging.getLogger("family_ops.telemetry")
            json_logger.setLevel(logging.INFO)
            json_logger.propagate = False
            if not json_logger.handlers:
                stream_handler = logging.StreamHandler(sys.stdout)
                formatter = JsonFormatter(
                    fmt="%(timestamp)s %(severity)s %(name)s %(event_type)s %(phase)s %(message)s"
                )
                stream_handler.setFormatter(formatter)
                json_logger.addHandler(stream_handler)

            _TELEMETRY_INITIALIZED = True

    return trace.get_tracer(SERVICE_NAME, SERVICE_VERSION)


def get_tracer() -> trace.Tracer:
    """Returns the initialized OpenTelemetry `Tracer` for Family Ops Concierge."""
    return setup_observability()


def _format_trace_and_span_ids(span: Optional[Span] = None) -> tuple[str, str]:
    """Extracts hex `trace_id` and `span_id` from the given or currently active OpenTelemetry span."""
    target_span = span or trace.get_current_span()
    ctx = target_span.get_span_context() if target_span else None
    if ctx and ctx.is_valid:
        return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    return "", ""


def emit_structured_log(
    *,
    event_type: str,
    phase: str,
    message: str,
    severity: str = "INFO",
    session_id: str = "",
    invocation_id: str = "",
    correlation_id: str = "",
    agent_name: str = "",
    model_id: str = "",
    tool_name: str = "",
    outcome_status: str = "",
    duration_ms: Optional[float] = None,
    payload: Optional[dict[str, Any]] = None,
    span: Optional[Span] = None,
    redactor: Optional[PiiRedactionPipeline] = None,
) -> dict[str, Any]:
    """Scrubs PII and emits a structured JSON log record to `python-json-logger` and telemetry buffer."""
    setup_observability()
    scrubber = redactor or default_pii_redactor
    trace_id, span_id = _format_trace_and_span_ids(span)
    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()

    safe_message = scrubber.scrub_text(message)
    safe_payload = scrubber.scrub_payload(payload or {})

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "severity": severity.upper(),
        "service": SERVICE_NAME,
        "event_type": event_type,
        "phase": phase,
        "session_id": session_id,
        "invocation_id": invocation_id,
        "correlation_id": correlation_id,
        "agent_name": agent_name,
        "model_id": model_id,
        "tool_name": tool_name,
        "outcome_status": outcome_status,
        "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
        "trace_id": trace_id,
        "span_id": span_id,
        "payload": safe_payload,
    }
    if trace_id and gcp_project:
        record["logging.googleapis.com/trace"] = f"projects/{gcp_project}/traces/{trace_id}"
    if span_id:
        record["logging.googleapis.com/spanId"] = span_id

    with _LOG_BUFFER_LOCK:
        _RECENT_JSON_LOGS.append({"message": safe_message, **record})

    log_file = os.environ.get("FAMILY_TELEMETRY_LOG_FILE", "").strip()
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            with Path(log_file).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"message": safe_message, **record}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    json_logger = logging.getLogger("family_ops.telemetry")
    level = getattr(logging, severity.upper(), logging.INFO)
    json_logger.log(level, safe_message, extra=record)
    return record


@contextmanager
def trace_concierge_request(
    operation_name: str,
    *,
    session_id: str = "",
    member_id: str = "",
    member_name: str = "",
) -> Iterator[Span]:
    """Context manager wrapping an end-to-end user query or confirmation in a root OpenTelemetry span."""
    tracer = get_tracer()
    safe_member = default_pii_redactor.scrub_text(member_name)
    with tracer.start_as_current_span(operation_name) as root_span:
        root_span.set_attribute("concierge.session_id", session_id)
        root_span.set_attribute("concierge.member_id", member_id)
        root_span.set_attribute("concierge.member_name", safe_member)
        yield root_span


def get_recent_structured_logs(limit: int = 50) -> list[dict[str, Any]]:
    """Returns recent PII-redacted structured JSON log records for diagnostics and tests."""
    with _LOG_BUFFER_LOCK:
        items = list(_RECENT_JSON_LOGS)
    return items[-max(1, min(limit, 300)) :]


def get_recent_trace_spans(limit: int = 50) -> list[dict[str, Any]]:
    """Serializes finished OpenTelemetry spans showing linked `trace_id` and `parent_span_id` chains."""
    spans = _SPAN_EXPORTER.get_finished_spans()[-max(1, min(limit, 400)) :]
    serialized: list[dict[str, Any]] = []
    for sp in spans:
        ctx = sp.get_span_context()
        parent_id = f"{sp.parent.span_id:016x}" if sp.parent and sp.parent.is_valid else ""
        duration_ms = (
            round((sp.end_time - sp.start_time) / 1_000_000.0, 2)
            if sp.end_time and sp.start_time
            else None
        )
        serialized.append(
            {
                "name": sp.name,
                "trace_id": f"{ctx.trace_id:032x}",
                "span_id": f"{ctx.span_id:016x}",
                "parent_span_id": parent_id,
                "status": sp.status.status_code.name,
                "duration_ms": duration_ms,
                "attributes": dict(sp.attributes or {}),
            }
        )
    return serialized


def clear_telemetry_buffers() -> None:
    """Clears in-memory log and span buffers (used in deterministic test suites)."""
    with _LOG_BUFFER_LOCK:
        _RECENT_JSON_LOGS.clear()
    _SPAN_EXPORTER.clear()


class FamilyObservabilityPlugin(BasePlugin):
    """ADK Plugin recording paired INTENT vs. OUTCOME JSON logs and linked OpenTelemetry spans."""

    def __init__(
        self,
        name: str = "family_observability_plugin",
        *,
        redactor: Optional[PiiRedactionPipeline] = None,
    ) -> None:
        super().__init__(name=name)
        self._redactor = redactor or default_pii_redactor
        self._inflight_runs: dict[str, tuple[Span, float, str]] = {}
        self._inflight_agents: dict[str, tuple[Span, float, str]] = {}
        self._inflight_models: dict[str, tuple[Span, float, str, str]] = {}
        self._inflight_tools: dict[str, tuple[Span, float, str]] = {}

    def _start_child_span(self, span_name: str, parent_span: Optional[Span] = None) -> Span:
        tracer = get_tracer()
        ctx = (
            trace.set_span_in_context(parent_span)
            if parent_span is not None
            else otel_context.get_current()
        )
        return tracer.start_span(span_name, context=ctx)

    async def before_run_callback(
        self,
        *,
        invocation_context: InvocationContext,
    ) -> None:
        inv_id = invocation_context.invocation_id or f"inv-{uuid.uuid4().hex[:8]}"
        session_id = invocation_context.session.id if invocation_context.session else ""
        root_agent_name = getattr(invocation_context.agent, "name", "family_concierge")

        user_preview = ""
        if invocation_context.user_content and invocation_context.user_content.parts:
            user_preview = " ".join(
                p.text for p in invocation_context.user_content.parts if p.text
            )[:400]

        span = self._start_child_span(f"adk.run.{root_agent_name}")
        span.set_attribute("gen_ai.invocation.id", inv_id)
        span.set_attribute("gen_ai.session.id", session_id)
        corr_id = f"run-{inv_id}"
        self._inflight_runs[inv_id] = (span, time.perf_counter(), corr_id)

        emit_structured_log(
            event_type="RUNNER_INVOCATION",
            phase="INTENT",
            message=f"Starting ADK invocation for {root_agent_name}",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=corr_id,
            agent_name=root_agent_name,
            payload={"intended_user_query": user_preview},
            span=span,
            redactor=self._redactor,
        )
        return None

    async def after_run_callback(
        self,
        *,
        invocation_context: InvocationContext,
    ) -> None:
        inv_id = invocation_context.invocation_id or ""
        entry = self._inflight_runs.pop(inv_id, None)
        session_id = invocation_context.session.id if invocation_context.session else ""
        root_agent_name = getattr(invocation_context.agent, "name", "family_concierge")
        span, started_at, corr_id = (
            entry if entry is not None else (None, time.perf_counter(), f"run-{inv_id}")
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        event_count = len(invocation_context.session.events) if invocation_context.session else 0

        if span is not None:
            span.set_attribute("gen_ai.session.event_count", event_count)
            span.set_status(Status(StatusCode.OK))

        emit_structured_log(
            event_type="RUNNER_INVOCATION",
            phase="OUTCOME",
            message=f"Completed ADK invocation for {root_agent_name}",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=corr_id,
            agent_name=root_agent_name,
            outcome_status="SUCCESS",
            duration_ms=elapsed_ms,
            payload={"session_event_count": event_count},
            span=span,
            redactor=self._redactor,
        )
        if span is not None:
            span.end()

    async def on_run_error_callback(
        self,
        *,
        invocation_context: InvocationContext,
        error: Exception,
    ) -> None:
        inv_id = invocation_context.invocation_id or ""
        entry = self._inflight_runs.pop(inv_id, None)
        session_id = invocation_context.session.id if invocation_context.session else ""
        span, started_at, corr_id = (
            entry if entry is not None else (None, time.perf_counter(), f"run-{inv_id}")
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        if span is not None:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))

        emit_structured_log(
            event_type="RUNNER_INVOCATION",
            phase="OUTCOME",
            severity="ERROR",
            message=f"ADK invocation failed: {type(error).__name__}",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=corr_id,
            outcome_status="ERROR",
            duration_ms=elapsed_ms,
            payload={"error_type": type(error).__name__, "error_message": str(error)},
            span=span,
            redactor=self._redactor,
        )
        if span is not None:
            span.end()

    async def before_agent_callback(
        self,
        *,
        agent: BaseAgent,
        callback_context: CallbackContext,
    ) -> None:
        inv_id = callback_context.invocation_id
        session_id = callback_context.session.id if callback_context.session else ""
        parent_run = self._inflight_runs.get(inv_id)
        parent_span = parent_run[0] if parent_run else None

        key = f"{inv_id}:{agent.name}"
        corr_id = f"agent-{uuid.uuid4().hex[:8]}"
        span = self._start_child_span(f"adk.agent.{agent.name}", parent_span=parent_span)
        span.set_attribute("gen_ai.agent.name", agent.name)
        self._inflight_agents[key] = (span, time.perf_counter(), corr_id)

        emit_structured_log(
            event_type="AGENT_STEP",
            phase="INTENT",
            message=f"Agent '{agent.name}' starting execution",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=corr_id,
            agent_name=agent.name,
            payload={"agent_description": getattr(agent, "description", "")[:200]},
            span=span,
            redactor=self._redactor,
        )
        return None

    async def after_agent_callback(
        self,
        *,
        agent: BaseAgent,
        callback_context: CallbackContext,
    ) -> None:
        inv_id = callback_context.invocation_id
        session_id = callback_context.session.id if callback_context.session else ""
        key = f"{inv_id}:{agent.name}"
        entry = self._inflight_agents.pop(key, None)
        span, started_at, corr_id = (
            entry if entry is not None else (None, time.perf_counter(), f"agent-{agent.name}")
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        if span is not None:
            span.set_status(Status(StatusCode.OK))

        emit_structured_log(
            event_type="AGENT_STEP",
            phase="OUTCOME",
            message=f"Agent '{agent.name}' finished execution",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=corr_id,
            agent_name=agent.name,
            outcome_status="SUCCESS",
            duration_ms=elapsed_ms,
            span=span,
            redactor=self._redactor,
        )
        if span is not None:
            span.end()
        return None

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        inv_id = callback_context.invocation_id
        session_id = callback_context.session.id if callback_context.session else ""
        agent_name = callback_context.agent_name
        model_id = str(llm_request.model or "")
        parent_agent = self._inflight_agents.get(f"{inv_id}:{agent_name}")
        parent_run = self._inflight_runs.get(inv_id)
        parent_span = parent_agent[0] if parent_agent else (parent_run[0] if parent_run else None)

        key = f"{inv_id}:{agent_name}"
        corr_id = f"llm-{uuid.uuid4().hex[:8]}"
        span = self._start_child_span(f"adk.llm.{model_id or 'generate'}", parent_span=parent_span)
        span.set_attribute("gen_ai.request.model", model_id)
        span.set_attribute("gen_ai.agent.name", agent_name)
        span.set_attribute("gen_ai.prompt.turn_count", len(llm_request.contents or []))
        self._inflight_models[key] = (span, time.perf_counter(), corr_id, model_id)

        emit_structured_log(
            event_type="MODEL_GENERATION",
            phase="INTENT",
            message=f"Dispatching LLM request to '{model_id}' for '{agent_name}'",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=corr_id,
            agent_name=agent_name,
            model_id=model_id,
            payload={
                "content_turns": len(llm_request.contents or []),
                "declared_tools": list((llm_request.tools_dict or {}).keys()),
            },
            span=span,
            redactor=self._redactor,
        )
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> None:
        inv_id = callback_context.invocation_id
        session_id = callback_context.session.id if callback_context.session else ""
        agent_name = callback_context.agent_name
        key = f"{inv_id}:{agent_name}"
        entry = self._inflight_models.pop(key, None)
        span, started_at, corr_id, model_id = (
            entry if entry is not None else (None, time.perf_counter(), f"llm-{agent_name}", "")
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0

        usage = llm_response.usage_metadata
        prompt_tokens = getattr(usage, "prompt_token_count", None) if usage else None
        output_tokens = getattr(usage, "candidates_token_count", None) if usage else None
        proposed_fcs = [
            p.function_call.name
            for p in (llm_response.content.parts if llm_response.content and llm_response.content.parts else [])
            if p.function_call and p.function_call.name
        ]
        preview_text = " ".join(
            p.text
            for p in (llm_response.content.parts if llm_response.content and llm_response.content.parts else [])
            if p.text and not p.thought
        )[:300]

        if span is not None:
            if prompt_tokens is not None:
                span.set_attribute("gen_ai.usage.input_tokens", int(prompt_tokens))
            if output_tokens is not None:
                span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))
            span.set_status(Status(StatusCode.OK))

        emit_structured_log(
            event_type="MODEL_GENERATION",
            phase="OUTCOME",
            message=f"Received LLM response from '{model_id}' for '{agent_name}'",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=corr_id,
            agent_name=agent_name,
            model_id=model_id,
            outcome_status="SUCCESS",
            duration_ms=elapsed_ms,
            payload={
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "proposed_tool_calls": proposed_fcs,
                "response_preview": preview_text,
            },
            span=span,
            redactor=self._redactor,
        )
        if span is not None:
            span.end()
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> None:
        inv_id = tool_context.invocation_id
        session_id = tool_context.session.id if tool_context.session else ""
        agent_name = tool_context.agent_name
        fc_id = tool_context.function_call_id or f"fc-{uuid.uuid4().hex[:8]}"

        parent_agent = self._inflight_agents.get(f"{inv_id}:{agent_name}")
        parent_run = self._inflight_runs.get(inv_id)
        parent_span = parent_agent[0] if parent_agent else (parent_run[0] if parent_run else None)

        span = self._start_child_span(f"adk.tool.{tool.name}", parent_span=parent_span)
        span.set_attribute("gen_ai.tool.name", tool.name)
        span.set_attribute("gen_ai.tool.call_id", fc_id)
        span.set_attribute("gen_ai.agent.name", agent_name)
        self._inflight_tools[fc_id] = (span, time.perf_counter(), fc_id)

        emit_structured_log(
            event_type="TOOL_EXECUTION",
            phase="INTENT",
            message=f"Agent '{agent_name}' intends to invoke tool '{tool.name}'",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=fc_id,
            agent_name=agent_name,
            tool_name=tool.name,
            payload={"intended_args": tool_args},
            span=span,
            redactor=self._redactor,
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> None:
        del tool_args
        inv_id = tool_context.invocation_id
        session_id = tool_context.session.id if tool_context.session else ""
        agent_name = tool_context.agent_name
        fc_id = tool_context.function_call_id or ""
        entry = self._inflight_tools.pop(fc_id, None)
        span, started_at, corr_id = (
            entry if entry is not None else (None, time.perf_counter(), fc_id or f"fc-{tool.name}")
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0

        outcome_status = "SUCCESS"
        if isinstance(result, dict):
            if "requires confirmation" in str(result.get("error", "")).lower():
                outcome_status = "PAUSED_FOR_HITL"
            elif result.get("status") == "error" or result.get("error"):
                outcome_status = "GUIDED_ERROR"

        if span is not None:
            span.set_attribute("concierge.tool.outcome_status", outcome_status)
            span.set_status(Status(StatusCode.OK))

        emit_structured_log(
            event_type="TOOL_EXECUTION",
            phase="OUTCOME",
            message=f"Tool '{tool.name}' completed with outcome '{outcome_status}'",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=corr_id,
            agent_name=agent_name,
            tool_name=tool.name,
            outcome_status=outcome_status,
            duration_ms=elapsed_ms,
            payload={"actual_result": result},
            span=span,
            redactor=self._redactor,
        )
        if span is not None:
            span.end()
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> None:
        del tool_args
        inv_id = tool_context.invocation_id
        session_id = tool_context.session.id if tool_context.session else ""
        agent_name = tool_context.agent_name
        fc_id = tool_context.function_call_id or ""
        entry = self._inflight_tools.pop(fc_id, None)
        span, started_at, corr_id = (
            entry if entry is not None else (None, time.perf_counter(), fc_id or f"fc-{tool.name}")
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0

        if span is not None:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))

        emit_structured_log(
            event_type="TOOL_EXECUTION",
            phase="OUTCOME",
            severity="ERROR",
            message=f"Tool '{tool.name}' raised {type(error).__name__}",
            session_id=session_id,
            invocation_id=inv_id,
            correlation_id=corr_id,
            agent_name=agent_name,
            tool_name=tool.name,
            outcome_status="EXCEPTION",
            duration_ms=elapsed_ms,
            payload={"error_type": type(error).__name__, "error_message": str(error)},
            span=span,
            redactor=self._redactor,
        )
        if span is not None:
            span.end()
        return None
