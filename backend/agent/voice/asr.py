"""
Whisper-based ASR. Accepts raw WAV bytes (16kHz mono PCM) recorded by
the browser and returns a transcript string.

Model is loaded once on first call (tiny.en, ~150 MB, runs on MPS/CPU).
"""

import asyncio
import base64
import io
import logging
from typing import Optional

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

_model = None
_load_attempted = False


def _get_model():
    global _model, _load_attempted
    if _load_attempted:
        return _model
    _load_attempted = True
    try:
        import whisper  # noqa: PLC0415
        log.info("Loading Whisper tiny.en…")
        _model = whisper.load_model("tiny.en")
        log.info("Whisper ready.")
    except Exception:
        log.exception("Failed to load Whisper — ASR disabled")
    return _model


async def transcribe(audio_b64: str) -> Optional[str]:
    """
    Transcribe base64-encoded WAV audio (16kHz mono, 16-bit PCM).
    Returns transcript text, or None on failure.
    """
    model = _get_model()
    if model is None:
        return None

    def _run():
        raw = base64.b64decode(audio_b64)
        buf = io.BytesIO(raw)
        data, sr = sf.read(buf, dtype="float32")

        # Ensure mono
        if data.ndim > 1:
            data = data.mean(axis=1)

        # Whisper expects float32 at 16kHz — browser records at exactly 16kHz
        result = model.transcribe(data, language="en", fp16=False)
        text = result["text"].strip()
        if text:
            log.info("ASR: %r", text)
        else:
            log.debug("ASR: empty (no speech detected)")
        return text

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        log.exception("ASR transcription failed")
        return None
