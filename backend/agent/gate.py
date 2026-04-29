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
        "driving_min": round(d.get("minutes_driving_continuously", 0)),
    }


def _get_thresholds() -> dict:
    try:
        import trip_memory  # noqa: PLC0415
        return trip_memory.get_adaptive_thresholds()
    except Exception:
        return {"meal_hours_threshold": 4.0, "fuel_pct_threshold": 15.0, "rest_minutes_threshold": 90.0}


def _score_triggers(s: dict, last_type: Optional[str]) -> list[tuple[int, str]]:
    """
    Real-Time Tradeoff Engine: score every active signal.
    Returns list of (urgency_score, trigger_string) sorted highest first.
    """
    candidates: list[tuple[int, str]] = []
    thresholds = _get_thresholds()

    rain = s.get("rain_in_min")
    if rain is not None and rain < 10 and (s.get("windows_open") or s.get("sunroof_open")):
        urgency = 5 if rain < 4 else 4
        candidates.append((urgency, "rain approaching with windows/sunroof open — suggest closing them (type=cabin)"))

    fuel = s.get("fuel_pct", 100)
    range_km = s.get("range_km", 999)
    station_km = s.get("next_station_km", 999)
    fuel_threshold = thresholds["fuel_pct_threshold"]
    if fuel < fuel_threshold and range_km < station_km * 2:
        urgency = 5 if fuel < fuel_threshold * 0.55 else 4
        candidates.append((urgency, "fuel critically low — suggest stopping for fuel (type=range)"))

    driving_min = s.get("driving_min", 0)
    rest_threshold = thresholds["rest_minutes_threshold"]
    if driving_min >= rest_threshold:
        urgency = 5 if driving_min >= rest_threshold * 1.6 else 4
        candidates.append((urgency, f"driver has been driving {driving_min:.0f} minutes continuously — suggest a rest stop (type=rest)"))

    meal_hours = s.get("hours_since_meal", 0)
    meal_threshold = thresholds["meal_hours_threshold"]
    try:
        hour = int(str(s.get("time", "0:00")).split(":")[0])
        is_mealtime = (11 <= hour <= 14) or (17 <= hour <= 21)
    except (ValueError, AttributeError):
        is_mealtime = False
    if meal_hours > meal_threshold and is_mealtime:
        urgency = 4 if meal_hours > meal_threshold + 2 else 3
        candidates.append((urgency, f"driver hasn't eaten in {meal_hours:.1f} hours during mealtime — suggest a nearby restaurant (type=meal)"))

    # Remove triggers that repeat the last type (except critical safety ones)
    SAFETY_TYPES = {"cabin", "range"}
    filtered = [
        (u, t) for u, t in candidates
        if last_type is None
        or any(f"type={last_type}" not in t or st in t for st in SAFETY_TYPES)
        or u >= 5
    ]

    return sorted(filtered, key=lambda x: x[0], reverse=True)


def _python_precheck(
    s: dict, last_type: Optional[str], mins_since_last: float
) -> tuple[Optional[bool], Optional[str]]:
    """
    Tradeoff engine pre-check: returns highest-priority trigger.
    Returns (True, trigger)  → definitely speak
    Returns (False, None)    → definitely silent (cooldown)
    Returns (None, None)     → ambiguous, let LLM decide (schedule etc.)
    """
    if mins_since_last < 3:
        return False, None

    candidates = _score_triggers(s, last_type)
    if candidates:
        best_urgency, best_trigger = candidates[0]
        log.info("Tradeoff engine: %d candidate(s), best urgency=%d — %s",
                 len(candidates), best_urgency, best_trigger[:60])
        return True, best_trigger

    # Only hand off to the LLM when there is genuinely something to evaluate.
    # Truly idle state (no meeting, low meal hours, good fuel, no fatigue) → skip LLM.
    has_meeting      = s.get("meeting") is not None
    meal_approaching = s.get("hours_since_meal", 0) > 3.0
    fuel_watch       = s.get("fuel_pct", 100) < 30
    fatigue_watch    = s.get("driving_min", 0) > 60
    traffic_notable  = s.get("traffic_delay_min", 0) > 5

    if not any([has_meeting, meal_approaching, fuel_watch, fatigue_watch, traffic_notable]):
        return False, None   # nothing interesting — skip LLM entirely

    return None, None  # let the LLM handle schedule / ambiguous cases


# How often the LLM gate is allowed to run (separate from rule-check interval)
_LLM_GATE_INTERVAL_SEC = 30
_last_llm_gate_at: float = 0.0


async def should_speak(
    state_window: list,
    last_suggestion_type: Optional[str],
    minutes_since_last: float,
    was_dismissed: bool = False,
    minutes_since_dismiss: float = 99.0,
) -> tuple[bool, Optional[str]]:
    global _total_calls, _yes_count
    _total_calls += 1

    global _last_llm_gate_at

    compact_window = [_compact(s) for s in state_window]
    latest = compact_window[-1]

    log.debug("Gate tick (rain=%s fuel=%.0f%% meal=%.1fh driving=%dmin)",
              latest["rain_in_min"], latest["fuel_pct"],
              latest["hours_since_meal"], latest["driving_min"])

    # Fast path — rule-based triggers (no LLM)
    precheck, trigger = _python_precheck(latest, last_suggestion_type, minutes_since_last)
    if precheck is True:
        _yes_count += 1
        log.info("Gate: YES (rule trigger=%s)", trigger[:60])
        return True, trigger
    if precheck is False:
        return False, None

    # LLM path — only for schedule / nuanced conditions, throttled to 30 s
    import time as _time
    now = _time.monotonic()
    if now - _last_llm_gate_at < _LLM_GATE_INTERVAL_SEC:
        return False, None
    _last_llm_gate_at = now

    log.info("Gate: LLM check (meeting=%s meal=%.1fh traffic=%dmin)",
             latest.get("meeting"), latest["hours_since_meal"], latest.get("traffic_delay_min", 0))

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
        log.debug("Gate: NO (LLM raw=%r)", raw)
        return False, None
