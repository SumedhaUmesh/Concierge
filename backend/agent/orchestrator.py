"""
Orchestration Engine.

Translates vague, emotional, and indirect driver language into
coordinated multi-system action plans via the local LLM.

Input:  "I'm tired" / "Take me somewhere relaxing" / "Actually make it closer"
Output: structured plan → multiple actions executed in parallel
"""

import json
import logging
from typing import Optional

from agent import llm as llm_mod

log = logging.getLogger(__name__)

_SYSTEM = """\
You are Concierge, an in-car AI assistant. You understand natural, emotional, and indirect language — not commands.

Given the driver's message, infer their actual need and return a JSON action plan.
Always make a confident interpretation and act on it. Never say "I don't understand."

Emotional → action mappings (use as guidance, apply judgment):
- "tired / sleepy / exhausted"    → cooler cabin + find coffee/rest stop + alert music + fewer interruptions
- "relaxing / chill / peaceful"   → calm music (energy 2-3) + comfortable cabin (21°C) + reduce alerts
- "stressed / anxious / on edge"  → calm music + reduce alerts + reassuring reply
- "energetic / pumped / awake"    → upbeat music (energy 7-9) + slightly cooler cabin
- "hungry / starving"             → find food nearby
- "home"                          → navigate home
- "I want a relaxing drive"       → scenic route preference + calm music + minimal alerts
- "long drive"                    → comfortable cabin + rest stop awareness + alert pacing

Constraint handling:
- "scenic but don't make me late"    → prefer scenic if time margin > 10 min, else note trade-off in reply
- "closer" (follow-up)               → same category POI, smaller radius
- "why [that / this route / place]?" → explain the previous decision briefly

Conversation continuity:
- Resolve "it", "that", "closer", "instead", "another" using Prior context
- Modify the previous plan, not start fresh

Output ONLY valid JSON (no markdown, no explanation outside JSON):
{
  "interpretation": "one sentence describing what you understood",
  "confidence": "high" | "medium" | "low",
  "clarify": null,
  "reply": "warm, direct 1-2 sentence spoken response — sound like a calm human, not a robot",
  "actions": [
    {"type": "cabin_temp", "celsius": 20},
    {"type": "music", "mood": "calm", "energy": 3},
    {"type": "find_poi", "category": "coffee"},
    {"type": "find_poi", "category": "food"},
    {"type": "find_poi", "category": "rest"},
    {"type": "navigate", "destination": "home"},
    {"type": "reduce_alerts", "minutes": 15},
    {"type": "windows", "open": false},
    {"type": "ac", "on": true}
  ]
}
Only include actions that are relevant. Omit irrelevant ones entirely."""

# Smart clarification questions for when confidence is low
_CLARIFY_TEMPLATES = {
    "route":   "Scenic or just faster than usual?",
    "music":   "Something calm or more energetic?",
    "food":    "Grab-and-go or sit-down?",
    "default": "Do you mean comfort, navigation, or something else?",
}


async def orchestrate(
    utterance: str,
    state_json: str,
    conversation_context: str,
    preferences_context: str = "",
) -> Optional[dict]:
    """
    Translate a natural language utterance into a structured action plan.
    Returns the parsed plan dict, or None on failure.
    """
    pref_block = f"\nDriver preferences:\n{preferences_context}" if preferences_context else ""
    user_msg = (
        f"Vehicle state: {state_json}\n"
        f"Recent conversation:\n{conversation_context}"
        f"{pref_block}\n\n"
        f'Driver said: "{utterance}"\n\n'
        "JSON:"
    )

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",   "content": user_msg},
    ]

    raw = await llm_mod.complete(messages, max_tokens=350, temperature=0.35)
    if not raw:
        log.warning("Orchestrator: LLM returned None")
        return None

    try:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("no JSON object found")
        plan = json.loads(raw[start:end])
        log.info(
            "Orchestrator: %r (confidence=%s, %d actions)",
            plan.get("interpretation", "?")[:60],
            plan.get("confidence", "?"),
            len(plan.get("actions", [])),
        )
        return plan
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("Orchestrator: parse failed (%s): %r", exc, raw[:120])
        return None


def fallback_clarify(utterance: str) -> str:
    """Return a smart clarifying question when the LLM can't parse the input."""
    lowered = utterance.lower()
    if any(w in lowered for w in ("route", "way", "road", "drive", "scenic")):
        return _CLARIFY_TEMPLATES["route"]
    if any(w in lowered for w in ("music", "song", "play", "sound")):
        return _CLARIFY_TEMPLATES["music"]
    if any(w in lowered for w in ("eat", "food", "hungry", "restaurant")):
        return _CLARIFY_TEMPLATES["food"]
    return _CLARIFY_TEMPLATES["default"]
