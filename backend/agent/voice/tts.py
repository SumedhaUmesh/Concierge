"""
TTS via macOS `say`.

Uses the neural voice "Flo (English (US))" when available (macOS 13+),
falls back to Samantha. Neural voices sound noticeably cleaner and are
the same engine Apple uses in Siri on device.
"""

import asyncio
import logging
import subprocess

log = logging.getLogger(__name__)

_PREFERRED = "Flo (English (US))"
_FALLBACK  = "Samantha"
_RATE      = 185   # words per minute; default is 175

_muted = False
_voice: str = _FALLBACK   # resolved at startup


def _resolve_voice() -> str:
    """Pick neural voice if installed, otherwise fall back."""
    try:
        result = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=5
        )
        if _PREFERRED in result.stdout:
            log.info("TTS: using neural voice %r", _PREFERRED)
            return _PREFERRED
    except Exception:
        pass
    log.info("TTS: neural voice not installed, using %r", _FALLBACK)
    return _FALLBACK


def init():
    """Call once at server startup to resolve the voice."""
    global _voice
    _voice = _resolve_voice()


def set_muted(muted: bool):
    global _muted
    _muted = muted
    log.info("TTS muted: %s", muted)


def is_muted() -> bool:
    return _muted


async def speak(text: str) -> None:
    if _muted:
        return
    spoken = text[:160]
    try:
        await asyncio.to_thread(
            subprocess.run,
            ["say", "-v", _voice, "-r", str(_RATE), spoken],
            check=True,
            timeout=20,
        )
    except Exception:
        log.exception("TTS failed for text: %r", spoken[:40])


async def speak_stream(token_gen) -> str:
    """
    Consume an async token generator and speak each sentence as it completes.
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
