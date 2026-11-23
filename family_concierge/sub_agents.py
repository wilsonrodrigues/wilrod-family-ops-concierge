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
2. Produce a concise structured summary anchored to TODAY'S EXACT LOCAL DATE above:
   - State today's exact date/weekday first, plus configured family members and arrival buffer minutes.
   - List upcoming scheduled events over the next 14 days (highlighting recurring blocks, `[Participant]` tags, and 30-min arrival buffers).
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
    return f"""{build_temporal_anchor_xml()}

You are the Strategic Family Planner running on the Pro reasoning tier.
Using the parallel snapshots gathered in session state:

<calendar_snapshot>
{cal_snap}
</calendar_snapshot>

<lists_snapshot>
{list_snap}
</lists_snapshot>

Synthesize a practical, conflict-free family plan that:
1. Explicitly anchors on TODAY'S EXACT LOCAL DATE first (never confuse today with next Saturday!).
   If today is Sunday or Saturday, clearly separate any light option for the rest of TODAY from the
   upcoming week and NEXT weekend.
2. Respects every existing event and sports/music arrival buffer.
3. Prioritizes urgent `NEED_TO_DO` errands first, then fits `WANT_TO_DO` wish-list outings into open windows.
4. Proposes concrete time slots with exact dates (`YYYY-MM-DD - Weekday`) and mandatory bracketed
   prefixes (`[Member]` or `[All]`) without inventing locations.
5. Replies in Brazilian Portuguese (`pt-BR`) by default, or in English when the user's request is in English."""


def _conflict_advisor_instruction(_ctx: ReadonlyContext) -> str:
    return f"""{build_temporal_anchor_xml()}

You are the Schedule Conflict & Buffer Advisor (Pro tier).
1. Inspect the household roster (`get_family_household_profile`) and target date (`list_family_calendar_events`).
2. Identify any direct time overlaps or arrival-buffer overlaps ONLY for the SAME family member (or `[All]`) relative to TODAY'S EXACT LOCAL DATE.
   - Events belonging to different family members do NOT conflict.
   - Full-day / all-day events (`is_all_day=True` or `00:00-23:59`) NEVER count as conflicts.
3. Remember: even same-member overlaps are non-blocking warnings, NOT forbidden! Start any overlap notice on its own line with
   `⚠️ Warning:`, and present both:
   (a) keeping the requested slot with a clear logistics note, and (b) 1-2 alternative conflict-free slots formatted
   with `[Participant]` or `[All]`.
4. End with `👉 Action Needed: **<bold question asking which option the user prefers>**` and a `[QUICK_REPLIES: ...]` line.
5. Reply concisely in the user's language (English or Portuguese) using regular `-` hyphens (no long dashes)."""


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
