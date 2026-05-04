"""
LLM-first voice intent classifier.

Single LLM call → structured JSON → direct handler dispatch.
No regex pre-filters. Keyword fallback when model is unavailable.
"""

import json
import logging
from agent import llm as llm_mod

log = logging.getLogger(__name__)

_SYSTEM = """\
Classify a driver's spoken request for an in-car AI assistant.
Return ONLY a JSON object — no markdown, no explanation.

Intents:
  accept    – agrees with current suggestion: "yes", "ok", "navigate there", "do it", "let's go"
  dismiss   – rejects: "no", "not now", "cancel", "skip it", "I'm fine"
  defer     – wants a reminder: "maybe later", "remind me", "not yet"
  cabin     – comfort/temperature/AC/windows: "too hot", "I'm cold", "open the window",
               "turn on AC", "it's stuffy", "close the sunroof", "I'm sweating"
  meal      – food or drink search: "I'm hungry", "find me a coffee", "I want pizza",
               "get me a burger", "I feel like sushi"
  music     – music request: "play something calm", "I want upbeat music",
               "[song] by [artist]", "put on some jazz"
  navigate  – routing or destination: "take me home", "scenic route", "fastest way",
               "avoid the highway", "I want to see the coast"
  query     – factual question: "how far is it", "how much fuel do I have",
               "what time will I arrive", "which exit"
  compound  – clearly multiple intents in one: "find coffee and check if I have time
               before my meeting", "I need food and gas"
  other     – emotional state, vague, or doesn't fit above: "I'm tired", "I'm stressed",
               "long drive today", "I'm bored"

Note: the transcription may contain speech recognition errors. Common mistakes:
  "called" → "cold", "thought" → "hot", "warned" → "warm", "holds" → "cold",
  "find" → "fine", "board" → "bored". Use surrounding context to infer true meaning.

Return schema — include only relevant fields, set unused ones to null:
{
  "intent":       "<intent>",
  "cuisine":      "<coffee|pizza|burger|sushi|taco|sandwich|salad|indian|thai|korean|chinese|breakfast — or null>",
  "mood":         "<music mood string if intent=music, else null>",
  "energy":       <1-10 if intent=music, else null>,
  "cabin_action": "<cool|warm|ac_on|windows_open|windows_close|sunroof_open|sunroof_close — or null>",
  "celsius":      <target temp number if explicitly stated, else null>,
  "destination":  "<destination string if intent=navigate, else null>",
  "fast":         <true if fastest/quickest/direct route requested, else false>
}"""

_USER = 'Driver said: "{t}"\nJSON:'

# Ordered keyword fallback — evaluated top to bottom, first match wins
_FALLBACK_RULES = [
    ("accept",   ["yes", "ok", "okay", "sure", "navigate", "go ahead", "let's go", "do it",
                  "sounds good", "great"]),
    ("dismiss",  ["no", "not now", "cancel", "ignore", "dismiss", "nope", "skip", "no thanks",
                  "i'm fine", "fine"]),
    ("defer",    ["later", "remind me", "maybe", "wait", "hold on", "give me a minute"]),
    ("cabin",    ["hot", "cold", "warm", "cool", "stuffy", "freezing", "chilly", "burning",
                  "sweating", "temperature", "air con", "air conditioning", " ac ", "heat",
                  "fan", "window", "sunroof", "degrees"]),
    ("meal",     ["hungry", "starving", "food", "eat", "lunch", "dinner", "breakfast",
                  "restaurant", "snack", "meal", "coffee", "burger", "pizza", "sushi",
                  "taco", "sandwich", "cafe", "i want to eat", "find me food"]),
    ("music",    ["music", "play", "song", "chill", "jazz", "rock", "upbeat", " by ",
                  "put on", "something calm", "playlist"]),
    ("navigate", ["take me", "navigate to", "directions", "go home", "route", "scenic",
                  "fastest", "avoid highway", "coastal road", "countryside"]),
    ("query",    ["how far", "how long", "what time", "how much", "when will", "tell me",
                  "which exit", "what's the"]),
]


def _keyword_fallback(text: str) -> dict:
    lower = text.lower()
    for intent, kws in _FALLBACK_RULES:
        if any(k in lower for k in kws):
            return {"intent": intent}
    return {"intent": "other"}


async def classify(transcript: str) -> dict:
    """
    Single LLM call → structured intent dict with extracted parameters.
    Falls back to keyword matching if the model is unavailable or parse fails.
    """
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",   "content": _USER.format(t=transcript)},
    ]
    raw = await llm_mod.complete(messages, max_tokens=120, temperature=0.0)

    if not raw:
        result = _keyword_fallback(transcript)
        log.info("Intent [keyword]: %s ← %r", result["intent"], transcript[:50])
        return result

    try:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("no JSON object in response")
        result = json.loads(raw[start:end])
        if "intent" not in result:
            raise ValueError("missing 'intent' field")
        log.info("Intent [LLM]: %s ← %r", result.get("intent"), transcript[:50])
        return result
    except Exception as exc:
        log.warning("Intent parse failed (%s): %r — falling back to keywords", exc, (raw or "")[:80])
        result = _keyword_fallback(transcript)
        log.info("Intent [keyword]: %s ← %r", result["intent"], transcript[:50])
        return result
