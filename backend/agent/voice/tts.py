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
_queue: asyncio.Queue = asyncio.Queue()
_worker_started = False


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
    """Call once at server startup to resolve the voice and start the TTS worker."""
    global _voice, _worker_started
    _voice = _resolve_voice()
    if not _worker_started:
        _worker_started = True
        asyncio.get_event_loop().create_task(_tts_worker())


async def _tts_worker():
    """Drain the TTS queue one utterance at a time — prevents overlapping say processes."""
    while True:
        text = await _queue.get()
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["say", "-v", _voice, "-r", str(_RATE), text],
                check=True,
                timeout=30,
            )
        except Exception:
            log.exception("TTS worker failed for text: %r", text[:40])
        finally:
            _queue.task_done()


def set_muted(muted: bool):
    global _muted
    _muted = muted
    log.info("TTS muted: %s", muted)


def is_muted() -> bool:
    return _muted


async def speak(text: str) -> None:
    if _muted or not text:
        return
    await _queue.put(text[:160])


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

    # Wait for all queued utterances to finish before returning
    await _queue.join()
    return full_text
