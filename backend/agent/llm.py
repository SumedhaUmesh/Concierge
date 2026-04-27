"""
Singleton LLM wrapper around llama-cpp-python.

Loads the first .gguf file found in the project's models/ directory
with Metal GPU acceleration. Falls back to None (no-op) when no model
is present so the server still runs for dashboard-only testing.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).parent.parent.parent / "models"

_llm = None          # llama_cpp.Llama instance, or None
_load_attempted = False


def _find_model() -> Optional[Path]:
    gguf_files = sorted(MODELS_DIR.glob("*.gguf"))
    return gguf_files[0] if gguf_files else None


def _load():
    global _llm, _load_attempted
    if _load_attempted:
        return
    _load_attempted = True

    model_path = _find_model()
    if model_path is None:
        log.warning(
            "No .gguf model found in %s — agent will run in no-op mode. "
            "Run scripts/fetch_model.sh to download LFM2.5-1.2B-Instruct.",
            MODELS_DIR,
        )
        return

    try:
        from llama_cpp import Llama  # noqa: PLC0415

        log.info("Loading model: %s", model_path.name)
        _llm = Llama(
            model_path=str(model_path),
            n_gpu_layers=-1,   # offload all layers to Metal
            n_ctx=4096,
            verbose=False,
        )
        log.info("Model loaded.")
    except Exception:
        log.exception("Failed to load model — agent will run in no-op mode.")


def get_llm():
    """Return the Llama instance, loading it on first call."""
    _load()
    return _llm


async def complete(
    messages: list[dict],
    max_tokens: int = 256,
    temperature: float = 0.0,
    grammar=None,
) -> Optional[str]:
    """
    Async chat completion. Wraps the blocking call in a thread.
    Returns the text of the first choice, or None on failure.
    """
    llm = get_llm()
    if llm is None:
        return None

    def _run():
        kwargs = dict(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if grammar is not None:
            kwargs["grammar"] = grammar
        result = llm.create_chat_completion(**kwargs)
        return result["choices"][0]["message"]["content"].strip()

    try:
        return await asyncio.to_thread(_run)
    except Exception:
        log.exception("LLM completion failed")
        return None
