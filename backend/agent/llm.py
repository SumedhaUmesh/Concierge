"""
Singleton LLM wrapper around llama-cpp-python.

Loads the first .gguf file found in the project's models/ directory
with Metal GPU acceleration. Falls back to None (no-op) when no model
is present so the server still runs for dashboard-only testing.

Threading: Metal is not safe for concurrent calls. A threading.Lock
serialises every load + inference call so only one runs at a time.
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).parent.parent.parent / "models"

_llm = None
_ready = False          # True once load attempt is fully complete
_lock = threading.Lock()  # one Metal call at a time


def _find_model() -> Optional[Path]:
    gguf_files = sorted(MODELS_DIR.glob("*.gguf"))
    return gguf_files[0] if gguf_files else None


def _load():
    """Load the model. Must be called inside _lock."""
    global _llm, _ready

    model_path = _find_model()
    if model_path is None:
        log.warning(
            "No .gguf model found in %s — agent will run in no-op mode. "
            "Run scripts/fetch_model.sh to download LFM2.5-1.2B-Instruct.",
            MODELS_DIR,
        )
        _ready = True
        return

    try:
        from llama_cpp import Llama  # noqa: PLC0415
        log.info("Loading model: %s", model_path.name)
        _llm = Llama(
            model_path=str(model_path),
            n_gpu_layers=-1,
            n_ctx=4096,
            verbose=False,
        )
        log.info("Model loaded.")
    except Exception:
        log.exception("Failed to load model — agent will run in no-op mode.")
    finally:
        _ready = True


def _ensure_loaded():
    """Load on first call; blocks until load is complete."""
    global _ready
    with _lock:
        if not _ready:
            _load()
    return _llm


def get_llm():
    return _ensure_loaded()


async def complete(
    messages: list[dict],
    max_tokens: int = 256,
    temperature: float = 0.0,
    grammar=None,
) -> Optional[str]:
    """
    Async chat completion. Serialises through _lock so Metal is never
    called concurrently from multiple threads.
    """
    def _run():
        # Acquire lock for the full load + inference cycle
        with _lock:
            if not _ready:
                _load()
            if _llm is None:
                return None
            kwargs = dict(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if grammar is not None:
                kwargs["grammar"] = grammar
            result = _llm.create_chat_completion(**kwargs)
            return result["choices"][0]["message"]["content"].strip()

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        log.exception("LLM completion failed")
        return None
