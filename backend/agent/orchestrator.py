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
You are Concierge, an in-car AI assistant. Translate the driver's message into a JSON action plan.

STRICT RULES — follow exactly:
1. Food/drink only: If the request is about food or coffee, ONLY include find_poi:food. Do NOT add music, cabin_temp, windows, or navigation.
2. Emotional only: "tired/stressed/energetic" → music + cabin_temp. Do NOT add find_poi unless explicitly asked to find food.
3. Cabin only: temperature/AC/windows requests → cabin_temp or ac or windows only.
4. Never add actions the driver did not ask for.

Emotional → action mappings:
- "tired / sleepy / exhausted"  → cabin_temp:19 + music:calm energy:3 + reduce_alerts:20
- "stressed / anxious"          → cabin_temp:21 + music:calm energy:2 + reduce_alerts:15
- "energetic / pumped"          → cabin_temp:20 + music:upbeat energy:8
- "relaxing / chill"            → cabin_temp:21 + music:relaxed energy:3
- "hungry / starving"           → find_poi:food ONLY
- "home"                        → navigate:home ONLY

Conversation continuity:
- Resolve "it", "that", "closer", "instead" using prior context
- Modify the previous plan, not start fresh

Output ONLY valid JSON:
{
  "interpretation": "one sentence",
  "confidence": "high" | "medium" | "low",
  "clarify": null,
  "reply": "warm, direct 1-2 sentence spoken response",
  "actions": [
    {"type": "cabin_temp", "celsius": 20},
    {"type": "music", "mood": "calm", "energy": 3},
    {"type": "find_poi", "category": "food"},
    {"type": "navigate", "destination": "home"},
    {"type": "reduce_alerts", "minutes": 15},
    {"type": "windows", "open": false},
    {"type": "ac", "on": true}
  ]
}
Only include actions that are directly needed. Omit everything else."""

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
