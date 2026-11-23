"""Specialist ADK sub-agents implementing Parallel, Sequential, and Coordinator patterns."""

from __future__ import annotations

from google.adk.agents import Agent, ParallelAgent, SequentialAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import FunctionTool
from google.adk.tools.agent_tool import AgentTool

from .model_router import TaskComplexityTier, get_model_for_tier
from .time_context import build_temporal_anchor_xml
from .tools import (
    get_family_household_profile,
    list_family_action_items,
    list_family_calendar_events,
)


def _calendar_specialist_instruction(_ctx: ReadonlyContext) -> str:
    return f"""{build_temporal_anchor_xml()}

You are the Calendar Availability Specialist for Family Ops Concierge.
1. Call `get_family_household_profile` and `list_family_calendar_events`.
2. Produce a concise internal availability summary anchored to TODAY'S EXACT LOCAL DATE above:
   - List configured family members and upcoming scheduled events over the next 14 days (including `[Participant]` tags, `location`, and any internal 30-min sports/music arrival buffers - noting that back-to-back events at the SAME `location` do NOT require a 30-min buffer).
   - Explicitly separate open windows remaining TODAY vs. open windows during the upcoming week and next weekend.
Do not invent events or locations."""


def _lists_specialist_instruction(_ctx: ReadonlyContext) -> str:
    return f"""{build_temporal_anchor_xml()}

You are the Errands & Wish-List Specialist for Family Ops Concierge.
1. Call `list_family_action_items` (with `include_completed=False`).
2. Produce a concise summary anchored to TODAY'S EXACT LOCAL DATE above, separating:
   - Urgent `NEED_TO_DO` errands (obligations, owner, and any deadline dates relative to today).
   - `WANT_TO_DO` wish-list activities (outings/cinema/ideas, owner, and any closing dates relative to today)."""


def _strategic_planner_instruction(ctx: ReadonlyContext) -> str:
    cal_snap = ctx.state.get("calendar_snapshot", "") if ctx and ctx.state else ""
    list_snap = ctx.state.get("lists_snapshot", "") if ctx and ctx.state else ""
    already_replied_today = bool(ctx and ctx.state and ctx.state.get("already_replied_today"))
    date_rule = (
        "DO NOT state or repeat today's date (e.g. never write 'Hoje é...') because you already replied today; jump straight to the planned bullets."
        if already_replied_today
        else "You may mention today's date at most once briefly if helpful."
    )
    return f"""{build_temporal_anchor_xml()}

You are the Strategic Family Planner running on the Pro reasoning tier.
Using the parallel snapshots gathered in session state:

<calendar_snapshot>
{cal_snap}
</calendar_snapshot>

<lists_snapshot>
{list_snap}
</lists_snapshot>

Synthesize a practical, conflict-free family plan optimized for a mobile screen (STRICTLY CONCISE, max ~100 words total):
1. Use TODAY'S EXACT LOCAL DATE silently for accurate date math (never confuse today with next Saturday!). {date_rule}
2. Respect existing events and internal 30-min sports/music arrival buffers (skipping the 30-min buffer only when two back-to-back events happen at the SAME location); do NOT print buffer labels on non-conflicting bullets. Prioritize urgent `NEED_TO_DO` items, then fit `WANT_TO_DO` wishes.
3. Output at most 4 to 6 compact one-line bullets (`- DD/MM (Dia) HH:MM-HH:MM: **[Membro] Atividade**`) with zero filler intro/outro, without addressing the user by name, and without inventing locations.
4. End with one short `👉 Ação Necessária: **...**` question and `[QUICK_REPLIES: ...]`.
5. Reply in Brazilian Portuguese (`pt-BR`) by default, or in English when the user's request is in English, using regular `-` hyphens only."""


def _conflict_advisor_instruction(ctx: ReadonlyContext) -> str:
    already_replied_today = bool(ctx and ctx.state and ctx.state.get("already_replied_today"))
    date_rule = (
        "DO NOT repeat today's date ('Hoje é...') in your reply."
        if already_replied_today
        else "Mention today's date only if strictly needed."
    )
    return f"""{build_temporal_anchor_xml()}

You are the Schedule Conflict & Buffer Advisor (Pro tier).
1. Inspect the household roster (`get_family_household_profile`) and target date (`list_family_calendar_events`).
2. Identify any direct time overlaps or 30-min arrival-buffer overlaps ONLY for the SAME family member (or `[All]`) relative to TODAY'S EXACT LOCAL DATE:
   - Events belonging to different family members do NOT conflict.
   - Full-day / all-day events (`is_all_day=True` or `00:00-23:59`) NEVER count as conflicts.
   - Back-to-back events at the SAME `location` do NOT trigger a 30-min arrival-buffer warning.
3. Keep your entire reply ultra-concise (max ~65 words total, zero filler, do not address the user by name; {date_rule}):
   - One `⚠️ Aviso:` line naming the conflicting event and time.
   - At most 2 short bullets: (a) keep requested time, or (b) 1-2 free slots formatted with `[Participant]` or `[All]`.
   - One `👉 Ação Necessária: **<short question>**` line + `[QUICK_REPLIES: ...]`.
4. Reply in Brazilian Portuguese (`pt-BR`) by default (or English if requested in English) using regular `-` hyphens (no long dashes)."""


# ---------------------------------------------------------------------------
# Stage 1 (Parallel Fan-Out on Fast Flash-Lite Tier): Concurrent Context Gatherers
# ---------------------------------------------------------------------------

calendar_availability_specialist = Agent(
    name="calendar_availability_specialist",
    model=get_model_for_tier(TaskComplexityTier.FAST_PARALLEL_LOOKUP),
    description="Fast read-only specialist that retrieves household members, buffer rules, and scheduled calendar events.",
    instruction=_calendar_specialist_instruction,
    tools=[
        FunctionTool(get_family_household_profile),
        FunctionTool(list_family_calendar_events),
    ],
    output_key="calendar_snapshot",
)

errands_and_wishlist_specialist = Agent(
    name="errands_and_wishlist_specialist",
    model=get_model_for_tier(TaskComplexityTier.FAST_PARALLEL_LOOKUP),
    description="Fast read-only specialist that retrieves open Need-To-Do errands and expiring Want-To-Do wishes.",
    instruction=_lists_specialist_instruction,
    tools=[FunctionTool(list_family_action_items)],
    output_key="lists_snapshot",
)

parallel_household_gatherer = ParallelAgent(
    name="parallel_household_gatherer",
    description="Concurrently fetches Family Calendar availability and Need-To-Do / Want-To-Do lists.",
    sub_agents=[
        calendar_availability_specialist,
        errands_and_wishlist_specialist,
    ],
)

# ---------------------------------------------------------------------------
# Stage 2 (Sequential Synthesis on Pro Reasoning Tier): Multi-Constraint Planner
# ---------------------------------------------------------------------------

strategic_family_planner_pro = Agent(
    name="strategic_family_planner_pro",
    model=get_model_for_tier(TaskComplexityTier.DEEP_STRATEGIC_PLANNING),
    description="High-reasoning Pro specialist that synthesizes conflict-free weekend and weekly family plans.",
    instruction=_strategic_planner_instruction,
    output_key="proposed_family_plan",
)

weekend_planning_pipeline = SequentialAgent(
    name="weekend_planning_pipeline",
    description=(
        "Multi-agent Sequential + Parallel workflow for organizing schedules/weekends, fitting wish-list outings "
        "into open calendar slots, and balancing Need-To-Do errands across family members."
    ),
    sub_agents=[
        parallel_household_gatherer,
        strategic_family_planner_pro,
    ],
)

weekend_planning_pipeline_tool = AgentTool(agent=weekend_planning_pipeline)

# ---------------------------------------------------------------------------
# Inline Single-Turn Pro Sub-Agent: Deep Schedule Conflict & Buffer Resolver
# ---------------------------------------------------------------------------

schedule_conflict_advisor_pro = Agent(
    name="schedule_conflict_advisor_pro",
    model=get_model_for_tier(TaskComplexityTier.DEEP_STRATEGIC_PLANNING),
    mode="single_turn",
    description=(
        "Pro-tier conflict resolution specialist invoked when multi-member calendar overlaps "
        "or tricky arrival-buffer trade-offs need alternative slot options."
    ),
    instruction=_conflict_advisor_instruction,
    tools=[
        FunctionTool(get_family_household_profile),
        FunctionTool(list_family_calendar_events),
    ],
)
