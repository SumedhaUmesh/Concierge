"""
AgentLoop — one instance per active WebSocket session.

Receives state ticks, throttles gate checks, fires the generator
when appropriate, and enforces the cooldown window. Never blocks
the state stream — all inference runs in background tasks.
"""

import asyncio
import logging
import time
from typing import Callable, Optional

from agent.gate import should_speak
from agent.generator import generate_suggestion
from signals import Signal, Suggestion

log = logging.getLogger(__name__)

GATE_INTERVAL_SEC = 3       # minimum seconds between gate checks
COOLDOWN_SEC      = 180     # seconds of silence after a suggestion fires
WINDOW_SIZE       = 3       # how many ticks to keep in the state window


class AgentLoop:
    def __init__(self, on_suggestion: Callable[[Suggestion], None]):
        """
        on_suggestion: async or sync callback invoked when a suggestion is ready.
        """
        self._on_suggestion = on_suggestion
        self._window: list[Signal] = []

        self._last_gate_at: float = 0.0
        self._last_suggestion_at: float = 0.0
        self._last_suggestion_type: Optional[str] = None

        self._dismissed: bool = False
        self._dismissed_at: float = 0.0

        self._running_task: Optional[asyncio.Task] = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def tick(self, state: Signal) -> None:
        """Called for every new state frame from the simulator."""
        self._window.append(state)
        if len(self._window) > WINDOW_SIZE:
            self._window.pop(0)

        now = time.monotonic()

        # Respect minimum gate interval
        if now - self._last_gate_at < GATE_INTERVAL_SEC:
            return

        # Respect cooldown after last suggestion
        if now - self._last_suggestion_at < COOLDOWN_SEC:
            return

        # Don't stack inference tasks
        if self._running_task and not self._running_task.done():
            return

        self._last_gate_at = now
        self._running_task = asyncio.create_task(self._run_inference())

    def dismiss(self) -> None:
        """Called when the driver dismisses a suggestion."""
        self._dismissed = True
        self._dismissed_at = time.monotonic()
        log.info("Suggestion dismissed — cooldown extended")

    def accept(self) -> None:
        """Called when the driver accepts / acts on a suggestion."""
        log.info("Suggestion accepted")

    def force_suggest(self, trigger: str) -> None:
        """Bypass the gate and generate a suggestion immediately (e.g. voice request)."""
        if self._running_task and not self._running_task.done():
            return
        self._running_task = asyncio.create_task(self._run_forced(trigger))

    async def _run_forced(self, trigger: str) -> None:
        if not self._window:
            return
        suggestion = await generate_suggestion(self._window, trigger=trigger)
        if suggestion is None:
            return
        self._last_suggestion_at = time.monotonic()
        self._last_suggestion_type = suggestion.type
        self._dismissed = False
        try:
            result = self._on_suggestion(suggestion)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("on_suggestion callback failed (forced)")

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _run_inference(self) -> None:
        if len(self._window) == 0:
            return

        now = time.monotonic()
        mins_since_last = (now - self._last_suggestion_at) / 60.0
        mins_since_dismiss = (now - self._dismissed_at) / 60.0 if self._dismissed else 99.0

        speak, trigger = await should_speak(
            state_window=self._window,
            last_suggestion_type=self._last_suggestion_type,
            minutes_since_last=mins_since_last,
            was_dismissed=self._dismissed,
            minutes_since_dismiss=mins_since_dismiss,
        )

        if not speak:
            return

        suggestion = await generate_suggestion(self._window, trigger=trigger)
        if suggestion is None:
            return

        self._last_suggestion_at = time.monotonic()
        self._last_suggestion_type = suggestion.type
        self._dismissed = False

        try:
            result = self._on_suggestion(suggestion)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("on_suggestion callback failed")
