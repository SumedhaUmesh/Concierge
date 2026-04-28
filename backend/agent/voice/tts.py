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
    # Trim to ~100 chars so it doesn't ramble on
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
