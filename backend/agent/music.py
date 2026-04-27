"""
Music concierge — converts a natural-language taste description into
a structured query and matches it against a curated track catalogue.

No artist imitation: inputs and outputs stay in taste/mood space.
The LLM extracts genres, energy, tempo, and mood tags.
"""

import json
import logging
from typing import Optional

from agent import llm as llm_mod

log = logging.getLogger(__name__)

# ── Curated track catalogue (30 tracks across taste dimensions) ───────────────

CATALOGUE = [
    {"id": "t01", "title": "Open Road", "genres": ["electronic", "ambient"], "energy": 3, "tempo": 90, "mood": ["calm", "spacious", "focus"]},
    {"id": "t02", "title": "Late Night Cruise", "genres": ["lo-fi", "hip-hop"], "energy": 3, "tempo": 85, "mood": ["calm", "chill", "night"]},
    {"id": "t03", "title": "Highway Pulse", "genres": ["electronic", "house"], "energy": 7, "tempo": 125, "mood": ["energetic", "driving", "upbeat"]},
    {"id": "t04", "title": "Morning Commute", "genres": ["indie", "pop"], "energy": 5, "tempo": 110, "mood": ["upbeat", "fresh", "morning"]},
    {"id": "t05", "title": "Desert Run", "genres": ["rock", "alternative"], "energy": 8, "tempo": 135, "mood": ["intense", "powerful", "driving"]},
    {"id": "t06", "title": "City Rain", "genres": ["jazz", "ambient"], "energy": 2, "tempo": 70, "mood": ["calm", "melancholic", "reflective"]},
    {"id": "t07", "title": "Coastal Wind", "genres": ["indie", "folk"], "energy": 4, "tempo": 100, "mood": ["warm", "breezy", "relaxed"]},
    {"id": "t08", "title": "Neon Drift", "genres": ["synthwave", "electronic"], "energy": 7, "tempo": 120, "mood": ["retro", "night", "cruising"]},
    {"id": "t09", "title": "Focus Session", "genres": ["ambient", "classical"], "energy": 2, "tempo": 60, "mood": ["focus", "calm", "minimal"]},
    {"id": "t10", "title": "Rush Hour", "genres": ["hip-hop", "rap"], "energy": 8, "tempo": 140, "mood": ["energetic", "urban", "confident"]},
    {"id": "t11", "title": "Sunday Drive", "genres": ["soul", "r&b"], "energy": 4, "tempo": 95, "mood": ["warm", "relaxed", "happy"]},
    {"id": "t12", "title": "Mountain Pass", "genres": ["classical", "orchestral"], "energy": 5, "tempo": 105, "mood": ["epic", "scenic", "focused"]},
    {"id": "t13", "title": "Twilight", "genres": ["electronic", "chillout"], "energy": 3, "tempo": 88, "mood": ["evening", "relaxed", "smooth"]},
    {"id": "t14", "title": "Storm Front", "genres": ["rock", "metal"], "energy": 9, "tempo": 155, "mood": ["intense", "aggressive", "powerful"]},
    {"id": "t15", "title": "Back Roads", "genres": ["country", "folk"], "energy": 4, "tempo": 100, "mood": ["relaxed", "open", "warm"]},
    {"id": "t16", "title": "Uptown Rhythm", "genres": ["funk", "soul"], "energy": 7, "tempo": 118, "mood": ["groovy", "upbeat", "confident"]},
    {"id": "t17", "title": "Midnight Blue", "genres": ["jazz", "blues"], "energy": 3, "tempo": 75, "mood": ["smooth", "night", "melancholic"]},
    {"id": "t18", "title": "Sunrise Tempo", "genres": ["electronic", "downtempo"], "energy": 4, "tempo": 95, "mood": ["morning", "hopeful", "fresh"]},
    {"id": "t19", "title": "Freeway", "genres": ["hip-hop", "electronic"], "energy": 8, "tempo": 135, "mood": ["driving", "urban", "energetic"]},
    {"id": "t20", "title": "Cabin Warmth", "genres": ["acoustic", "folk"], "energy": 2, "tempo": 72, "mood": ["cozy", "warm", "intimate"]},
    {"id": "t21", "title": "Adrenaline", "genres": ["electronic", "drum-and-bass"], "energy": 10, "tempo": 170, "mood": ["intense", "fast", "energetic"]},
    {"id": "t22", "title": "Gentle Drift", "genres": ["ambient", "new-age"], "energy": 1, "tempo": 55, "mood": ["calm", "peaceful", "focus"]},
    {"id": "t23", "title": "Golden Hour", "genres": ["indie", "dream-pop"], "energy": 5, "tempo": 105, "mood": ["warm", "nostalgic", "happy"]},
    {"id": "t24", "title": "Steel City", "genres": ["industrial", "electronic"], "energy": 8, "tempo": 130, "mood": ["dark", "urban", "intense"]},
    {"id": "t25", "title": "Smooth Operator", "genres": ["jazz", "r&b"], "energy": 4, "tempo": 88, "mood": ["smooth", "confident", "warm"]},
    {"id": "t26", "title": "Road Trip", "genres": ["rock", "pop"], "energy": 7, "tempo": 125, "mood": ["upbeat", "adventurous", "driving"]},
    {"id": "t27", "title": "Deep Space", "genres": ["ambient", "electronic"], "energy": 2, "tempo": 80, "mood": ["spacious", "focus", "calm"]},
    {"id": "t28", "title": "Groove Machine", "genres": ["funk", "electronic"], "energy": 7, "tempo": 115, "mood": ["groovy", "upbeat", "playful"]},
    {"id": "t29", "title": "Before the Storm", "genres": ["orchestral", "cinematic"], "energy": 6, "tempo": 110, "mood": ["tense", "epic", "dramatic"]},
    {"id": "t30", "title": "Easy Miles", "genres": ["pop", "acoustic"], "energy": 4, "tempo": 98, "mood": ["relaxed", "happy", "easygoing"]},
]

_SYSTEM = """\
You extract music taste parameters from a natural-language description.
Return ONLY a JSON object with these fields:
- genres: list of 1-3 genre strings (e.g. ["electronic", "ambient"])
- energy: integer 1-10 (1=very calm, 10=very intense)
- tempo_range: [min_bpm, max_bpm]
- mood_tags: list of 2-4 mood strings
- avoid: list of genres/moods to exclude (can be empty list)"""

_USER = "Music preference: {description}\n\nReturn taste parameters as JSON:"


def _score(track: dict, params: dict) -> float:
    score = 0.0
    genres = set(params.get("genres", []))
    mood_tags = set(params.get("mood_tags", []))
    avoid = set(params.get("avoid", []))
    energy = params.get("energy", 5)
    tempo_min, tempo_max = params.get("tempo_range", [60, 180])

    # Avoid penalty
    if any(a in track["genres"] for a in avoid):
        return -99.0
    if any(a in track["mood"] for a in avoid):
        return -99.0

    # Genre overlap
    score += len(genres & set(track["genres"])) * 3.0

    # Mood overlap
    score += len(mood_tags & set(track["mood"])) * 2.0

    # Energy proximity (closer = better)
    score -= abs(track["energy"] - energy) * 0.5

    # Tempo in range
    if tempo_min <= track["tempo"] <= tempo_max:
        score += 1.5

    return score


async def query(description: str, n: int = 5) -> Optional[list[dict]]:
    """
    Given a natural-language taste description, return up to n matched tracks.
    Returns None if the LLM is unavailable.
    """
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER.format(description=description)},
    ]

    raw = await llm_mod.complete(messages, max_tokens=150, temperature=0.2)
    if raw is None:
        # Fallback: return a mix of mid-energy tracks
        log.info("Music: LLM unavailable, returning default mix")
        return sorted(CATALOGUE, key=lambda t: abs(t["energy"] - 5))[:n]

    try:
        params = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Music: failed to parse taste params — raw=%r", raw[:80])
        return None

    scored = [(t, _score(t, params)) for t in CATALOGUE]
    scored = [(t, s) for t, s in scored if s > -90]
    scored.sort(key=lambda x: x[1], reverse=True)

    result = [t for t, _ in scored[:n]]
    log.info("Music query %r → %d matches", description[:40], len(result))
    return result
