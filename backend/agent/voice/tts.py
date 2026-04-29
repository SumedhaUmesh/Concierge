"""
TTS via macOS `say` — plays through system speakers.
Zero dependencies. Samantha voice sounds clean for a demo.
"""

import asyncio
import logging
import subprocess

log = logging.getLogger(__name__)

VOICE = "Samantha"
_muted = False


def set_muted(muted: bool):
    global _muted
    _muted = muted
    log.info("TTS muted: %s", muted)


def is_muted() -> bool:
    return _muted


async def speak(text: str) -> None:
    """Speak text through system speakers. Non-blocking."""
    if _muted:
        return
    spoken = text[:120]
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["say", "-v", VOICE, spoken],
            check=True,
            timeout=15,
        )
    except Exception:
        log.exception("TTS failed for text: %r", spoken[:40])


async def speak_stream(token_gen) -> str:
    """
    Consume an async token generator and speak each sentence as it completes.
    Sentence boundaries: '. ', '! ', '? ' (or followed by newline).
    Speaking the first sentence starts before the full response is generated,
    cutting perceived latency by 1–3 s on typical Q&A answers.
    Returns the full concatenated text.
    """
    _ENDINGS = (". ", "! ", "? ", ".\n", "!\n", "?\n")
    buffer = ""
    full_text = ""

    async for token in token_gen:
        buffer += token
        full_text += token

        for sep in _ENDINGS:
            pos = buffer.find(sep)
            if pos != -1:
                sentence = buffer[:pos + 1].strip()
                buffer = buffer[pos + len(sep):]
                if sentence:
                    await speak(sentence)
                break

    remainder = buffer.strip()
    if remainder:
        await speak(remainder)

    return full_text
