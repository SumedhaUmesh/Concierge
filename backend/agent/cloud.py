"""
Cloud AI via OpenRouter (openrouter.ai).

OpenRouter provides a unified API for Claude, GPT-4, Gemini, and others.
Set OPENROUTER_API_KEY in .env to enable.

Two use cases:
  1. Q&A fallback  — driver asks a factual question the local LFM can't answer well
  2. Compound orchestration — multi-intent queries needing real reasoning
     (meeting-time math, route + schedule trade-offs, etc.)

Falls back gracefully — all callers check for None and use the local LFM instead.
"""

import json
import logging
import os
from typing import AsyncIterator, Optional

import aiohttp

log = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
_MODEL    = "anthropic/claude-haiku-4-5-20251001"

_HEDGES = (
    "i don't know", "i'm not sure", "i cannot", "i can't",
    "not enough information", "unable to", "no information",
    "i do not have", "i'm unable",
)

_QA_SYSTEM = """\
You are a concise in-car voice assistant. Answer the driver's spoken question
using the vehicle state provided. Be specific with numbers. One or two short
sentences only. Do not offer unsolicited advice."""

_ORCHESTRATE_SYSTEM = """\
You are Concierge, an in-car AI assistant. Translate the driver's message into a structured action plan.

Rules:
- Food/drink only: ONLY include find_poi:food. Do NOT add music, cabin_temp, or navigation.
- Compound queries with meeting check: calculate margin = time_until_meeting - (normal_travel + traffic_delay + detour + 10min_stop). State clearly if driver has time or not.
- Emotional states: music + cabin_temp only. No find_poi unless explicitly asked.
- Keep reply warm and direct — 1-2 spoken sentences.
- Only include actions directly relevant to the request. Omit everything else.

Output ONLY valid JSON:
{
  "interpretation": "one sentence",
  "confidence": "high" | "medium" | "low",
  "clarify": null,
  "reply": "1-2 spoken sentences",
  "actions": [
    {"type": "cabin_temp", "celsius": 20},
    {"type": "music", "mood": "calm", "energy": 3},
    {"type": "find_poi", "category": "food"},
    {"type": "navigate", "destination": "home"},
    {"type": "reduce_alerts", "minutes": 15},
    {"type": "windows", "open": false},
    {"type": "ac", "on": true}
  ]
}"""


def _get_key() -> Optional[str]:
    return os.environ.get("OPENROUTER_API_KEY", "").strip() or None


def needs_cloud(local_answer: str) -> bool:
    """Return True if the local LLM answer is too weak to use."""
    if not local_answer or len(local_answer.strip()) < 30:
        return True
    return any(h in local_answer.lower() for h in _HEDGES)


async def _complete(messages: list[dict], max_tokens: int = 400) -> Optional[str]:
    """Single (non-streaming) OpenRouter completion."""
    key = _get_key()
    if not key:
        return None

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "http://localhost:8000",
        "X-Title":       "Concierge In-Car AI",
    }
    payload = {
        "model":       _MODEL,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": 0.0,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_BASE_URL, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("OpenRouter error %d: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception:
        log.exception("OpenRouter request failed")
        return None


async def orchestrate_compound(
    utterance: str,
    state_json: str,
    conversation_context: str,
) -> Optional[dict]:
    """
    Use Claude (via OpenRouter) to orchestrate complex/compound queries.
    Returns a plan dict, or None if unavailable — caller falls back to local LLM.
    """
    if not _get_key():
        return None

    messages = [
        {"role": "system", "content": _ORCHESTRATE_SYSTEM},
        {"role": "user",   "content": (
            f"Vehicle state: {state_json}\n"
            f"Recent conversation:\n{conversation_context}\n\n"
            f'Driver said: "{utterance}"\n\nJSON:'
        )},
    ]

    raw = await _complete(messages, max_tokens=400)
    if not raw:
        return None

    try:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        plan = json.loads(raw[start:end])
        log.info("Cloud orchestrator: %r (%d actions)",
                 plan.get("interpretation", "?")[:60], len(plan.get("actions", [])))
        return plan
    except Exception as exc:
        log.warning("Cloud orchestrator parse failed: %s", exc)
        return None


async def stream_answer(
    question: str,
    state_json: str,
) -> Optional[AsyncIterator[str]]:
    """
    Stream a spoken answer to a driver question via OpenRouter.
    Returns an async generator of text chunks, or None if unavailable.
    """
    key = _get_key()
    if not key:
        log.warning("OPENROUTER_API_KEY not set — cloud Q&A disabled")
        return None

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "http://localhost:8000",
        "X-Title":       "Concierge In-Car AI",
    }
    payload = {
        "model":       _MODEL,
        "messages":    [
            {"role": "system", "content": _QA_SYSTEM},
            {"role": "user",   "content":
                f"Vehicle state:\n{state_json}\n\nDriver asked: \"{question}\"\n\nAnswer:"},
        ],
        "max_tokens":  150,
        "temperature": 0.0,
        "stream":      True,
    }

    async def _gen():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(_BASE_URL, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        log.error("OpenRouter stream error %d", resp.status)
                        return
                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield delta
                        except Exception:
                            continue
        except Exception:
            log.exception("OpenRouter stream failed")

    return _gen()
