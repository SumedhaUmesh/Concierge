"""
Intent Decomposition Engine.

Parses compound driver utterances into ordered, typed sub-intents.
Examples:
  "I'm hungry and need gas"   → [fuel(4), meal(3)]
  "Find a scenic route"        → [navigate(3), scenic(3)]
  "I'm tired and want music"  → [rest(4), music(2)]
"""

import re
from dataclasses import dataclass, field


@dataclass
class Intent:
    type: str               # fuel | meal | rest | comfort | music | navigate | scenic | query
    detail: str             # matched keyword/phrase
    urgency: int            # 1–5
    modifiers: dict = field(default_factory=dict)  # extra constraints (e.g. {"scenic": True})


# (regex, intent_type, urgency)
_PATTERNS: list[tuple[str, str, int]] = [
    (r"\b(gas|fuel|charge|charging|fill.?up|petrol|station)\b",                       "fuel",     4),
    (r"\b(tired|fatigue|sleepy|rest|break|nap|stretch|pull.?over)\b",                "rest",     4),
    (r"\b(hungry|eat|food|coffee|lunch|dinner|breakfast|snack|cafe|restaurant)\b",   "meal",     3),
    (r"\b(hot|cold|warm|cool|ac|heat|window|sunroof|temperature|fan)\b",              "comfort",  3),
    (r"\b(scenic|view|beautiful|landscape|coast|coastal|mountain|countryside)\b",    "scenic",   3),
    (r"\b(go to|navigate|take me|directions?|route|how (do i|to get)|find)\b",        "navigate", 3),
    (r"\b(music|song|play|playlist|radio|beats|something)\b",                         "music",    2),
    (r"\b(what|how|when|where|why|is there|tell me|range|speed)\b",                  "query",    1),
]

# Scenic qualifiers that can attach to a navigate intent as a modifier
_SCENIC_RE = re.compile(
    r"\b(scenic|beautiful|pretty|view|coastal|mountain|countryside|avoid.?highway|back.?road)\b",
    re.IGNORECASE,
)
_FAST_RE = re.compile(
    r"\b(fastest|quickest|direct|highway|freeway|shortest)\b",
    re.IGNORECASE,
)


def decompose(utterance: str) -> list[Intent]:
    """
    Parse an utterance into ordered Intent objects (highest urgency first).
    Scenic and fast preferences are attached as modifiers to the navigate intent.
    """
    text = utterance.lower()
    seen: dict[str, Intent] = {}

    for pattern, intent_type, urgency in _PATTERNS:
        m = re.search(pattern, text)
        if m:
            if intent_type not in seen or urgency > seen[intent_type].urgency:
                seen[intent_type] = Intent(
                    type=intent_type,
                    detail=m.group(0),
                    urgency=urgency,
                )

    # Attach route modifiers to navigate intent (or create implicit navigate)
    has_scenic = bool(_SCENIC_RE.search(utterance))
    has_fast   = bool(_FAST_RE.search(utterance))

    if has_scenic or has_fast:
        if "navigate" not in seen:
            # "Find scenic route" — implicit navigate even without explicit nav keyword
            seen["navigate"] = Intent(type="navigate", detail="implied", urgency=3)
        seen["navigate"].modifiers["scenic"] = has_scenic
        seen["navigate"].modifiers["fast"]   = has_fast

    return sorted(seen.values(), key=lambda i: i.urgency, reverse=True)


def is_compound(utterance: str) -> bool:
    """True if the utterance contains multiple distinct intents."""
    return len(decompose(utterance)) > 1


def describe(intents: list[Intent]) -> str:
    """Human-readable summary of decomposed intents (for logging/UI)."""
    parts = []
    for i in intents:
        s = i.type
        if i.modifiers:
            mods = ", ".join(f"{k}={v}" for k, v in i.modifiers.items())
            s += f"({mods})"
        parts.append(s)
    return " + ".join(parts)
