"""
Two-word gate: should the assistant speak right now?

Returns True (YES) or False (NO). Fails closed — any parse error or
model unavailability returns False.
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


async def should_speak(
    state_window: list,
    last_suggestion_type: Optional[str],
    minutes_since_last: float,
    was_dismissed: bool = False,
    minutes_since_dismiss: float = 99.0,
) -> bool:
    global _total_calls, _yes_count
    _total_calls += 1

    compact_window = [_compact(s) for s in state_window]
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
        return False

    decision = raw.strip().upper()
    result = decision.startswith("Y")

    if result:
        _yes_count += 1
        log.info("Gate: YES (fuel=%.0f%% rain=%s meal=%.1fh)",
                 compact_window[-1]["fuel_pct"],
                 compact_window[-1]["rain_in_min"],
                 compact_window[-1]["hours_since_meal"])
    else:
        log.debug("Gate: NO")

    return result
