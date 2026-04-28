"""
Two-stage gate: should the assistant speak right now?

1. Python rule pre-check — catches obvious triggers instantly (no LLM call).
2. LLM confirmation — handles ambiguous / compound conditions.

Returns True (YES) or False (NO). Fails closed on any error.
"""

import json
import logging
from dataclasses import asdict
from typing import Optional

from agent import llm as llm_mod
from agent.prompts import SHOULD_SPEAK_V1_SYSTEM, SHOULD_SPEAK_V1_USER
from agent.prompts import SHOULD_SPEAK_V2_SYSTEM, SHOULD_SPEAK_V2_USER

log = logging.getLogger(__name__)

# Metrics
_total_calls = 0
_yes_count = 0


def stats() -> dict:
    return {"total": _total_calls, "yes": _yes_count}


def _compact(state) -> dict:
    """Extract only decision-relevant fields for the prompt."""
    d = asdict(state) if not isinstance(state, dict) else state
    return {
        "fuel_pct": round(d.get("fuel_percent", 100)),
        "range_km": round(d.get("range_km", 500)),
        "speed_kmh": round(d.get("speed_kmh", 0)),
        "rain_in_min": d.get("rain_in_minutes"),
        "windows_open": d.get("windows_open", False),
        "sunroof_open": d.get("sunroof_open", False),
        "hours_since_meal": round(d.get("hours_since_meal", 0), 1),
        "time": d.get("current_time", ""),
        "meeting": (
            f"{d['next_meeting_title']} @ {d['next_meeting_time']}"
            if d.get("next_meeting_title") else None
        ),
        "traffic_delay_min": d.get("traffic_delay_minutes", 0),
        "normal_travel_min": d.get("normal_travel_minutes"),
        "next_station_km": round(d.get("next_gas_station_km", 999)),
    }


def _python_precheck(
    s: dict, last_type: Optional[str], mins_since_last: float
) -> tuple[Optional[bool], Optional[str]]:
    """
    Fast deterministic check for unambiguous triggers.
    Returns (True, trigger)  → definitely speak, with trigger label for generator
    Returns (False, None)    → definitely silent
    Returns (None, None)     → ambiguous, let LLM decide
    """
    if mins_since_last < 3:
        return False, None

    rain = s.get("rain_in_min")
    if rain is not None and rain < 10 and (s.get("windows_open") or s.get("sunroof_open")):
        if last_type != "cabin":
            return True, "rain approaching with windows/sunroof open — suggest closing them (type=cabin)"

    fuel = s.get("fuel_pct", 100)
    range_km = s.get("range_km", 999)
    station_km = s.get("next_station_km", 999)
    if fuel < 15 and range_km < station_km * 2:
        if last_type != "range":
            return True, "fuel critically low — suggest stopping for fuel (type=range)"

    return None, None  # let the LLM handle meal / schedule / ambiguous cases


async def should_speak(
    state_window: list,
    last_suggestion_type: Optional[str],
    minutes_since_last: float,
    was_dismissed: bool = False,
    minutes_since_dismiss: float = 99.0,
) -> tuple[bool, Optional[str]]:
    global _total_calls, _yes_count
    _total_calls += 1

    compact_window = [_compact(s) for s in state_window]
    latest = compact_window[-1]

    log.info("Gate called (rain=%s windows=%s sunroof=%s fuel=%.0f%% meal=%.1fh)",
             latest["rain_in_min"], latest["windows_open"], latest["sunroof_open"],
             latest["fuel_pct"], latest["hours_since_meal"])

    # Fast path — no LLM needed for clear triggers
    precheck, trigger = _python_precheck(latest, last_suggestion_type, minutes_since_last)
    if precheck is True:
        _yes_count += 1
        log.info("Gate: YES (rule-based trigger=%s)", trigger)
        return True, trigger
    if precheck is False:
        log.info("Gate: NO (rule-based cooldown)")
        return False, None

    # LLM path — meal, schedule, ambiguous
    state_json = json.dumps(compact_window, indent=None)

    if was_dismissed:
        system = SHOULD_SPEAK_V2_SYSTEM
        user = SHOULD_SPEAK_V2_USER.format(
            n=len(state_window),
            state_json=state_json,
            prev_type=last_suggestion_type or "none",
            minutes_since_last=round(minutes_since_last, 1),
            was_dismissed="yes",
            minutes_since_dismiss=round(minutes_since_dismiss, 1),
        )
    else:
        system = SHOULD_SPEAK_V1_SYSTEM
        user = SHOULD_SPEAK_V1_USER.format(
            n=len(state_window),
            state_json=state_json,
            prev_type=last_suggestion_type or "none",
            minutes_since_last=round(minutes_since_last, 1),
        )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    raw = await llm_mod.complete(messages, max_tokens=4, temperature=0.0)
    if raw is None:
        log.warning("Gate: LLM returned None (no model?)")
        return False, None

    decision = raw.strip().upper()
    result = decision.startswith("Y")

    if result:
        _yes_count += 1
        log.info("Gate: YES (LLM, rain=%s meal=%.1fh)", latest["rain_in_min"], latest["hours_since_meal"])
        return True, "ambiguous condition — generate the most relevant suggestion"
    else:
        log.info("Gate: NO (LLM raw=%r)", raw)
        return False, None
