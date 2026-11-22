"""Strategic Model Router mapping Family Ops tasks to optimal Gemini model tiers with HTTP retry resilience."""

from __future__ import annotations

from enum import Enum
import os

from google.adk.models import Gemini
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from .secrets import resolve_gemini_api_key


class TaskComplexityTier(str, Enum):
    """Task tiers used to route agent workloads to the optimal cost/latency/reasoning model."""

    FAST_PARALLEL_LOOKUP = "FAST_PARALLEL_LOOKUP"
    MULTIMODAL_COORDINATOR = "MULTIMODAL_COORDINATOR"
    DEEP_STRATEGIC_PLANNING = "DEEP_STRATEGIC_PLANNING"


class ModelRouteDecision(BaseModel):
    """Structured routing decision explaining why a specific Gemini model tier was chosen."""

    model_config = ConfigDict(extra="forbid")

    tier: TaskComplexityTier = Field(..., description="Selected complexity tier.")
    model_id: str = Field(..., description="Concrete Gemini model identifier.")
    rationale: str = Field(..., description="Engineering rationale for cost, latency, and reasoning quality.")


# Configurable model IDs verified against corporate AI Studio / Gemini API endpoints
FAST_MODEL_ID = os.environ.get("FAMILY_MODEL_FAST", "gemini-3.5-flash-lite")
FLASH_MODEL_ID = os.environ.get("FAMILY_MODEL_FLASH", "gemini-3.8-flash")
PRO_MODEL_ID = os.environ.get("FAMILY_MODEL_PRO", "gemini-3.1-pro-preview")

RETRY_HTTP_OPTIONS = types.HttpOptions(
    timeout=45_000,  # 45s per-attempt timeout in ms so hung sockets retry instead of stalling
    retry_options=types.HttpRetryOptions(
        attempts=3,
        initial_delay=1.0,
        exp_base=2.0,
        http_status_codes=[408, 429, 500, 502, 503, 504],
    ),
)
_RETRY_HTTP_OPTIONS = RETRY_HTTP_OPTIONS

_ROUTE_TABLE: dict[TaskComplexityTier, ModelRouteDecision] = {
    TaskComplexityTier.FAST_PARALLEL_LOOKUP: ModelRouteDecision(
        tier=TaskComplexityTier.FAST_PARALLEL_LOOKUP,
        model_id=FAST_MODEL_ID,
        rationale="Ultra-low-latency Flash-Lite tier for concurrent read-only SQLite calendar and action-list retrieval.",
    ),
    TaskComplexityTier.MULTIMODAL_COORDINATOR: ModelRouteDecision(
        tier=TaskComplexityTier.MULTIMODAL_COORDINATOR,
        model_id=FLASH_MODEL_ID,
        rationale="Balanced multimodal Flash tier for root coordination, voice/image invitation extraction, and CRUD tool calls.",
    ),
    TaskComplexityTier.DEEP_STRATEGIC_PLANNING: ModelRouteDecision(
        tier=TaskComplexityTier.DEEP_STRATEGIC_PLANNING,
        model_id=PRO_MODEL_ID,
        rationale="High-reasoning Pro tier for multi-member schedule synthesis, 30-min buffer math, and wish-list optimization.",
    ),
}

_PLANNING_KEYWORDS = (
    "organizar",
    "organize",
    "planejar",
    "planeje",
    "plan ",
    "fim de semana",
    "weekend",
    "semana",
    "encaixar",
    "sugira",
    "suggest",
    "conflito",
    "conflict",
    "otimizar",
)


def get_model_for_tier(tier: TaskComplexityTier) -> Gemini:
    """Returns a retry-hardened ADK `Gemini` model instance resolved via Secret Manager / env."""
    client_kwargs: dict[str, object] = {"http_options": _RETRY_HTTP_OPTIONS}
    resolved_key = resolve_gemini_api_key()
    if resolved_key:
        client_kwargs["api_key"] = resolved_key
    return Gemini(
        model=_ROUTE_TABLE[tier].model_id,
        client_kwargs=client_kwargs,
    )


def get_model_id_for_tier(tier: TaskComplexityTier) -> str:
    """Returns the string Gemini model ID for a given `TaskComplexityTier`."""
    return _ROUTE_TABLE[tier].model_id


def get_route_decision(tier: TaskComplexityTier) -> ModelRouteDecision:
    """Returns full `ModelRouteDecision` metadata for observability and UI badges."""
    return _ROUTE_TABLE[tier]


def classify_request_complexity(message: str, has_media: bool = False) -> ModelRouteDecision:
    """Classifies an incoming user turn for telemetry and strategic routing hints."""
    lowered = (message or "").lower()
    if any(kw in lowered for kw in _PLANNING_KEYWORDS):
        return _ROUTE_TABLE[TaskComplexityTier.DEEP_STRATEGIC_PLANNING]
    if has_media:
        return _ROUTE_TABLE[TaskComplexityTier.MULTIMODAL_COORDINATOR]
    return _ROUTE_TABLE[TaskComplexityTier.MULTIMODAL_COORDINATOR]
