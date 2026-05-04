"""
TTS via macOS `say`.

Preference order: Eddy → Flo → Samantha.
Eddy (English (US)) is one of Apple's newer neural voices — clear and natural,
well-suited for a concierge. Falls back gracefully on older macOS.
"""

import asyncio
import logging
import subprocess
import time as _time

log = logging.getLogger(__name__)

_PREFERRED_LIST = [
    "Eddy (English (US))",   # best natural male neural voice
    "Flo (English (US))",    # natural female neural voice
    "Samantha",              # classic fallback (always present)
]
_RATE = 185   # words per minute; default is 175

_muted = False
_voice: str = "Samantha"   # resolved at startup by init()
_queue: asyncio.Queue = asyncio.Queue()
_worker_started = False
_last_text: str = ""
_last_at: float = 0.0

# Optional callbacks registered by server.py to broadcast tts_start / tts_end
_on_speak_start = None   # async callable(text: str)
_on_speak_end   = None   # async callable()


def register_speak_events(on_start, on_end) -> None:
    """Register async callbacks fired before/after each spoken utterance."""
    global _on_speak_start, _on_speak_end
    _on_speak_start = on_start
    _on_speak_end   = on_end


def _resolve_voice() -> str:
    """Pick the first available voice from the preference list."""
    try:
        result = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=5
        )
        installed = result.stdout
        for candidate in _PREFERRED_LIST:
            if candidate in installed:
                log.info("TTS: using voice %r", candidate)
                return candidate
    except Exception:
        pass
    log.info("TTS: could not enumerate voices, using Samantha")
    return "Samantha"


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
            if _on_speak_start:
                try:
                    await _on_speak_start(text)
                except Exception:
                    pass
            await asyncio.to_thread(
                subprocess.run,
                ["say", "-v", _voice, "-r", str(_RATE), text],
                check=True,
                timeout=30,
            )
        except Exception:
            log.exception("TTS worker failed for text: %r", text[:40])
        finally:
            if _on_speak_end:
                try:
                    await _on_speak_end()
                except Exception:
                    pass
            _queue.task_done()


def set_muted(muted: bool):
    global _muted
    _muted = muted
    log.info("TTS muted: %s", muted)


def is_muted() -> bool:
    return _muted


async def speak(text: str) -> None:
    global _last_text, _last_at
    if _muted or not text:
        return
    now = _time.monotonic()
    if text[:80] == _last_text[:80] and (now - _last_at) < 5.0:
        log.debug("TTS dedup: skipping duplicate %r", text[:40])
        return
    _last_text = text
    _last_at = now
    # Truncate at a sentence boundary so TTS never cuts off mid-word
    chunk = text[:160]
    if len(text) > 160:
        for sep in (". ", "! ", "? "):
            pos = chunk.rfind(sep)
            if pos > 40:
                chunk = chunk[:pos + 1]
                break
    await _queue.put(chunk)


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
