"""
Maps a voice transcript to one of four driver intents:
  accept   — yes, navigate, go, okay, do it
  dismiss  — no, dismiss, cancel, ignore, not now
  defer    — later, give me a minute, remind me
  music    — play, something chill, music request
  query    — what, how far, explain

Uses a 3-shot LLM prompt. Falls back to keyword matching if model is unavailable.
"""

import logging
from typing import Optional

from agent import llm as llm_mod

log = logging.getLogger(__name__)

INTENTS = ("accept", "dismiss", "defer", "music", "query")

_SYSTEM = """\
Classify a driver's spoken response into one intent. Return exactly one word.

accept  — agrees or wants to act: "yes", "navigate there", "go", "okay", "do it", "let's go"
dismiss — rejects: "no", "not now", "ignore", "cancel", "dismiss", "I'm fine"
defer   — wants a reminder: "maybe later", "give me a minute", "remind me", "not yet"
music   — music request: "play something", "I want music", "something chill", "put on jazz", "Hips don't lie by Shakira", "[song] by [artist]", any song or artist name
query   — question: "how far", "what time", "tell me more", "which exit"

Reply with one word only."""

_USER = 'Driver said: "{transcript}"\n\nIntent:'

# Keyword fallback when model is unavailable
_KEYWORDS = {
    "accept":  ["yes", "ok", "okay", "sure", "navigate", "go", "let's go", "do it", "good"],
    "dismiss": ["no", "not now", "cancel", "ignore", "dismiss", "nope", "stop", "fine"],
    "defer":   ["later", "minute", "remind", "maybe", "wait", "hold on"],
    "music":   ["music", "play", "song", "chill", "jazz", "rock", "something", "listen", "artist", "album", "track", "tune", "beat"],
}

import re
_BY_ARTIST = re.compile(r'\b\w[\w\s]+\bby\b', re.IGNORECASE)


def _keyword_classify(text: str) -> str:
    lower = text.lower()
    if _BY_ARTIST.search(text):
        return "music"
    for intent, words in _KEYWORDS.items():
        if any(w in lower for w in words):
            return intent
    return "query"


async def classify(transcript: str) -> str:
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER.format(transcript=transcript)},
    ]
    raw = await llm_mod.complete(messages, max_tokens=4, temperature=0.0)

    if raw is None:
        result = _keyword_classify(transcript)
        log.info("Intent (keyword fallback): %s ← %r", result, transcript[:40])
        return result

    word = raw.strip().lower().split()[0] if raw.strip() else "query"
    result = word if word in INTENTS else _keyword_classify(transcript)
    log.info("Intent: %s ← %r", result, transcript[:40])
    return result
