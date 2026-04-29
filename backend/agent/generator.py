"""
Suggestion generator: given the current state window, produce a
structured Suggestion. Uses a GBNF grammar to guarantee valid JSON.
"""

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from agent import llm as llm_mod
from agent.prompts import SUGGESTION_GENERATOR_V1_SYSTEM, SUGGESTION_GENERATOR_V1_USER
from signals import Suggestion
import trip_memory

log = logging.getLogger(__name__)

_GRAMMAR_PATH = Path(__file__).parent / "grammars" / "suggestion.gbnf"
_grammar_obj = None  # loaded on first use; reload server to pick up .gbnf changes

# Metrics
_total_calls = 0
_parse_failures = 0
_latencies: list[float] = []


def stats() -> dict:
    lats = _latencies[-100:]
    return {
        "total": _total_calls,
        "parse_failures": _parse_failures,
        "parse_rate": 1 - (_parse_failures / max(_total_calls, 1)),
        "latency_p50": sorted(lats)[len(lats) // 2] if lats else 0,
        "latency_p95": sorted(lats)[int(len(lats) * 0.95)] if lats else 0,
    }


def _get_grammar():
    global _grammar_obj
    if _grammar_obj is not None:
        return _grammar_obj
    try:
        from llama_cpp import LlamaGrammar  # noqa: PLC0415
        _grammar_obj = LlamaGrammar.from_file(str(_GRAMMAR_PATH))
        log.info("Grammar loaded from %s", _GRAMMAR_PATH.name)
    except Exception:
        log.exception("Could not load grammar — output will be unconstrained")
    return _grammar_obj


def _compact_state(state) -> dict:
    d = asdict(state) if not isinstance(state, dict) else state
    compact = {k: v for k, v in d.items() if v is not None and v != 0}
    # rename for brevity
    compact.pop("is_on_highway", None)
    return compact


async def generate_suggestion(state_window: list, trigger: Optional[str] = None) -> Optional[Suggestion]:
    global _total_calls, _parse_failures
    _total_calls += 1
    t0 = time.monotonic()

    latest = state_window[-1]
    state_json = json.dumps(_compact_state(latest), indent=2)

    prefs_ctx = trip_memory.get_context_string()
    preferences_block = prefs_ctx if prefs_ctx else "No driver preferences recorded yet."

    messages = [
        {"role": "system", "content": SUGGESTION_GENERATOR_V1_SYSTEM},
        {"role": "user", "content": SUGGESTION_GENERATOR_V1_USER.format(
            state_json=state_json,
            trigger=trigger or "generate the most relevant suggestion based on state",
            preferences=preferences_block,
        )},
    ]

    grammar = _get_grammar()
    raw = await llm_mod.complete(messages, max_tokens=200, temperature=0.0, grammar=grammar)

    latency = time.monotonic() - t0
    _latencies.append(latency)

    if raw is None:
        _parse_failures += 1
        return None

    try:
        data = json.loads(raw)
        suggestion = Suggestion(
            type=data["type"],
            urgency=int(data["urgency"]),
            headline=data["headline"],
            detail=data["detail"],
            suggested_action=data["suggested_action"],
        )
        log.info(
            "Generator: [%s urgency=%d] %s (%.1fs)",
            suggestion.type, suggestion.urgency, suggestion.headline, latency
        )
        return suggestion
    except (json.JSONDecodeError, KeyError, ValueError):
        _parse_failures += 1
        log.warning("Generator: parse failure — raw=%r", raw[:120])
        return None
