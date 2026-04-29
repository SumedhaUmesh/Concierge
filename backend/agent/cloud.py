"""
Cloud fallback for complex Q&A queries.

Local LFM handles simple state lookups ("what's my fuel?").
Claude Haiku handles reasoning, comparisons, open-ended questions
("should I stop now or wait until the highway?").

The escalation decision is made by inspecting the local answer:
- fewer than 30 characters → too short, likely a non-answer
- contains hedging phrases  → model was uncertain

Requires ANTHROPIC_API_KEY in environment.
"""

import logging
import os
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)

_HEDGES = (
    "i don't know", "i'm not sure", "i cannot", "i can't",
    "not enough information", "unable to", "no information",
    "i do not have", "i'm unable",
)

_SYSTEM = """\
You are a concise in-car voice assistant. Answer the driver's spoken question
using the vehicle state provided. Be specific with numbers. One or two short
sentences only. Do not offer unsolicited advice."""


def needs_cloud(local_answer: str) -> bool:
    """Return True if the local answer looks too weak to use."""
    if not local_answer or len(local_answer.strip()) < 30:
        return True
    lower = local_answer.lower()
    return any(h in lower for h in _HEDGES)


async def stream_answer(
    question: str,
    state_json: str,
) -> Optional[AsyncIterator[str]]:
    """
    Stream a Claude Haiku answer as an async generator of text chunks.
    Returns None if ANTHROPIC_API_KEY is not set or the call fails.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set — cloud fallback disabled")
        return None

    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.AsyncAnthropic(api_key=api_key)

        user_content = f"Vehicle state:\n{state_json}\n\nDriver asked: \"{question}\"\n\nAnswer:"

        async def _gen():
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user_content}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text

        return _gen()

    except Exception:
        log.exception("Cloud fallback stream failed")
        return None
