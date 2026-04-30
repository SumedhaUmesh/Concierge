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
from agent.driver_model import compute as compute_driver_state
from signals import Signal, Suggestion

log = logging.getLogger(__name__)

GATE_INTERVAL_SEC    = 3    # minimum seconds between gate checks
COOLDOWN_SEC         = 180  # seconds of silence after a suggestion fires
WINDOW_SIZE          = 3    # how many ticks to keep in the state window
GEOFENCE_COOLDOWN    = 600  # seconds before re-triggering the same place
_COOLDOWN_MIN        = 60   # floor for adaptive cooldown
_COOLDOWN_MAX        = 480  # ceiling for adaptive cooldown


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

        self._last_geofence_name: Optional[str] = None
        self._last_geofence_at: float = 0.0

        # Shared Control Model — driver trust tracking
        self._accept_streak: int = 0
        self._dismiss_streak: int = 0
        self._adaptive_cooldown: float = float(COOLDOWN_SEC)
        self.driver_trust: float = 0.5   # 0=low trust, 1=high trust (public for UI)

    # ── Public API ────────────────────────────────────────────────────────────

    async def tick(self, state: Signal) -> None:
        """Called for every new state frame from the simulator."""
        # Cognitive Driver Model — compute and stamp onto state each tick
        ds = compute_driver_state(state)
        state.fatigue_index   = ds.fatigue_index
        state.cognitive_load  = ds.cognitive_load
        state.stress_index    = ds.stress_index
        state.driver_risk     = ds.risk_level

        # High fatigue → lower rest-stop threshold to 60 min
        if ds.fatigue_index > 0.6:
            state.minutes_driving_continuously = max(
                state.minutes_driving_continuously, 91.0
            )

        self._window.append(state)
        if len(self._window) > WINDOW_SIZE:
            self._window.pop(0)

        now = time.monotonic()

        # Respect minimum gate interval
        if now - self._last_gate_at < GATE_INTERVAL_SEC:
            return

        # Respect adaptive cooldown (Shared Control Model adjusts this)
        if now - self._last_suggestion_at < self._adaptive_cooldown:
            return

        # Don't stack inference tasks
        if self._running_task and not self._running_task.done():
            return

        # Geofence check — proactively surface known nearby places
        self._check_geofence(state, now)

        self._last_gate_at = now
        self._running_task = asyncio.create_task(self._run_inference())

    def reset_cooldown(self) -> None:
        """Full reset for new scenario — clears all accumulated state so the next gate fires fresh."""
        self._window.clear()
        self._last_gate_at = 0.0
        self._last_suggestion_at = 0.0
        self._last_suggestion_type = None
        self._dismissed = False
        self._dismissed_at = 0.0
        self._last_geofence_name = None
        self._last_geofence_at = 0.0
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()
        self._running_task = None
        log.info("AgentLoop: full reset for new scenario")

    def dismiss(self) -> None:
        """Called when the driver dismisses a suggestion."""
        self._dismissed = True
        self._dismissed_at = time.monotonic()
        self._dismiss_streak += 1
        self._accept_streak = 0
        # Shared Control: back off if driver keeps dismissing
        self._adaptive_cooldown = min(
            COOLDOWN_SEC * (1.0 + self._dismiss_streak * 0.4),
            _COOLDOWN_MAX,
        )
        self.driver_trust = max(0.0, self.driver_trust - 0.08)
        log.info("Dismissed (streak=%d) → cooldown=%.0fs trust=%.2f",
                 self._dismiss_streak, self._adaptive_cooldown, self.driver_trust)

    def accept(self) -> None:
        """Called when the driver accepts / acts on a suggestion."""
        self._accept_streak += 1
        self._dismiss_streak = 0
        # Shared Control: tighten cooldown when driver trusts the AI
        self._adaptive_cooldown = max(
            COOLDOWN_SEC * (1.0 - self._accept_streak * 0.12),
            _COOLDOWN_MIN,
        )
        self.driver_trust = min(1.0, self.driver_trust + 0.10)
        log.info("Accepted (streak=%d) → cooldown=%.0fs trust=%.2f",
                 self._accept_streak, self._adaptive_cooldown, self.driver_trust)

    def suppress(self, minutes: float) -> None:
        """Silence proactive suggestions for N minutes (reduce_alerts action)."""
        self._last_suggestion_at = time.monotonic() + minutes * 60 - self._adaptive_cooldown
        log.info("AgentLoop: suppressed for %.0f min", minutes)

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

    def _check_geofence(self, state: Signal, now: float) -> None:
        """Fire a proactive suggestion when approaching a previously visited place."""
        import trip_memory  # noqa: PLC0415
        place = trip_memory.get_nearby_accepted_place(state.lat, state.lng, radius_km=0.5)
        if not place:
            return
        same_place = place["name"] == self._last_geofence_name
        cooldown_ok = (now - self._last_geofence_at) > GEOFENCE_COOLDOWN
        suggestion_ok = (now - self._last_suggestion_at) > COOLDOWN_SEC
        if same_place and not cooldown_ok:
            return
        if not suggestion_ok:
            return
        self._last_geofence_name = place["name"]
        self._last_geofence_at = now
        visits = place["visits"]
        trigger = (
            f"driver is {place['distance_km']} km from {place['name']}, "
            f"a place they've visited {visits} time{'s' if visits != 1 else ''} before "
            f"— suggest stopping (type={place['type']})"
        )
        log.info("Geofence: near %s (%.2f km)", place["name"], place["distance_km"])
        if not (self._running_task and not self._running_task.done()):
            self._running_task = asyncio.create_task(self._run_forced(trigger))

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
