"""
Deepgram Nova-2 ASR. Accepts raw WAV bytes (16kHz mono PCM) recorded by
the browser and returns a transcript string.

API key is read from DEEPGRAM_API_KEY env var (loaded from project .env).
"""

import base64
import logging
import os
from pathlib import Path
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

_URL = (
    "https://api.deepgram.com/v1/listen"
    "?model=nova-2"
    "&language=en"
    "&smart_format=true"
    "&punctuate=true"
    "&filler_words=false"
)

_key: Optional[str] = None


def _get_key() -> Optional[str]:
    global _key
    if _key:
        return _key
    # Try env var (set at startup by server.py dotenv load)
    _key = os.environ.get("DEEPGRAM_API_KEY", "").strip() or None
    if not _key:
        log.error("DEEPGRAM_API_KEY not set — ASR disabled")
    return _key


async def transcribe(audio_b64: str) -> Optional[str]:
    """
    Transcribe base64-encoded WAV audio using Deepgram Nova-2.
    Returns transcript text, or None on failure.
    """
    key = _get_key()
    if not key:
        return None

    raw = base64.b64decode(audio_b64)
    headers = {
        "Authorization": f"Token {key}",
        "Content-Type": "audio/wav",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_URL, headers=headers, data=raw) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("Deepgram error %d: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
                transcript = (
                    data.get("results", {})
                    .get("channels", [{}])[0]
                    .get("alternatives", [{}])[0]
                    .get("transcript", "")
                    .strip()
                )
                if transcript:
                    log.info("ASR: %r", transcript)
                else:
                    log.info("ASR: empty transcript (Deepgram found no speech)")
                return transcript
    except Exception:
        log.exception("ASR transcription failed")
        return None
