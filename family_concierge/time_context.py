"""Dynamic timezone-aware temporal grounding injected into every Family Ops agent turn."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import store

_PT_WEEKDAYS = {
    0: "Segunda-feira",
    1: "Terça-feira",
    2: "Quarta-feira",
    3: "Quinta-feira",
    4: "Sexta-feira",
    5: "Sábado",
    6: "Domingo",
}


def get_household_now() -> tuple[datetime, str]:
    """Returns `(now_local, timezone_name)` using the household's configured timezone."""
    try:
        tz_name = store.get_household_bundle()["household"].get("timezone") or "America/Sao_Paulo"
    except Exception:  # noqa: BLE001
        tz_name = "America/Sao_Paulo"
    try:
        return datetime.now(ZoneInfo(tz_name)), tz_name
    except ZoneInfoNotFoundError:
        return datetime.now(), tz_name


def build_temporal_anchor_xml(days_ahead: int = 14) -> str:
    """Builds a live `<live_temporal_anchor>` block with today's exact clock and a 14-day lookup table."""
    now_local, tz_name = get_household_now()
    today_iso = now_local.strftime("%Y-%m-%d")
    today_br = now_local.strftime("%d/%m/%Y")
    time_hm = now_local.strftime("%H:%M")
    en_day = now_local.strftime("%A")
    pt_day = _PT_WEEKDAYS[now_local.weekday()]

    upcoming_lines: list[str] = []
    for offset in range(days_ahead):
        dt = now_local + timedelta(days=offset)
        d_iso = dt.strftime("%Y-%m-%d")
        d_br = dt.strftime("%d/%m")
        d_en = dt.strftime("%A")
        d_pt = _PT_WEEKDAYS[dt.weekday()]
        marker = ""
        if offset == 0:
            marker = " <-- TODAY / HOJE"
        elif offset == 1:
            marker = " <-- TOMORROW / AMANHÃ"
        upcoming_lines.append(f"    - {d_iso} ({d_br}): {d_en} / {d_pt}{marker}")

    calendar_block = "\n".join(upcoming_lines)
    return f"""<live_temporal_anchor>
  TODAY'S EXACT LOCAL DATE: {today_iso} ({today_br}) - {en_day} / {pt_day}
  CURRENT LOCAL TIME: {time_hm} ({tz_name})

  REFERENCE 14-DAY CALENDAR (Use this exact table to resolve relative weekdays):
{calendar_block}

  MANDATORY DATE AWARENESS RULES:
  1. Today is STRICTLY {today_iso} ({today_br}, {en_day}). NEVER assume today is the date of
     an upcoming calendar event or next Saturday.
  2. When summarizing or organizing schedules, explicitly mention today's real date first
     (e.g. "Today is {en_day}, {today_iso}" in English, or "Hoje é {pt_day.lower()}, {today_br}" in Portuguese).
  3. If today is Saturday or Sunday, clearly distinguish what can still be done TODAY ({today_iso})
     versus what is planned for the NEXT weekend.
</live_temporal_anchor>"""
